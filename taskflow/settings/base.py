"""Settings shared by every environment; dev/prod/test layer policy on top."""

from pathlib import Path

from .env import env_int, env_list, env_str


BASE_DIR = Path(__file__).resolve().parent.parent.parent

# --- Applications ---

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "apps.accounts",
    "apps.workspaces",
    "apps.boards",
    "apps.tasks",
    "apps.attachments",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "taskflow.urls"
WSGI_APPLICATION = "taskflow.wsgi.application"
ASGI_APPLICATION = "taskflow.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "taskflow" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --- Data ---

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env_str("POSTGRES_DB", "taskflow"),
        "USER": env_str("POSTGRES_USER", "taskflow"),
        "PASSWORD": env_str("POSTGRES_PASSWORD", ""),
        "HOST": env_str("POSTGRES_HOST", "localhost"),
        # Postgres' own port, not the Compose mapping: a deployment that
        # forgets this should reach a real server.
        "PORT": env_str("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": env_int("POSTGRES_CONN_MAX_AGE", 60),
        "CONN_HEALTH_CHECKS": True,
        # Explicit: libpq's default falls back to plaintext. prod.py requires.
        "OPTIONS": {"sslmode": env_str("POSTGRES_SSLMODE", "prefer")},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

# --- Authentication ---

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = env_str("DJANGO_SESSION_COOKIE_SAMESITE", "Lax")
CSRF_COOKIE_SAMESITE = env_str("DJANGO_CSRF_COOKIE_SAMESITE", "Lax")

# The client reads this cookie from JavaScript, so an API on api.example.com
# has to widen it to .example.com for a client on app.example.com. The session
# cookie stays host-only: nothing outside the API needs it.
CSRF_COOKIE_DOMAIN = env_str("DJANGO_CSRF_COOKIE_DOMAIN", "") or None

# --- Cross-origin access for the React client ---

CORS_ALLOWED_ORIGINS = env_list("DJANGO_CORS_ALLOWED_ORIGINS", ["http://localhost:5173"])
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", CORS_ALLOWED_ORIGINS)
FRONTEND_BASE_URL = env_str("FRONTEND_BASE_URL", "http://localhost:5173")

# --- REST framework ---

REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "taskflow.exceptions.normalized_exception_handler",
    "DEFAULT_AUTHENTICATION_CLASSES": ("rest_framework.authentication.SessionAuthentication",),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_PAGINATION_CLASS": "taskflow.pagination.DefaultPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        # Ceilings for every endpoint; the scopes below are the tight ones.
        "anon": env_str("DJANGO_THROTTLE_ANON", "60/min"),
        "user": env_str("DJANGO_THROTTLE_USER", "2000/hour"),
        "login": env_str("DJANGO_THROTTLE_LOGIN", "10/min"),
        "register": env_str("DJANGO_THROTTLE_REGISTER", "20/hour"),
        "invitation": env_str("DJANGO_THROTTLE_INVITATION", "60/hour"),
    },
    # Anonymous throttles key on the client IP, which is read from the left of
    # X-Forwarded-For -- a header the client writes. Set this to the number of
    # proxies actually in front of Django, or the rate limit is bypassable.
    "NUM_PROXIES": env_int("DJANGO_NUM_PROXIES", 0) or None,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "TaskFlow API",
    "DESCRIPTION": "Workspaces, boards, lists, and tasks for the TaskFlow client.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1",
    "COMPONENT_SPLIT_REQUEST": True,
    # Three models expose a "role" field with different choice sets.
    "ENUM_NAME_OVERRIDES": {
        "BoardRoleEnum": "apps.boards.models.BOARD_ROLE_CHOICES",
        "BoardInvitableRoleEnum": "apps.boards.models.BOARD_INVITABLE_ROLE_CHOICES",
        "WorkspaceRoleEnum": "apps.workspaces.models.WORKSPACE_ROLE_CHOICES",
        "WorkspaceAssignableRoleEnum": ("apps.workspaces.models.WORKSPACE_ASSIGNABLE_ROLE_CHOICES"),
    },
}

# --- Domain policy ---

INVITATION_EXPIRY_DAYS = env_int("INVITATION_EXPIRY_DAYS", 14)

# Bytes of serialized JSON.
TASK_DESCRIPTION_MAX_BYTES = env_int("TASK_DESCRIPTION_MAX_BYTES", 100_000)

# A reverse proxy enforces its own ceiling (nginx defaults to 1 MB); raise
# `client_max_body_size` to match or uploads fail before Django sees them.
ATTACHMENT_MAX_BYTES = env_int("ATTACHMENT_MAX_BYTES", 25 * 1024 * 1024)

# Longest edge of the image preview, and its WebP quality.
ATTACHMENT_THUMBNAIL_MAX_PX = env_int("ATTACHMENT_THUMBNAIL_MAX_PX", 400)
ATTACHMENT_THUMBNAIL_QUALITY = env_int("ATTACHMENT_THUMBNAIL_QUALITY", 80)

# Past this an upload is stored without a preview rather than decoded.
ATTACHMENT_THUMBNAIL_MAX_PIXELS = env_int("ATTACHMENT_THUMBNAIL_MAX_PIXELS", 50_000_000)

# Presigned URL lifetime. Unused by local disk, which streams through Django.
ATTACHMENT_URL_TTL_SECONDS = env_int("ATTACHMENT_URL_TTL_SECONDS", 300)

# How long an upload may sit unattached before `cleanup_attachments` reaps it.
ATTACHMENT_UNCLAIMED_MAX_AGE_HOURS = env_int("ATTACHMENT_UNCLAIMED_MAX_AGE_HOURS", 6)

# Concurrent board sockets one account may hold; 0 disables the cap.
WEBSOCKET_MAX_SOCKETS_PER_USER = env_int("DJANGO_WEBSOCKET_MAX_SOCKETS_PER_USER", 20)
WEBSOCKET_SOCKET_COUNT_TTL_SECONDS = env_int("DJANGO_WEBSOCKET_SOCKET_COUNT_TTL", 3600)

DEFAULT_FROM_EMAIL = env_str("DJANGO_DEFAULT_FROM_EMAIL", "no-reply@taskflow.local")

# --- Internationalization and static files ---

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# --- File storage ---

# Local disk unless a bucket is named. Nothing outside this setting knows the
# difference: attachments are always read through an access-checked endpoint.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

AWS_STORAGE_BUCKET_NAME = env_str("AWS_STORAGE_BUCKET_NAME", "")

if AWS_STORAGE_BUCKET_NAME:
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": AWS_STORAGE_BUCKET_NAME,
            "region_name": env_str("AWS_S3_REGION_NAME", ""),
            # Presigned URLs; off would publish every attachment.
            "querystring_auth": True,
            "querystring_expire": ATTACHMENT_URL_TTL_SECONDS,
            "file_overwrite": False,
            "default_acl": None,
            "endpoint_url": env_str("AWS_S3_ENDPOINT_URL", "") or None,
            # Pinned: boto3 still picks SigV2 for the us-east-1 legacy
            # endpoint, and SigV2 presigned URLs are rejected post-2014 regions.
            "signature_version": "s3v4",
            # Emulators need "path"; virtual-hosted signs `bucket.localhost`,
            # which most resolvers do not answer.
            "addressing_style": env_str("AWS_S3_ADDRESSING_STYLE", "") or None,
        },
    }

MEDIA_ROOT = BASE_DIR / env_str("DJANGO_MEDIA_ROOT", "media")

# Deliberately empty: attachments are private, so nothing under MEDIA_ROOT is
# ever published by URL.
MEDIA_URL = ""

# --- Logging ---

LOG_LEVEL = env_str("DJANGO_LOG_LEVEL", "INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        # Without this, unhandled view exceptions are swallowed when DEBUG is off.
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
    },
}
