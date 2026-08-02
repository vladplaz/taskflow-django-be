from django.apps import AppConfig


class AttachmentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.attachments"

    def ready(self) -> None:
        # Connects the post_delete receiver; imported here, not at module
        # level, so the app loads before its models are ready.
        from . import signals  # noqa: F401
