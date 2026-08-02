from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.functions import Lower

from .managers import UserManager


class User(AbstractUser):
    """A person who signs in with an email address rather than a username."""

    # None removes the inherited columns rather than carrying unused fields.
    username = None
    first_name = None
    last_name = None

    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=80)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["display_name"]

    objects = UserManager()

    class Meta:
        constraints = [
            # Case-insensitive uniqueness, plus the functional index that
            # Lower("email") lookups use.
            models.UniqueConstraint(Lower("email"), name="unique_user_email_ci"),
        ]

    def save(self, *args, **kwargs):
        self.email = self.__class__.objects.normalize_email(self.email)
        super().save(*args, **kwargs)

    def get_full_name(self) -> str:
        """Override: AbstractUser's implementation reads the removed name fields."""
        return self.display_name

    def get_short_name(self) -> str:
        return self.display_name

    def __str__(self) -> str:
        return self.email
