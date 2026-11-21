"""ORM models for the Telegram Cloud Drive project.

Importing this package registers every table on ``db.database.Base.metadata``:
``users``, ``folders``, ``files``, ``shared_links``, ``audit_logs`` and
``system_flags``.
"""

from db.models.audit import AuditAction, AuditLog
from db.models.base import TimestampMixin
from db.models.file import File, FileCategory, Visibility
from db.models.flag import (
    FLAG_ALLOW_REGISTER,
    FLAG_AUTH_REQUIRED,
    SystemFlag,
    get_flag,
    load_flags,
    set_flag,
)
from db.models.folder import Folder
from db.models.password import hash_password, verify_password
from db.models.shared_link import SharedLink
from db.models.user import User, UserSettings

#: The four display groups used by ``/files`` (🎬 🎵 📄 📦) plus the legacy
#: APK/IPA tab. Keys are what the bot/API accept, values the ORM categories.
FILE_GROUPS: dict[str, tuple[FileCategory, ...]] = {
    "media": (FileCategory.PHOTO, FileCategory.VIDEO, FileCategory.ANIMATION),
    "audio": (FileCategory.AUDIO, FileCategory.VOICE),
    "document": (FileCategory.DOCUMENT, FileCategory.OTHER),
    "archive": (FileCategory.ARCHIVE,),
    "apps": (FileCategory.APK, FileCategory.IPA),
}

__all__ = [
    "FILE_GROUPS",
    "FLAG_ALLOW_REGISTER",
    "FLAG_AUTH_REQUIRED",
    "AuditAction",
    "AuditLog",
    "File",
    "FileCategory",
    "Folder",
    "SharedLink",
    "SystemFlag",
    "TimestampMixin",
    "User",
    "UserSettings",
    "Visibility",
    "get_flag",
    "hash_password",
    "load_flags",
    "set_flag",
    "verify_password",
]
