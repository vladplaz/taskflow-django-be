"""Storage cleanup for deleted attachment rows.

Django has not deleted the underlying file since 1.3. A signal rather than a
service call, because `post_delete` also fires for cascaded rows.
"""

import logging

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import Attachment


logger = logging.getLogger(__name__)


@receiver(post_delete, sender=Attachment)
def delete_attachment_files(sender, instance: Attachment, **kwargs) -> None:
    """Remove the stored file and its thumbnail once the delete commits.

    `on_commit`, or a rollback leaves the row alive and its bytes gone.
    Storage errors are logged: the row is gone either way, and raising would
    only turn a stray file into a 500.
    """
    files = [file for file in (instance.file, instance.thumbnail) if file]

    def remove() -> None:
        for file in files:
            try:
                file.storage.delete(file.name)
            except Exception:
                logger.exception(
                    "Unable to delete attachment file from storage",
                    extra={"attachment_id": instance.pk, "name": file.name},
                )

    transaction.on_commit(remove)
