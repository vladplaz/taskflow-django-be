"""Abstract model bases shared by the domain apps."""

from django.db import models


class CreatedAtModel(models.Model):
    """Records when a row was created."""

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class TimeStampedModel(CreatedAtModel):
    """Records when a row was created and last saved."""

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
