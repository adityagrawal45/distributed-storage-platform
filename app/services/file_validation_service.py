"""
Upload-time file validation.

Design decision: every check here runs BEFORE any bytes are sent to
Google Cloud Storage. Validating after upload would mean paying the
network cost of transferring bytes we're about to reject and would
require an extra rollback/delete step — validating first means a
rejected upload never creates a storage object to begin with.

MIME detection deliberately trusts file *content* (magic bytes, via
`filetype`) over the client-supplied `Content-Type` header and over the
filename extension: both of those are trivially spoofable by a malicious
or buggy client (e.g. renaming `payload.exe` to `photo.jpg` and setting
`Content-Type: image/jpeg`). Content sniffing is the only one of the
three signals an uploader can't directly control.
"""

import filetype

from app.core.config import Settings
from app.exceptions.custom_exceptions import FileTooLargeException, UnsupportedFileTypeException, ValidationException
from app.schemas.folder import _validate_folder_or_file_name

DEFAULT_MIME_TYPE = "application/octet-stream"
_SNIFF_BYTES = 8192  # magic-byte detectors need only the first few KB


class FileValidationService:
    def __init__(self, settings: Settings):
        self._settings = settings

    def validate_filename(self, filename: str) -> str:
        try:
            return _validate_folder_or_file_name(filename)
        except ValueError as exc:
            raise ValidationException(str(exc)) from exc

    def validate_size(self, size: int) -> None:
        if size <= 0:
            raise ValidationException("File is empty.")
        if size > self._settings.MAX_UPLOAD_SIZE_BYTES:
            raise FileTooLargeException(
                f"File exceeds the maximum allowed size of {self._settings.MAX_UPLOAD_SIZE_MB} MB."
            )

    def validate_extension(self, extension: str | None) -> None:
        if extension and extension.lower() in self._settings.BLOCKED_EXTENSIONS:
            raise UnsupportedFileTypeException(f"Files with extension '.{extension}' are not allowed.")

    def detect_and_validate_mime_type(self, head_bytes: bytes, declared_content_type: str | None) -> str:
        """Sniffs the real MIME type from content and enforces the allowlist, if configured."""
        kind = filetype.guess(head_bytes)
        detected = kind.mime if kind is not None else (declared_content_type or DEFAULT_MIME_TYPE)

        allowed = self._settings.ALLOWED_MIME_TYPES
        if allowed and detected not in allowed:
            raise UnsupportedFileTypeException(f"MIME type '{detected}' is not permitted.")

        return detected

    def validate_upload(
        self, *, filename: str, size: int, head_bytes: bytes, declared_content_type: str | None
    ) -> tuple[str, str]:
        """Runs every check and returns (validated_filename, detected_mime_type)."""
        validated_filename = self.validate_filename(filename)
        self.validate_size(size)

        extension = validated_filename.rsplit(".", 1)[-1].lower() if "." in validated_filename else None
        self.validate_extension(extension)

        mime_type = self.detect_and_validate_mime_type(head_bytes, declared_content_type)
        return validated_filename, mime_type
