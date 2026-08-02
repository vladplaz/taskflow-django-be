import re

from django.conf import settings
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    HttpResponseRedirect,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tasks.realtime import publish_task_event
from apps.tasks.views import BoardAccessMixin

from .models import Attachment
from .serializers import AttachmentSerializer, AttachmentUploadSerializer
from .services import create_attachment, delete_attachment
from .storage import content_disposition, is_inline_renderable, signed_url


#: Only media asks for byte ranges; see `_stream`.
_SEEKABLE_PREFIXES = ("video/", "audio/")

_RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")

_CHUNK_SIZE = 64 * 1024

#: Bytes never change (a re-upload is a new row). `private` because the
#: response is access-checked and must not land in a shared cache.
_IMMUTABLE_CACHE = "private, max-age=86400, immutable"

#: Boundaries, part headers, and the `placement` field wrapped around the file.
_MULTIPART_OVERHEAD = 16 * 1024


class PayloadTooLarge(APIException):
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    default_detail = "This upload is too large."


def _reject_oversized_body(request) -> None:
    """Answer from Content-Length, before the parser spools the body to disk.

    The serializer's own size check runs too late to prevent that; a reverse
    proxy's `client_max_body_size` is the layer in front of both.
    """
    try:
        length = int(request.META.get("CONTENT_LENGTH"))
    except (TypeError, ValueError):
        return
    if length <= settings.ATTACHMENT_MAX_BYTES + _MULTIPART_OVERHEAD:
        return
    limit_mb = settings.ATTACHMENT_MAX_BYTES / (1024 * 1024)
    raise PayloadTooLarge(
        f"This upload is too large ({length / (1024 * 1024):.1f} MB, limit {limit_mb:.0f} MB)."
    )


VARIANT_PARAMETER = OpenApiParameter(
    "variant",
    str,
    description="Pass `thumb` for the generated preview instead of the original file.",
)
DOWNLOAD_PARAMETER = OpenApiParameter(
    "download",
    bool,
    description="Force a download instead of rendering the file in the browser.",
)
STREAM_PARAMETER = OpenApiParameter(
    "stream",
    bool,
    description=(
        "Return the bytes from this endpoint instead of redirecting to storage. "
        "For callers that read the response with JavaScript."
    ),
)


class AttachmentAccessMixin(BoardAccessMixin):
    """Scopes attachments to the boards the requesting user may reach."""

    def attachment_queryset(self, *, edit: bool):
        return Attachment.objects.filter(
            # An archived task keeps its files readable; an archived list or
            # board hides them, as it hides every other read of that task.
            task__board_list__is_archived=False,
            task__board_list__board__is_archived=False,
            task__board_list__board__workspace_id=self.kwargs["workspace_id"],
            task__board_list__board__in=self.board_queryset(edit=edit),
        )

    def get_serializer_context(self) -> dict:
        return {"request": self.request, "view": self, "workspace_id": self.kwargs["workspace_id"]}


@extend_schema(
    request={"multipart/form-data": AttachmentUploadSerializer},
    responses={201: AttachmentSerializer},
)
class TaskAttachmentUploadView(AttachmentAccessMixin, APIView):
    """Upload one file against a task.

    Separate from saving the task, so editing a description only ever sends
    ids and cannot re-upload what it already references.
    """

    parser_classes = (MultiPartParser, FormParser)

    def post(self, request, workspace_id: int, task_id: int):
        _reject_oversized_body(request)
        task = get_object_or_404(self.task_queryset(edit=True), pk=task_id)
        serializer = AttachmentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        attachment = create_attachment(
            task=task,
            uploader=request.user,
            upload=serializer.validated_data["file"],
            placement=serializer.validated_data["placement"],
        )
        if attachment.placement == Attachment.Placement.TASK:
            publish_task_event(
                task=task,
                event_type="task.attachment_created",
                actor_id=request.user.id,
            )
        return Response(
            AttachmentSerializer(attachment, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )


@extend_schema(
    parameters=[VARIANT_PARAMETER, DOWNLOAD_PARAMETER, STREAM_PARAMETER],
    responses={200: bytes},
)
class AttachmentDetailView(AttachmentAccessMixin, APIView):
    """Serve or delete one attachment.

    The only way to read an attachment's bytes. Storage keys never reach the
    client, so a private board's files cannot be found by guessing a URL.
    """

    def get(self, request, workspace_id: int, pk: int):
        attachment = get_object_or_404(self.attachment_queryset(edit=False), pk=pk)

        wants_thumbnail = request.query_params.get("variant") == "thumb" and attachment.thumbnail
        if wants_thumbnail:
            stored, content_type = attachment.thumbnail, "image/webp"
            filename = f"{attachment.original_name.rsplit('.', 1)[0]}.webp"
        else:
            stored, content_type = attachment.file, attachment.content_type
            filename = attachment.original_name

        # Opt-in per type: an HTML upload served inline would execute in the
        # API's own origin, where it could read the session cookie.
        inline = is_inline_renderable(content_type) and not _flag(request, "download")
        disposition = content_disposition(filename=filename, inline=inline)

        # On S3 the bucket does the transfer; local disk has no URL to sign,
        # so Django streams the file itself.
        redirect_url = (
            None
            if _wants_stream(request)
            else signed_url(
                storage=stored.storage,
                name=stored.name,
                disposition=disposition,
                content_type=content_type,
            )
        )
        if redirect_url is not None:
            response = HttpResponseRedirect(redirect_url)
            # The target expires; a cached redirect would outlive it.
            response["Cache-Control"] = "no-store"
            return _allow_framing_by_the_client(response)

        try:
            response = _stream(request, stored, content_type=content_type, disposition=disposition)
        except FileNotFoundError as error:
            # The row outlived its bytes; a 500 would misreport that as a bug.
            raise Http404("This attachment is no longer available.") from error
        return _allow_framing_by_the_client(response)

    def delete(self, request, workspace_id: int, pk: int):
        attachment = get_object_or_404(
            self.attachment_queryset(edit=True).select_related("comment__activity", "task"),
            pk=pk,
        )
        self._check_may_delete(attachment)
        task = attachment.task
        delete_attachment(attachment=attachment, actor=request.user)
        publish_task_event(
            task=task,
            event_type="task.attachment_deleted",
            actor_id=request.user.id,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _check_may_delete(self, attachment: Attachment) -> None:
        """Strip files are task content, so any board editor may remove one.
        Comment files follow the comment: author only."""
        if attachment.placement != Attachment.Placement.COMMENT:
            return
        author_id = (
            attachment.comment.activity.author_id
            if attachment.comment_id
            else attachment.uploaded_by_id
        )
        if author_id != self.request.user.id:
            raise PermissionDenied("Only the comment author can delete its attachments.")


def _flag(request, name: str) -> bool:
    return request.query_params.get(name, "").lower() in {"1", "true", "yes"}


def _wants_stream(request) -> bool:
    """Whether this caller needs the bytes from here rather than from storage.

    Office and text previews read the response with ``fetch(..., {credentials:
    "include"})``. A redirect keeps that credentials mode, and S3 never answers
    a credentialed cross-origin read with ``Access-Control-Allow-Credentials``,
    which no bucket CORS policy can add. Elements that load a URL themselves
    (``<img>``, ``<video>``, ``<iframe>``) are unaffected and keep the redirect.
    """
    return _flag(request, "stream")


def _allow_framing_by_the_client(response):
    """Let the React client embed this file, and nobody else.

    `XFrameOptionsMiddleware` stamps `DENY` on every response, which blocks the
    PDF preview's `<iframe>`; `SAMEORIGIN` would not help either, since client
    and API are different origins. `frame-ancestors` can express "the client".
    """
    response.xframe_options_exempt = True
    allowed = " ".join(settings.CORS_ALLOWED_ORIGINS)
    response["Content-Security-Policy"] = f"frame-ancestors 'self' {allowed}".strip()
    return response


def _stream(request, stored, *, content_type: str, disposition: str):
    """Send the bytes, honouring a byte range when the client asks for one.

    `FileResponse` sends whole files only, but `<video>` cannot seek without
    range support and Safari refuses to play a source that lacks it.
    """
    response = _partial(request, stored, content_type=content_type)
    if response is None:
        response = FileResponse(stored.open("rb"), content_type=content_type)
        response["Cache-Control"] = _IMMUTABLE_CACHE

    response["Content-Disposition"] = disposition
    # A file whose stored type is wrong still cannot be sniffed into
    # something executable.
    response["X-Content-Type-Options"] = "nosniff"
    if content_type.startswith(_SEEKABLE_PREFIXES):
        response["Accept-Ranges"] = "bytes"
    return response


def _partial(request, stored, *, content_type: str):
    """Build a 206 response, or None when the request is not a usable range."""
    header = request.headers.get("Range", "")
    if not header or not content_type.startswith(_SEEKABLE_PREFIXES):
        return None

    match = _RANGE_PATTERN.match(header.strip())
    if not match:
        # Multi-range and non-byte units: answer with the whole file.
        return None

    size = stored.size
    first, last = match.groups()
    if first:
        start = int(first)
        end = int(last) if last else size - 1
    elif last:
        # `bytes=-500` means the final 500 bytes.
        start, end = max(size - int(last), 0), size - 1
    else:
        return None

    end = min(end, size - 1)
    if start > end or start >= size:
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{size}"
        return response

    response = StreamingHttpResponse(
        _chunks(stored.open("rb"), start=start, length=end - start + 1),
        status=206,
        content_type=content_type,
    )
    response["Content-Range"] = f"bytes {start}-{end}/{size}"
    response["Content-Length"] = str(end - start + 1)
    response["Cache-Control"] = _IMMUTABLE_CACHE
    return response


def _chunks(file, *, start: int, length: int):
    file.seek(start)
    remaining = length
    try:
        while remaining > 0:
            chunk = file.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        file.close()
