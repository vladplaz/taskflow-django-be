from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from taskflow.views import health


api_v1_patterns = [
    path("auth/", include("apps.accounts.urls")),
    path("", include("apps.accounts.profile_urls")),
    path("workspaces/", include("apps.workspaces.urls")),
    path("", include("apps.boards.urls")),
    path("", include("apps.tasks.urls")),
    path("", include("apps.attachments.urls")),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "schema/swagger-ui/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path(
        "schema/redoc/",
        SpectacularRedocView.as_view(url_name="schema"),
        name="redoc",
    ),
]

urlpatterns = [
    # Not "healthz": Google's front end answers that path itself on
    # *.run.app, so the request never reaches the container.
    path("health", health, name="health"),
    path("admin/", admin.site.urls),
    path("api/v1/", include(api_v1_patterns)),
]
