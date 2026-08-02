"""Storage policy for uploaded files: keys, signed URLs, safe content types."""

import mimetypes
import re
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.core.files.base import ContentFile


#: Content types the browser may render in place; everything else downloads.
#: Opt-in, because a file rendered inline executes in the API's own origin,
#: where it could read the session cookie it was fetched with.
INLINE_CONTENT_TYPES = frozenset(
    {
        "image/apng",
        "image/avif",
        "image/bmp",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
        "video/mp4",
        "video/ogg",
        "video/quicktime",
        "video/webm",
        "audio/aac",
        "audio/flac",
        "audio/mp4",
        "audio/mpeg",
        "audio/ogg",
        "audio/wav",
        "audio/webm",
        "audio/x-wav",
        "application/pdf",
        "text/plain",
    }
)

#: Raster formats a thumbnail is generated for. ``image/svg+xml`` is absent
#: here and from INLINE_CONTENT_TYPES: an SVG can carry script.
THUMBNAILABLE_CONTENT_TYPES = frozenset(
    {
        "image/apng",
        "image/avif",
        "image/bmp",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)

FALLBACK_CONTENT_TYPE = "application/octet-stream"

_CONTENT_TYPE_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
)

_EXTENSION_PATTERN = re.compile(r"^\.[A-Za-z0-9]{1,12}$")


def _key(instance, filename: str, *, folder: str) -> str:
    """Build the storage key for one file.

    The client's filename never reaches the path -- it could contain
    separators that walk out of the upload directory. Only a sanitized
    extension survives; the display name lives in ``Attachment.original_name``.
    """
    extension = Path(filename).suffix
    if not _EXTENSION_PATTERN.match(extension):
        extension = ""
    return f"attachments/{folder}/{instance.task_id}/{uuid.uuid4().hex}{extension.lower()}"


def attachment_upload_to(instance, filename: str) -> str:
    return _key(instance, filename, folder="files")


def thumbnail_upload_to(instance, filename: str) -> str:
    return _key(instance, filename, folder="thumbnails")


def resolve_content_type(*, declared: str | None, filename: str) -> str:
    """Settle on the content type to store for an upload.

    The declared value is attacker-controlled and is later echoed in a
    ``Content-Type`` header, so anything unparseable is discarded and the
    filename's own guess is used instead.
    """
    candidate = (declared or "").split(";")[0].strip().lower()
    if not _CONTENT_TYPE_PATTERN.match(candidate) or candidate == FALLBACK_CONTENT_TYPE:
        guessed, _ = mimetypes.guess_type(filename)
        candidate = (guessed or FALLBACK_CONTENT_TYPE).lower()
    return candidate


def is_inline_renderable(content_type: str) -> bool:
    return content_type in INLINE_CONTENT_TYPES


def content_disposition(*, filename: str, inline: bool) -> str:
    """Build the header that names a download and decides whether it renders.

    Two spellings per RFC 6266. Quotes and control characters are stripped from
    the ASCII form so a crafted name cannot inject a second header parameter.
    """
    ascii_name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode()
    ascii_name = re.sub(r'[\x00-\x1f"\\]', "", ascii_name).strip() or "download"
    quoted = quote(filename, safe="")
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


def build_thumbnail(*, upload, content_type: str) -> ContentFile | None:
    """Derive a small WebP preview, or None when one cannot be made cheaply.

    Corrupt, truncated, and oversized images all upload without a preview
    rather than failing the request.
    """
    if content_type not in THUMBNAILABLE_CONTENT_TYPES:
        return None

    # Lazy: the migration referencing the upload_to callables imports this
    # module and must not need Pillow.
    from PIL import Image, UnidentifiedImageError

    upload.seek(0)
    try:
        with Image.open(upload) as image:
            # From the header, before any pixel is decoded: Pillow's own guard
            # only raises past twice its limit, and warns under that.
            if image.width * image.height > settings.ATTACHMENT_THUMBNAIL_MAX_PIXELS:
                return None
            image.load()
            # WebP cannot encode a palette or a 16-bit channel.
            frame = image.convert("RGBA" if image.mode in ("RGBA", "LA", "PA") else "RGB")
            frame.thumbnail(
                (settings.ATTACHMENT_THUMBNAIL_MAX_PX, settings.ATTACHMENT_THUMBNAIL_MAX_PX),
                Image.Resampling.LANCZOS,
            )
            buffer = ContentFile(b"")
            frame.save(buffer, format="WEBP", quality=settings.ATTACHMENT_THUMBNAIL_QUALITY)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        return None
    finally:
        upload.seek(0)

    buffer.seek(0)
    buffer.name = "thumbnail.webp"
    return buffer


def signed_url(*, storage, name: str, disposition: str, content_type: str) -> str | None:
    """A time-limited URL straight to the backend, or None if it has none.

    ``FileSystemStorage.url`` takes no such arguments, and the resulting
    ``TypeError`` is how the caller learns to stream the file itself.
    """
    try:
        return storage.url(
            name,
            parameters={
                "ResponseContentDisposition": disposition,
                "ResponseContentType": content_type,
            },
            expire=settings.ATTACHMENT_URL_TTL_SECONDS,
        )
    except TypeError:
        return None
