"""Test settings: ``dev`` with the slow or external pieces swapped out."""

import tempfile

from .dev import *


CHANNEL_LAYERS = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
}

# A developer's DJANGO_DEVELOPMENT_LATENCY would add minutes to the suite.
MIDDLEWARE = [entry for entry in MIDDLEWARE if "DevelopmentLatencyMiddleware" not in entry]

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "taskflow-test",
    },
}

# Off so unrelated tests are not rate limited. @override_settings cannot turn
# it back on: DRF copies these onto SimpleRateThrottle.THROTTLE_RATES at import
# time. See with_throttle_rates in apps/accounts/tests/test_auth_api.py.
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {
    "anon": None,
    "user": None,
    "login": None,
    "register": None,
    "invitation": None,
}

# Local disk whatever the environment says, so a developer's
# AWS_STORAGE_BUCKET_NAME cannot point the suite at a real bucket. The S3
# branch is covered by apps/attachments/tests/test_storage_backends.py.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Safety net for tests that do not override MEDIA_ROOT themselves.
MEDIA_ROOT = tempfile.mkdtemp(prefix="taskflow-test-media-")

LOGGING["root"]["level"] = "CRITICAL"
LOGGING["loggers"]["django.request"]["level"] = "CRITICAL"
