from django.contrib.auth import authenticate, password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from rest_framework import serializers

from apps.boards.models import BoardInvitation
from apps.workspaces.models import WorkspaceInvitation

from .models import User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "email", "display_name")
        read_only_fields = ("id",)


class MyProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "email", "display_name")
        read_only_fields = ("id", "email")


class RegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    invite_token = serializers.UUIDField(required=False, write_only=True)
    board_invite_token = serializers.UUIDField(required=False, write_only=True)

    class Meta:
        model = User
        fields = (
            "email",
            "display_name",
            "password",
            "invite_token",
            "board_invite_token",
        )

    def validate_email(self, email: str) -> str:
        email = User.objects.normalize_email(email)
        if User.objects.filter(email=email).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return email

    def validate_invitation(self, *, model, token, field: str, email: str):
        """Check a signup token points at a live invitation for this address."""
        invitation = model.objects.filter(token=token, status=model.Status.PENDING).first()
        if invitation is None or invitation.is_expired:
            raise serializers.ValidationError({field: "This invitation link is no longer valid."})
        if invitation.email.casefold() != email.casefold():
            raise serializers.ValidationError(
                {"email": "Use the email address this invitation was sent to."}
            )

    def validate(self, attrs):
        if invite_token := attrs.get("invite_token"):
            self.validate_invitation(
                model=WorkspaceInvitation,
                token=invite_token,
                field="invite_token",
                email=attrs["email"],
            )
        if board_invite_token := attrs.get("board_invite_token"):
            self.validate_invitation(
                model=BoardInvitation,
                token=board_invite_token,
                field="board_invite_token",
                email=attrs["email"],
            )
        try:
            password_validation.validate_password(
                attrs["password"],
                user=User(email=attrs["email"], display_name=attrs["display_name"]),
            )
        except DjangoValidationError as error:
            raise serializers.ValidationError({"password": error.messages}) from error
        return attrs

    def create(self, validated_data):
        validated_data.pop("invite_token", None)
        validated_data.pop("board_invite_token", None)
        try:
            with transaction.atomic():
                user = User.objects.create_user(**validated_data)
                for model in (WorkspaceInvitation, BoardInvitation):
                    model.objects.filter(
                        email=user.email,
                        status=model.Status.PENDING,
                    ).update(invited_user=user)
                return user
        except IntegrityError as error:
            # The unique constraint is the real guard against a race here.
            raise serializers.ValidationError(
                {"email": "A user with this email already exists."}
            ) from error


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        user = authenticate(
            request=self.context.get("request"),
            email=User.objects.normalize_email(attrs["email"]),
            password=attrs["password"],
        )
        if user is None:
            raise serializers.ValidationError("Unable to log in with those credentials.")
        attrs["user"] = user
        return attrs
