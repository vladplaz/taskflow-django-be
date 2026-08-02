"""Production settings; anything dangerous to guess uses ``env_str_required``."""

from .base import *
from .env import env_bool, env_int, env_list, env_redis_url, env_str, env_str_required


DEBUG = False

SECRET_KEY = env_str_required("DJANGO_SECRET_KEY")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", [])
if not ALLOWED_HOSTS:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be set in production.")

DATABASES["default"]["PASSWORD"] = env_str_required("POSTGRES_PASSWORD")

DATABASES["default"]["OPTIONS"]["sslmode"] = env_str("POSTGRES_SSLMODE", "require")

REDIS_URL = env_redis_url()

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [REDIS_URL]},
    },
}

# Shared across processes, or each worker enforces its own rate limit.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    },
}

# Attachment storage is configured in base.py via AWS_STORAGE_BUCKET_NAME.

# No proxy in front of Django, so WhiteNoise serves the collected static
# files. It belongs behind SecurityMiddleware, which owns the HTTPS redirect.
MIDDLEWARE.insert(
    MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1,
    "whitenoise.middleware.WhiteNoiseMiddleware",
)

STORAGES["staticfiles"] = {
    "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
}

# No SMTP host logs invitation emails instead of sending them; the invite
# link still works. Refusing to boot would block a deploy over a feature
# that degrades cleanly.
EMAIL_HOST = env_str("EMAIL_HOST", "")

if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = env_int("EMAIL_PORT", 587)
    EMAIL_HOST_USER = env_str("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = env_str("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# --- Transport security ---

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
SECURE_HSTS_SECONDS = env_int("DJANGO_SECURE_HSTS_SECONDS", 31_536_000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", True)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", True)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

if env_bool("DJANGO_BEHIND_PROXY", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# No browsable API: it renders HTML forms for authenticated endpoints.
REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = ("rest_framework.renderers.JSONRenderer",)
