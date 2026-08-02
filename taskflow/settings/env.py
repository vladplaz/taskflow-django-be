"""Typed environment readers. Importing this module loads ``.env``."""

import os
from pathlib import Path
from urllib.parse import quote

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# override=False so a real environment variable always beats the file.
load_dotenv(PROJECT_ROOT / ".env", override=False)


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ImproperlyConfigured(f"{name} must be a boolean value.")


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise ImproperlyConfigured(f"{name} must be an integer.") from error


def env_list(name: str, default: list[str]) -> list[str]:
    value = os.environ.get(name)
    if value is None:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


def env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def env_str_required(name: str) -> str:
    """Return an environment value, refusing to boot without it."""
    value = os.environ.get(name)
    if not value:
        raise ImproperlyConfigured(f"{name} must be set.")
    return value


_REDIS_SCHEMES = ("redis://", "rediss://", "unix://")


def env_redis_url(name: str = "REDIS_URL", default: str | None = None) -> str:
    """Return a Redis URL, composing one from host and token if none is given.

    An HTTP REST endpoint is refused: the channel layer speaks RESP over a
    socket, and failing at boot beats failing when a WebSocket opens.
    """
    if url := os.environ.get(name):
        if url.startswith(("http://", "https://")):
            raise ImproperlyConfigured(
                f"{name} is an HTTP REST endpoint, which cannot be used for the "
                f"channel layer or the cache. Use the provider's Redis protocol "
                f"URL instead -- it starts with rediss:// and carries the token "
                f"as its password."
            )
        if not url.startswith(_REDIS_SCHEMES):
            raise ImproperlyConfigured(f"{name} must start with redis://, rediss://, or unix://.")
        return url

    host = os.environ.get("REDIS_HOST", "").strip()
    if not host:
        if default is not None:
            return default
        raise ImproperlyConfigured(f"{name} must be set, or REDIS_HOST with REDIS_PASSWORD.")

    host = host.removeprefix("https://").removeprefix("http://").rstrip("/").split("/")[0]
    is_local = host in ("localhost", "127.0.0.1", "::1", "redis")
    scheme = "rediss" if env_bool("REDIS_TLS", not is_local) else "redis"

    password = os.environ.get("REDIS_PASSWORD", "")
    username = os.environ.get("REDIS_USERNAME", "default" if password else "")
    credentials = f"{quote(username, safe='')}:{quote(password, safe='')}@" if password else ""

    return f"{scheme}://{credentials}{host}:{env_int('REDIS_PORT', 6379)}/{env_int('REDIS_DB', 0)}"
