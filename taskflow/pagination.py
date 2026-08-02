"""Project-wide pagination; every list endpoint is paginated by default."""

from rest_framework.pagination import LimitOffsetPagination, PageNumberPagination


class DefaultPagination(PageNumberPagination):
    """Page-number pagination with a client-controllable, bounded page size."""

    page_size_query_param = "page_size"
    max_page_size = 200


class OffsetPagination(LimitOffsetPagination):
    """For lists whose batches are not all the same size.

    A card opens with five activity entries and loads ten at a time after
    that. A page number can only start at a multiple of its own page size.
    """

    default_limit = 20
    max_limit = 200
