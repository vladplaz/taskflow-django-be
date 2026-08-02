"""View helpers shared by more than one domain app."""

from django.http import JsonResponse
from drf_spectacular.utils import OpenApiParameter


def health(request):
    """Liveness probe, touching no database, cache, or bucket.

    A probe that fails when Postgres blips gets the container killed
    instead of the query retried.
    """
    return JsonResponse({"status": "ok"})


ARCHIVED_PARAMETER = OpenApiParameter(
    "archived",
    bool,
    description="Return archived rows instead of active ones.",
)

_TRUTHY = {"1", "true", "yes"}


class ArchiveFilterMixin:
    """Reads the `?archived=true` switch shared by every soft-deleted listing."""

    def wants_archived(self) -> bool:
        return self.request.query_params.get("archived", "").lower() in _TRUTHY
