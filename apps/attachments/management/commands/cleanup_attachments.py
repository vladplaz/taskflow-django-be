"""Remove uploads that never became part of a comment.

python manage.py cleanup_attachments --dry-run
"""

from django.core.management.base import BaseCommand

from apps.attachments.services import unclaimed_attachments


class Command(BaseCommand):
    help = "Delete uploads that were never attached to a comment."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--hours",
            type=int,
            default=None,
            help="Minimum age in hours (default: ATTACHMENT_UNCLAIMED_MAX_AGE_HOURS).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting it.",
        )

    def handle(self, *args, **options) -> None:
        stale = unclaimed_attachments(older_than_hours=options["hours"])

        if options["dry_run"]:
            for attachment in stale:
                self.stdout.write(f"would delete {attachment.pk} {attachment.original_name}")
            self.stdout.write(self.style.SUCCESS(f"{stale.count()} unclaimed attachment(s)"))
            return

        deleted = 0
        for attachment in stale:
            attachment.delete()
            deleted += 1
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} unclaimed attachment(s)"))
