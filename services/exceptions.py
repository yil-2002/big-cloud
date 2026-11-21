"""Domain exceptions shared by services, bot handlers and API routes.

Keeping them in one module means the API can translate any service error into a
proper HTTP status with a single ``exception_handler``, and the bot can render a
friendly Telegram message from the same objects.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AppError",
    "DomainError",
    "DuplicateFileError",
    "FileNotFoundError",
    "FolderNotEmptyError",
    "FolderNotFoundError",
    "InsufficientQuotaError",
    "InvalidPassCodeError",
    "LinkExpiredError",
    "LinkNotFoundError",
    "LinkRevokedError",
    "NotOwnerError",
    "TelegramAPIError",
    "TooLargeError",
    "UserNotFoundError",
]


class AppError(Exception):
    """Base class for every error raised by this project."""

    #: Default HTTP status used by the FastAPI exception handler.
    status_code: int = 500
    #: Machine readable code sent in the JSON body (``{"error": {"code": ...}}``).
    code: str = "app_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details

    def to_dict(self) -> dict[str, Any]:
        """Serialisable representation for API responses and logs."""
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class DomainError(AppError):
    """Business-rule violation (4xx class of errors)."""

    status_code = 400
    code = "domain_error"


class UserNotFoundError(DomainError):
    """Telegram user has never started the bot."""

    status_code = 404
    code = "user_not_found"


class FileNotFoundError(DomainError):
    """No file with that id / token exists."""

    status_code = 404
    code = "file_not_found"


class FolderNotFoundError(DomainError):
    """No folder with that id exists."""

    status_code = 404
    code = "folder_not_found"


class FolderNotEmptyError(DomainError):
    """Folder still contains files or sub-folders."""

    status_code = 409
    code = "folder_not_empty"


class DuplicateFileError(DomainError):
    """The same Telegram file is already stored by this user."""

    status_code = 409
    code = "duplicate_file"


class NotOwnerError(DomainError):
    """Caller is not the owner of the resource."""

    status_code = 403
    code = "not_owner"


class TooLargeError(DomainError):
    """Upload exceeds the configured size limit."""

    status_code = 413
    code = "file_too_large"


class InsufficientQuotaError(DomainError):
    """Storage quota would be exceeded by this upload."""

    status_code = 507
    code = "insufficient_storage"


class LinkNotFoundError(DomainError):
    """Unknown share token."""

    status_code = 404
    code = "link_not_found"


class LinkExpiredError(DomainError):
    """Share link exists but its ``expires_at`` has passed."""

    status_code = 410
    code = "link_expired"


class LinkRevokedError(DomainError):
    """Share link was revoked by its owner."""

    status_code = 410
    code = "link_revoked"


class InvalidPassCodeError(DomainError):
    """Wrong access code for a protected share link."""

    status_code = 403
    code = "invalid_pass_code"


class TelegramAPIError(AppError):
    """Telegram Bot API call failed (bad file_id, flood control, ...)."""

    status_code = 502
    code = "telegram_api_error"
