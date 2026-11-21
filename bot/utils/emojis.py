"""Emoji registry with optional Telegram Premium *custom emoji* support.

Two problems this module solves:

1. **Consistency** - every screen uses the same pictograms, defined once.
2. **Custom emoji** - Telegram lets bots embed animated custom emoji in messages
   via ``<tg-emoji emoji-id="...">🙂</tg-emoji>`` (HTML parse mode). Those ids are
   tied to a specific bot/account, so they live in configuration, not in code.

Usage
-----
    from bot.utils.emojis import Emoji

    text = f"{Emoji.CLOUD} Your cloud storage"
    # or, when custom emoji ids are configured:
    use_custom_emoji(True)
    text = f"{Emoji.CLOUD} Your cloud storage"   # -> <tg-emoji ...>☁️</tg-emoji>

Custom emoji only render for **premium** users, so :func:`resolve_emoji` is
typically called with the viewer's premium flag::

    Emoji.get("cloud", premium=user.is_premium)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger(__name__)

__all__ = [
    "CUSTOM_EMOJI_ENV_PREFIX",
    "Emoji",
    "custom_emoji_enabled",
    "load_custom_emoji_from_env",
    "resolve_emoji",
    "use_custom_emoji",
]

#: Environment prefix used to inject custom emoji ids without code changes,
#: e.g. ``TG_EMOJI_CLOUD=5341523706953564430``.
CUSTOM_EMOJI_ENV_PREFIX: Final[str] = "TG_EMOJI_"

#: ``True`` when custom emoji markup should be emitted globally.
_custom_emoji_enabled: bool = False


@dataclass(frozen=True, slots=True)
class EmojiSpec:
    """One pictogram: its Unicode fallback plus an optional custom emoji id."""

    name: str
    unicode: str
    custom_id: str | None = None

    def render(self, *, premium: bool = True, use_custom: bool | None = None) -> str:
        """Return HTML for this emoji.

        ``<tg-emoji>`` tags are emitted only when the viewer is premium *and* a
        custom id is known; otherwise the plain Unicode character is returned so
        the message still looks right for everyone else.
        """
        enabled = _custom_emoji_enabled if use_custom is None else use_custom
        if enabled and self.custom_id and premium:
            return f'<tg-emoji emoji-id="{self.custom_id}">{self.unicode}</tg-emoji>'
        return self.unicode

    def __str__(self) -> str:
        """f-string friendly: ``f"{Emoji.CLOUD} storage"`` renders the emoji."""
        return self.render()

    def __html__(self) -> str:
        """Alias used by some template engines."""
        return self.render()

    def plain(self) -> str:
        """Unicode character without any markup (callback data, logs)."""
        return self.unicode


class Emoji:
    """Namespace of every emoji used by the bot.

    Attributes are :class:`EmojiSpec` objects, but ``str(Emoji.CLOUD)`` and
    f-strings render the emoji itself (``__str__`` is overridden), so templates
    stay readable::

        f"{Emoji.UPLOAD} Upload a file"
    """

    # --- brand / navigation ---------------------------------------------------
    CLOUD: Final[EmojiSpec] = EmojiSpec("cloud", "☁️")
    HOME: Final[EmojiSpec] = EmojiSpec("home", "🏠")
    BACK: Final[EmojiSpec] = EmojiSpec("back", "◀️")
    NEXT: Final[EmojiSpec] = EmojiSpec("next", "▶️")
    UP: Final[EmojiSpec] = EmojiSpec("up", "⬆️")
    REFRESH: Final[EmojiSpec] = EmojiSpec("refresh", "🔄")
    CLOSE: Final[EmojiSpec] = EmojiSpec("close", "❌")
    CHECK: Final[EmojiSpec] = EmojiSpec("check", "✅")
    CROSS: Final[EmojiSpec] = EmojiSpec("cross", "❎")
    WARNING: Final[EmojiSpec] = EmojiSpec("warning", "⚠️")
    ERROR: Final[EmojiSpec] = EmojiSpec("error", "🚫")
    INFO: Final[EmojiSpec] = EmojiSpec("info", "ℹ️")
    ROBOT: Final[EmojiSpec] = EmojiSpec("robot", "🤖")
    STAR: Final[EmojiSpec] = EmojiSpec("star", "⭐")
    FIRE: Final[EmojiSpec] = EmojiSpec("fire", "🔥")
    SPARKLES: Final[EmojiSpec] = EmojiSpec("sparkles", "✨")

    # --- files & folders ------------------------------------------------------
    FOLDER: Final[EmojiSpec] = EmojiSpec("folder", "📁")
    FOLDER_OPEN: Final[EmojiSpec] = EmojiSpec("folder_open", "📂")
    NEW_FOLDER: Final[EmojiSpec] = EmojiSpec("new_folder", "🗂️")
    FILE: Final[EmojiSpec] = EmojiSpec("file", "📄")
    DOCUMENT: Final[EmojiSpec] = EmojiSpec("document", "📑")
    PHOTO: Final[EmojiSpec] = EmojiSpec("photo", "🖼️")
    VIDEO: Final[EmojiSpec] = EmojiSpec("video", "🎬")
    AUDIO: Final[EmojiSpec] = EmojiSpec("audio", "🎵")
    VOICE: Final[EmojiSpec] = EmojiSpec("voice", "🎙️")
    ANIMATION: Final[EmojiSpec] = EmojiSpec("animation", "🎞️")
    ARCHIVE: Final[EmojiSpec] = EmojiSpec("archive", "🗜️")
    #: Android package (legacy bot used 🤖 for the "APK/IPA" tab).
    APK: Final[EmojiSpec] = EmojiSpec("apk", "🤖")
    #: iOS package (legacy bot used 🍎).
    IPA: Final[EmojiSpec] = EmojiSpec("ipa", "🍎")
    STICKER: Final[EmojiSpec] = EmojiSpec("sticker", "🃏")
    OTHER: Final[EmojiSpec] = EmojiSpec("other", "📦")

    # --- actions --------------------------------------------------------------
    UPLOAD: Final[EmojiSpec] = EmojiSpec("upload", "📤")
    DOWNLOAD: Final[EmojiSpec] = EmojiSpec("download", "📥")
    SEND: Final[EmojiSpec] = EmojiSpec("send", "🚀")
    DELETE: Final[EmojiSpec] = EmojiSpec("delete", "🗑️")
    RENAME: Final[EmojiSpec] = EmojiSpec("rename", "✏️")
    MOVE: Final[EmojiSpec] = EmojiSpec("move", "📍")
    COPY: Final[EmojiSpec] = EmojiSpec("copy", "📋")
    SEARCH: Final[EmojiSpec] = EmojiSpec("search", "🔍")
    SHARE: Final[EmojiSpec] = EmojiSpec("share", "🔗")
    LINK: Final[EmojiSpec] = EmojiSpec("link", "🔗")
    LOCK: Final[EmojiSpec] = EmojiSpec("lock", "🔒")
    UNLOCK: Final[EmojiSpec] = EmojiSpec("unlock", "🔓")
    KEY: Final[EmojiSpec] = EmojiSpec("key", "🔑")
    EYE: Final[EmojiSpec] = EmojiSpec("eye", "👁️")
    PLAY: Final[EmojiSpec] = EmojiSpec("play", "▶️")
    PAUSE: Final[EmojiSpec] = EmojiSpec("pause", "⏸️")
    STOP: Final[EmojiSpec] = EmojiSpec("stop", "⏹️")
    SETTINGS: Final[EmojiSpec] = EmojiSpec("settings", "⚙️")
    WEB_APP: Final[EmojiSpec] = EmojiSpec("web_app", "🌐")
    INLINE: Final[EmojiSpec] = EmojiSpec("inline", "💬")
    STATS: Final[EmojiSpec] = EmojiSpec("stats", "📊")
    CLOCK: Final[EmojiSpec] = EmojiSpec("clock", "⏳")
    HOURGLASS: Final[EmojiSpec] = EmojiSpec("hourglass", "⌛")
    MONEY: Final[EmojiSpec] = EmojiSpec("money", "💎")
    PIN: Final[EmojiSpec] = EmojiSpec("pin", "📌")
    BELL: Final[EmojiSpec] = EmojiSpec("bell", "🔔")

    # --- storage states -------------------------------------------------------
    STORAGE_FULL: Final[EmojiSpec] = EmojiSpec("storage_full", "🟥")
    STORAGE_OK: Final[EmojiSpec] = EmojiSpec("storage_ok", "🟩")
    STORAGE_WARN: Final[EmojiSpec] = EmojiSpec("storage_warn", "🟨")
    EMPTY: Final[EmojiSpec] = EmojiSpec("empty", "🫙")

    # --- hugecloud: admin panel, sorting, file groups ------------------------
    #: 🛡️ Private archive vault / admin shield.
    SHIELD: Final[EmojiSpec] = EmojiSpec("shield", "🛡️")
    #: 🔃 Sorting menu.
    SORT: Final[EmojiSpec] = EmojiSpec("sort", "🔃")
    #: 🗃️ "Your files" menu header.
    FILES: Final[EmojiSpec] = EmojiSpec("files", "🗃️")
    #: 👥 User management.
    USERS: Final[EmojiSpec] = EmojiSpec("users", "👥")
    #: 👤 Single user card.
    USER: Final[EmojiSpec] = EmojiSpec("user", "👤")
    #: 📢 Broadcast.
    BROADCAST: Final[EmojiSpec] = EmojiSpec("broadcast", "📢")
    #: 📜 Audit log.
    AUDIT: Final[EmojiSpec] = EmojiSpec("audit", "📜")
    #: ♾️ Unlimited quota.
    UNLIMITED: Final[EmojiSpec] = EmojiSpec("unlimited", "♾️")
    #: 🔁 De-duplication (instant link of an existing file).
    DEDUPE: Final[EmojiSpec] = EmojiSpec("dedupe", "🔁")

    # --- registry helpers -----------------------------------------------------
    @classmethod
    def all_specs(cls) -> dict[str, EmojiSpec]:
        """Every registered emoji by name (used by env loading and tests)."""
        return {
            str(name).lower(): value
            for name, value in vars(cls).items()
            if isinstance(value, EmojiSpec)
        }

    @classmethod
    def get(cls, name: str, *, premium: bool = True, fallback: str = "") -> str:
        """Render one emoji by name; unknown names fall back to ``fallback``."""
        spec = cls.all_specs().get(name.lower())
        if spec is None:
            return fallback
        return spec.render(premium=premium)

    #: ``/files`` display groups -> emoji (🎬 🎵 📄 📦 🤖 📋 📌).
    _GROUP_EMOJI: Final[dict[str, EmojiSpec]] = {
        "media": VIDEO,
        "audio": AUDIO,
        "document": FILE,
        "archive": ARCHIVE,
        "apps": APK,
        "all": FILES,
        "pinned": PIN,
    }

    @classmethod
    def for_group(cls, group: str) -> EmojiSpec:
        """Emoji of a ``/files`` display group (falls back to :attr:`FILES`)."""
        return cls._GROUP_EMOJI.get(str(group).lower(), cls.FILES)

    @classmethod
    def for_category(cls, category: object, *, premium: bool = True) -> str:
        """Emoji matching a :class:`~db.models.file.FileCategory` value."""
        mapping = {
            "photo": cls.PHOTO,
            "video": cls.VIDEO,
            "audio": cls.AUDIO,
            "voice": cls.VOICE,
            "animation": cls.ANIMATION,
            "document": cls.DOCUMENT,
            "archive": cls.ARCHIVE,
            "apk": cls.APK,
            "ipa": cls.IPA,
            "other": cls.OTHER,
        }
        key = getattr(category, "value", str(category)).lower()
        spec = mapping.get(key, cls.FILE)
        return spec.render(premium=premium)

    @classmethod
    def set_custom_id(cls, name: str, custom_id: str) -> bool:
        """Attach a custom emoji id to a registered spec (returns success)."""
        spec = cls.all_specs().get(name.lower())
        if spec is None:
            logger.debug("Unknown emoji name for custom id: %s", name)
            return False
        # EmojiSpec is frozen: mutate through object.__setattr__ intentionally.
        object.__setattr__(spec, "custom_id", custom_id.strip())
        return True


def use_custom_emoji(enabled: bool) -> None:
    """Globally enable/disable ``<tg-emoji>`` markup."""
    global _custom_emoji_enabled
    _custom_emoji_enabled = bool(enabled)
    logger.info("Custom emoji markup %s", "enabled" if enabled else "disabled")


def custom_emoji_enabled() -> bool:
    """Current global custom-emoji switch."""
    return _custom_emoji_enabled


def resolve_emoji(name: str, *, premium: bool = True, fallback: str = "") -> str:
    """Functional alias of :meth:`Emoji.get` (handy inside f-strings)."""
    return Emoji.get(name, premium=premium, fallback=fallback)


def load_custom_emoji_from_env(prefix: str = CUSTOM_EMOJI_ENV_PREFIX) -> int:
    """Load ``TG_EMOJI_<NAME>=<id>`` variables into the registry.

    Returns how many custom ids were applied. Called once at startup; when at
    least one id is found, custom emoji markup is switched on automatically.
    """
    applied = 0
    for key, value in os.environ.items():
        if not key.startswith(prefix) or not value.strip():
            continue
        name = key[len(prefix):].lower()
        if Emoji.set_custom_id(name, value):
            applied += 1
    if applied:
        use_custom_emoji(True)
        logger.info("Loaded %d custom emoji ids from environment", applied)
    return applied
