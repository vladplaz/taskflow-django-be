"""Local development settings: everything unsafe-but-convenient lives here."""

from .base import *
from .env import env_bool, env_list, env_redis_url, env_str


DEBUG = True

SECRET_KEY = env_str(
    "DJANGO_SECRET_KEY",
    "django-insecure-local-development-key-change-before-deploying",
)

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["localhost", "127.0.0.1", "[::1]"])

DATABASES["default"]["PASSWORD"] = env_str("POSTGRES_PASSWORD", "taskflow_local_password")

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [env_redis_url(default="redis://localhost:6380/0")]},
    },
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "taskflow-dev",
    },
}

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

if env_bool("DJANGO_DEVELOPMENT_LATENCY", False):
    MIDDLEWARE.append("taskflow.middleware.DevelopmentLatencyMiddleware")

REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = (
    "rest_framework.renderers.JSONRenderer",
    "rest_framework.renderers.BrowsableAPIRenderer",
)
