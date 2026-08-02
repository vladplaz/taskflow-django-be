"""Create the attachment bucket when it does not exist yet.

    python manage.py ensure_attachment_bucket

Uses whatever `STORAGES["default"]` is configured against, so reaching a bucket
here proves the application reaches the same one with the same credentials.
"""

from django.core.files.storage import storages
from django.core.management.base import BaseCommand, CommandError


#: What "the bucket is not there" looks like across S3 and its emulators. 403
#: is absent on purpose: the bucket exists and belongs to somebody else.
_MISSING_BUCKET_CODES = frozenset({"404", "NoSuchBucket", "NotFound"})


class Command(BaseCommand):
    help = "Create the S3 bucket attachments are stored in, if it is missing."

    def handle(self, *args, **options) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        storage = storages["default"]
        bucket_name = getattr(storage, "bucket_name", None)
        if not bucket_name:
            raise CommandError(
                "The default storage is local disk, so there is no bucket to create. "
                "Set AWS_STORAGE_BUCKET_NAME to use S3."
            )

        client = storage.connection.meta.client
        try:
            client.head_bucket(Bucket=bucket_name)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in _MISSING_BUCKET_CODES:
                raise CommandError(f"Unable to reach {bucket_name}: {error}") from error
        except BotoCoreError as error:
            raise CommandError(f"Unable to reach the S3 endpoint: {error}") from error
        else:
            self.stdout.write(self.style.SUCCESS(f"{bucket_name} already exists"))
            return

        parameters: dict = {"Bucket": bucket_name}
        region = getattr(storage, "region_name", "")
        # us-east-1 is the one region that rejects an explicit constraint.
        if region and region != "us-east-1":
            parameters["CreateBucketConfiguration"] = {"LocationConstraint": region}

        try:
            client.create_bucket(**parameters)
        except (BotoCoreError, ClientError) as error:
            raise CommandError(f"Unable to create {bucket_name}: {error}") from error

        self.stdout.write(self.style.SUCCESS(f"Created {bucket_name}"))
