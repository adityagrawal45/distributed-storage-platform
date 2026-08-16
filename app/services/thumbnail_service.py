"""
Thumbnail generation (Phase 8).

Design decisions
----------------

**Explicit allow-list, checked BEFORE any decode is attempted.** The
supported set is `{image/jpeg, image/png, image/webp, image/gif}`
(`Settings.THUMBNAIL_SUPPORTED_CONTENT_TYPES`). Anything else raises
`NonRetryableEventError` immediately — Pillow is never handed the bytes.
This is not defensive politeness; it is the security boundary. Image
decoders are the classic memory-corruption attack surface, and the input
here is a file an arbitrary user uploaded with a content type they chose.
"Try to decode it and see what happens" would mean every format Pillow's
build happens to support (including ones with a history of CVEs) is
reachable by anyone with an account, and a decoder crash would take the
worker process down rather than failing one message. The allow-list means
an unsupported type is a clean, logged, ACKed permanent failure and the
worker keeps running.

`Image.open` is additionally told the expected format via `formats=`, so
even within the allow-list a file that lies about its type is rejected by
Pillow rather than silently sniffed into a different decoder.

**Aspect ratio is preserved via `thumbnail()`, not `resize()`.**
`Image.thumbnail` fits the image inside a bounding box and never
upscales — a 100px avatar stays 100px rather than being blown up to 512
and looking worse than the original while costing more storage.

**Output is always PNG**, regardless of input format. One output format
means one content type to serve, one decoder for any client, no
transparency-loss surprises converting PNG/GIF to JPEG, and no
"thumbnail of a WebP is a WebP that this browser cannot render" support
tickets. The size cost is irrelevant at 512px.

**Everything CPU-bound runs in a thread.** Decoding and re-encoding an
image is genuinely CPU-heavy and completely blocking; running it inline
on the worker's event loop would stall every other in-flight message on
that replica. `asyncio.to_thread` is the same technique `StorageService`
uses for the sync GCS client.

**Animated GIFs produce a thumbnail of the first frame.** Pillow gives us
frame 0 by default and that is the correct, cheap choice: an animated
thumbnail is a video-transcoding problem, not an image-resizing one.
"""

import asyncio
import hashlib
import io
import uuid
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from app.core.config import get_settings
from app.exceptions.custom_exceptions import NonRetryableEventError, RetryableEventError
from app.logging.logger import get_logger
from app.services.storage_service import StorageService

logger = get_logger(__name__)

#: Content type -> the Pillow format name(s) `Image.open(formats=...)` may
#: use for it. Pinning this stops a file that lies about its type from
#: being sniffed into an unrelated (possibly unaudited) decoder.
_CONTENT_TYPE_TO_PIL_FORMATS: dict[str, tuple[str, ...]] = {
    "image/jpeg": ("JPEG",),
    "image/png": ("PNG",),
    "image/webp": ("WEBP",),
    "image/gif": ("GIF",),
}

THUMBNAIL_CONTENT_TYPE = "image/png"


@dataclass(frozen=True)
class ThumbnailResult:
    object_name: str
    width: int
    height: int
    size_bytes: int
    content_type: str = THUMBNAIL_CONTENT_TYPE


class ThumbnailService:
    def __init__(self, storage_service: StorageService, *, max_dimension: int | None = None):
        settings = get_settings()
        self._storage = storage_service
        self._settings = settings
        self._max_dimension = max_dimension or settings.THUMBNAIL_MAX_DIMENSION_PX
        self._supported = set(settings.THUMBNAIL_SUPPORTED_CONTENT_TYPES)
        self._prefix = settings.THUMBNAIL_OBJECT_PREFIX

    # ------------------------------------------------------------------
    # Guard
    # ------------------------------------------------------------------
    def assert_supported(self, content_type: str | None) -> str:
        """
        Allow-list check. Raises `NonRetryableEventError` — a file whose
        type this build does not support will not become supported by
        being redelivered, so ACK-and-record is the right outcome, never
        NACK-and-retry and never the DLQ.
        """
        normalized = (content_type or "").lower().strip()
        if normalized not in self._supported:
            raise NonRetryableEventError(
                f"Unsupported content type for thumbnailing: {content_type!r}. "
                f"Supported: {sorted(self._supported)}."
            )
        return normalized

    def thumbnail_object_name(self, file_id: uuid.UUID | str) -> str:
        """
        `thumbnails/{file_id}.png` — deterministic, so regenerating
        overwrites in place rather than accumulating orphans, and so a
        duplicate delivery that slipped past deduplication is still
        harmless.
        """
        return f"{self._prefix}/{file_id}.png"

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------
    def _render(self, data: bytes, content_type: str) -> tuple[bytes, int, int]:
        """Pure CPU work — always called via `asyncio.to_thread`."""
        formats = _CONTENT_TYPE_TO_PIL_FORMATS.get(content_type)
        with Image.open(io.BytesIO(data), formats=formats) as image:
            # Normalize to a mode PNG can always write. GIF/PNG palettes
            # and JPEG CMYK all land safely in RGBA.
            converted = image.convert("RGBA")
            converted.thumbnail((self._max_dimension, self._max_dimension))
            buffer = io.BytesIO()
            converted.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue(), converted.width, converted.height

    async def generate(
        self, *, file_id: uuid.UUID | str, object_name: str, content_type: str | None
    ) -> ThumbnailResult:
        """
        Downloads the source object, renders a thumbnail and uploads it.

        Raises `NonRetryableEventError` for anything about the *content*
        (unsupported type, undecodable bytes) and `RetryableEventError`
        for anything about the *infrastructure* (storage read/write).
        That split is what decides ACK vs NACK upstream, so it is made
        here rather than left to the caller to guess.
        """
        normalized_type = self.assert_supported(content_type)

        # Size first, then one bounded range read. `StorageService`
        # exposes ranged reads (Phase 3's HTTP Range support) rather than
        # a "download everything" call, and going through the metadata
        # HEAD also gives an early, cheap failure for a missing object.
        try:
            blob = await self._storage.get_blob_metadata(object_name)
            source_size = int(getattr(blob, "size", 0) or 0)
            source_bytes = (
                await self._storage.download_range(object_name, 0, source_size - 1) if source_size else b""
            )
        except Exception as exc:  # noqa: BLE001
            raise RetryableEventError(f"Could not read source object '{object_name}': {exc}") from exc

        if not source_bytes:
            raise NonRetryableEventError(f"Source object '{object_name}' is empty; nothing to thumbnail.")

        try:
            png_bytes, width, height = await asyncio.to_thread(self._render, source_bytes, normalized_type)
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            # Corrupt/truncated/mislabelled bytes. Permanent: the same
            # bytes will fail identically on every redelivery.
            raise NonRetryableEventError(
                f"Could not decode '{object_name}' as {normalized_type}: {exc}"
            ) from exc

        thumbnail_name = self.thumbnail_object_name(file_id)
        try:
            await self._storage.upload(
                thumbnail_name,
                io.BytesIO(png_bytes),
                content_type=THUMBNAIL_CONTENT_TYPE,
                checksum_sha256=hashlib.sha256(png_bytes).hexdigest(),
                size=len(png_bytes),
            )
        except Exception as exc:  # noqa: BLE001
            raise RetryableEventError(f"Could not write thumbnail '{thumbnail_name}': {exc}") from exc

        logger.info(
            "thumbnail_generated",
            file_id=str(file_id),
            source_object=object_name,
            thumbnail_object=thumbnail_name,
            width=width,
            height=height,
            size_bytes=len(png_bytes),
        )
        return ThumbnailResult(
            object_name=thumbnail_name, width=width, height=height, size_bytes=len(png_bytes)
        )
