import os

from django.core.asgi import get_asgi_application


# Not dev: a deployment that forgets the variable must fail, not boot insecure.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "taskflow.settings.prod")

# The app registry must be populated before the routing imports below.
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import OriginValidator  # noqa: E402
from django.conf import settings  # noqa: E402

from apps.tasks.routing import websocket_urlpatterns  # noqa: E402


application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": OriginValidator(
            AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
            settings.CORS_ALLOWED_ORIGINS,
        ),
    }
)
