from django.contrib.auth import login, logout
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import generics, permissions, response, status, views

from apps.boards.access import visible_boards_for
from apps.workspaces.models import WorkspaceMembership

from .models import User
from .serializers import (
    LoginSerializer,
    MyProfileSerializer,
    RegistrationSerializer,
    UserSerializer,
)


@method_decorator(csrf_protect, name="dispatch")
class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    permission_classes = (permissions.AllowAny,)
    serializer_class = RegistrationSerializer
    throttle_scope = "register"

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        data = UserSerializer(user).data
        return response.Response(
            data,
            status=status.HTTP_201_CREATED,
            headers=self.get_success_headers(data),
        )


@extend_schema(
    request=LoginSerializer,
    responses={200: UserSerializer},
    description="Start a session cookie for the given credentials.",
)
@method_decorator(csrf_protect, name="dispatch")
class LoginView(views.APIView):
    permission_classes = (permissions.AllowAny,)
    throttle_scope = "login"

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        login(request, serializer.validated_data["user"])
        return response.Response(UserSerializer(request.user).data)


@extend_schema(
    request=None,
    responses={204: OpenApiResponse(description="Sets the csrftoken cookie.")},
)
@method_decorator(ensure_csrf_cookie, name="dispatch")
class CsrfView(views.APIView):
    permission_classes = (permissions.AllowAny,)
    authentication_classes = ()

    def get(self, request):
        return response.Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    request=None,
    responses={204: OpenApiResponse(description="Session destroyed.")},
)
@method_decorator(csrf_protect, name="dispatch")
class LogoutView(views.APIView):
    def post(self, request):
        logout(request)
        return response.Response(status=status.HTTP_204_NO_CONTENT)


class MeView(generics.RetrieveUpdateAPIView):
    serializer_class = MyProfileSerializer

    def get_object(self):
        return self.request.user


class WorkspaceUserProfileView(generics.RetrieveAPIView):
    """Look up one user the requester shares a workspace with."""

    serializer_class = UserSerializer

    def get_object(self):
        workspace_id = self.kwargs["workspace_id"]
        accessible_board_ids = (
            visible_boards_for(self.request.user).filter(workspace_id=workspace_id).values("id")
        )
        visible = Q(board_memberships__board_id__in=accessible_board_ids)
        requester_is_workspace_member = WorkspaceMembership.objects.filter(
            workspace_id=workspace_id,
            user=self.request.user,
        ).exists()
        if requester_is_workspace_member:
            visible |= Q(workspace_memberships__workspace_id=workspace_id)

        return get_object_or_404(
            User.objects.filter(visible).distinct(),
            pk=self.kwargs["user_id"],
        )
