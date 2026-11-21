"""Sharing subsystem: tokens, expiry, pass codes and stream URLs.

A *share link* is the bridge between the bot and the outside world:

* inside Telegram it becomes a deep link ``t.me/<bot>?start=<token>``;
* on the web it becomes ``{BASE_URL}/s/<token>`` (Mini App preview page);
* media playback uses the signed, short-lived stream URL built here.

Security rules implemented in this module:

1. Tokens are generated with :func:`secrets.token_urlsafe` (never random()).
2. Optional pass codes are stored **salted + SHA-256 hashed**, verified with a
   constant-time comparison.
3. Expiry and revocation are checked on every resolve - no trust in the client.
4. Stream URLs are signed with HMAC and expire in minutes, so a leaked URL
   cannot be reused indefinitely.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from sqlalchemy import and_, desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config import Settings, get_settings
from db.models import File, SharedLink, Visibility
from db.models.shared_link import generate_pass_code, hash_pass_code, verify_pass_code
from services.cache import Cache, get_cache
from services.exceptions import (
    FileNotFoundError,
    InvalidPassCodeError,
    LinkExpiredError,
    LinkNotFoundError,
    LinkRevokedError,
    NotOwnerError,
)

logger = logging.getLogger(__name__)

__all__ = ["ShareService", "StreamTicket"]


class StreamTicket:
    """A signed, short-lived grant to stream one file.

    Serialized form: ``base64url(payload).base64url(signature)`` where payload
    is ``file_id|telegram_id|exp|purpose``. Verification is a single HMAC
    compare - no DB round trip, which keeps media seeking cheap.
    """

    __slots__ = ("expires_at", "file_id", "owner_id", "purpose", "token")

    def __init__(
        self,
        *,
        file_id: uuid.UUID,
        owner_id: int,
        expires_at: datetime,
        purpose: str = "stream",
        token: str | None = None,
    ) -> None:
        self.file_id = file_id
        self.owner_id = owner_id
        self.expires_at = expires_at
        self.purpose = purpose
        self.token = token or ""

    @property
    def is_expired(self) -> bool:
        """``True`` once the ticket's lifetime has passed."""
        return datetime.now(UTC) >= self.expires_at

    @property
    def seconds_left(self) -> int:
        """Remaining validity in seconds (never negative)."""
        return max(0, int((self.expires_at - datetime.now(UTC)).total_seconds()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_id": str(self.file_id),
            "owner_id": self.owner_id,
            "expires_at": self.expires_at.isoformat(),
            "seconds_left": self.seconds_left,
            "purpose": self.purpose,
            "token": self.token,
        }


class ShareService:
    """Create, validate and revoke share links; sign stream URLs."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        cache: Cache | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.cache = cache or get_cache()
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------
    # Link lifecycle
    # ------------------------------------------------------------------
    async def create_link(
        self,
        file_uuid: uuid.UUID | str,
        telegram_id: int,
        *,
        ttl_seconds: int | None = None,
        pass_code: str | None = None,
        make_public: bool = False,
    ) -> SharedLink:
        """Create a share link for a file owned by ``telegram_id``."""
        record = await self._get_owned_file(file_uuid, telegram_id)

        ttl = self.settings.storage.default_link_ttl if ttl_seconds is None else ttl_seconds
        link = SharedLink.create_for(
            record.id,
            ttl_seconds=ttl,
            pass_code=pass_code,
            created_by=telegram_id,
        )
        self.session.add(link)

        # Havola **ishlaydigan** bo'lishi kerak: ``resolve_token`` PRIVATE faylni
        # rad etadi ("eski havola shaxsiy faylni oshkor qilmasin"). Shu sababli
        # havola yaratilganda fayl kamida UNLISTED bo'ladi:
        #
        # * ``UNLISTED`` — token bilan ochiladi, lekin ommaviy ro'yxatlarda
        #   ko'rinmaydi (``is_public`` False qoladi);
        # * ``PUBLIC``   — ``make_public=True`` bo'lsa: ro'yxatlarda ham ko'rinadi.
        #
        # Bu xatti-harakat oldin yo'q edi: havola yaratilar, lekin ochilganda
        # 410 "This file is private" qaytarardi (deep-link import ham shu
        # sababli ishlamasdi).
        if make_public:
            if record.visibility is not Visibility.PUBLIC:
                record.set_visibility(Visibility.PUBLIC)
        elif record.visibility is Visibility.PRIVATE:
            record.set_visibility(Visibility.UNLISTED)

        await self.session.commit()
        await self.cache.delete(self.cache.keys.file(record.id))
        logger.info("Created share link %s for file %s", link.token[:8], record.id)
        return link

    async def list_links(
        self, file_uuid: uuid.UUID | str, telegram_id: int, *, active_only: bool = True
    ) -> list[SharedLink]:
        """All links of one file (newest first)."""
        record = await self._get_owned_file(file_uuid, telegram_id)
        statement = (
            select(SharedLink)
            .where(SharedLink.file_id == record.id)
            .order_by(desc(SharedLink.created_at))
        )
        if active_only:
            statement = statement.where(SharedLink.is_active.is_(True))
        return list((await self.session.scalars(statement)).all())

    async def list_user_links(
        self, telegram_id: int, *, active_only: bool = True, limit: int = 100
    ) -> list[SharedLink]:
        """Every link created by ``telegram_id`` (newest first).

        Powers the Mini App's "Ulashishlar" tab; the file row is eagerly loaded so
        rendering never triggers extra queries.
        """
        statement = (
            select(SharedLink)
            .where(SharedLink.created_by == telegram_id)
            .options(selectinload(SharedLink.file))
            .order_by(desc(SharedLink.created_at))
            .limit(limit)
        )
        if active_only:
            statement = statement.where(SharedLink.is_active.is_(True))
        return list((await self.session.scalars(statement)).unique().all())

    async def get_link(self, token: str) -> SharedLink:
        """Fetch a link by token, raising :class:`LinkNotFoundError` when absent."""
        link = await self.session.scalar(
            select(SharedLink)
            .where(SharedLink.token == token)
            .options(selectinload(SharedLink.file))
        )
        if link is None:
            raise LinkNotFoundError(f"Unknown share token {token!r}")
        return link

    async def revoke_link(self, token: str, telegram_id: int) -> SharedLink:
        """Soft-delete a link (owner or admin only)."""
        link = await self.get_link(token)
        self._assert_can_manage(link, telegram_id)
        link.revoke()
        await self.session.commit()
        await self.cache.delete(self.cache.keys.file_by_token(token))
        await self.cache.delete(self.cache.keys.file(link.file_id))
        return link

    async def revoke_all_for_file(self, file_uuid: uuid.UUID | str, telegram_id: int) -> int:
        """Revoke every link of a file; returns how many were disabled."""
        record = await self._get_owned_file(file_uuid, telegram_id)
        result = await self.session.execute(
            update(SharedLink)
            .where(and_(SharedLink.file_id == record.id, SharedLink.is_active.is_(True)))
            .values(is_active=False)
            .execution_options(synchronize_session=False)
        )
        await self.session.commit()
        await self.cache.delete(self.cache.keys.file(record.id))
        return int(result.rowcount or 0)

    async def extend_link(self, token: str, telegram_id: int, ttl_seconds: int) -> SharedLink:
        """Push the expiry forward and re-activate a revoked link."""
        link = await self.get_link(token)
        self._assert_can_manage(link, telegram_id)
        link.extend(ttl_seconds)
        await self.session.commit()
        await self.cache.delete(self.cache.keys.file_by_token(token))
        return link

    async def set_pass_code(
        self, token: str, telegram_id: int, pass_code: str | None
    ) -> SharedLink:
        """Add, rotate or remove the access code of a link."""
        link = await self.get_link(token)
        self._assert_can_manage(link, telegram_id)
        link.pass_code = hash_pass_code(pass_code) if pass_code else None
        await self.session.commit()
        await self.cache.delete(self.cache.keys.file_by_token(token))
        return link

    @staticmethod
    def new_pass_code(length: int = 6) -> str:
        """Generate a fresh numeric access code."""
        return generate_pass_code(length)

    # ------------------------------------------------------------------
    # Resolution (public access)
    # ------------------------------------------------------------------
    async def resolve_token(
        self, token: str, *, pass_code: str | None = None, register_view: bool = True
    ) -> tuple[File, SharedLink]:
        """Validate a share token and return ``(file, link)``.

        Raises :class:`LinkExpiredError` / :class:`LinkRevokedError` /
        :class:`InvalidPassCodeError` - the API layer turns those into 410/403.
        The link row is always read from the database: an authorization decision
        must never be served from cache.
        """
        link = await self.get_link(token)

        if not link.is_active:
            raise LinkRevokedError("This link was revoked by its owner")
        if link.is_expired:
            raise LinkExpiredError("This link has expired")
        if link.has_pass_code and not verify_pass_code(pass_code or "", link.pass_code):
            raise InvalidPassCodeError("Wrong access code")

        record = link.file
        if record is None:  # pragma: no cover - defensive (joined load)
            record = await self.session.get(File, link.file_id)
        if record is None:
            raise FileNotFoundError("The shared file no longer exists")

        if record.visibility is Visibility.PRIVATE:
            # Private files are only reachable by their owner: a stale link must
            # not leak them.
            raise LinkRevokedError("This file is private")

        if register_view:
            await self._register_access(record.id, token, is_download=False)

        await self.cache.set(
            self.cache.keys.file_by_token(token),
            {
                "file_id": str(record.id),
                "name": record.file_name,
                "size": record.file_size,
                "category": record.category.value,
                "mime_type": record.mime_type,
                "expires_at": link.expires_at.isoformat() if link.expires_at else None,
                "has_pass_code": link.has_pass_code,
            },
            min(
                self.settings.cache.ttl_file,
                link.seconds_left or self.settings.cache.ttl_file,
            ),
        )
        return record, link

    async def resolve_for_owner(
        self, file_uuid: uuid.UUID | str, telegram_id: int
    ) -> File:
        """Resolve a file for its owner (used by the Mini App after JWT auth)."""
        return await self._get_owned_file(file_uuid, telegram_id)

    async def _register_access(
        self, file_id: uuid.UUID, token: str, *, is_download: bool
    ) -> None:
        """Bump counters on both the file and the link in one commit."""
        await self.session.execute(
            update(SharedLink)
            .where(SharedLink.token == token)
            .values(
                views=SharedLink.views + 1,
                downloads=SharedLink.downloads + (1 if is_download else 0),
                last_accessed_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
        await self.session.execute(
            update(File)
            .where(File.id == file_id)
            .values(
                views=File.views + 1,
                downloads=File.downloads + (1 if is_download else 0),
            )
            .execution_options(synchronize_session=False)
        )
        await self.session.commit()

    # ------------------------------------------------------------------
    # Signed stream tickets (Mini App media playback)
    # ------------------------------------------------------------------
    def issue_stream_ticket(
        self,
        file_uuid: uuid.UUID | str,
        telegram_id: int,
        *,
        ttl: int | None = None,
        purpose: str = "stream",
    ) -> StreamTicket:
        """Create an HMAC-signed ticket for ``/api/v1/stream/{ticket}``."""
        lifetime = ttl or self.settings.cache.ttl_stream_token
        expires_at = datetime.now(UTC) + timedelta(seconds=lifetime)
        ticket = StreamTicket(
            file_id=_as_uuid(file_uuid),
            owner_id=telegram_id,
            expires_at=expires_at,
            purpose=purpose,
        )
        payload = _encode_payload(
            f"{ticket.file_id}|{telegram_id}|{int(expires_at.timestamp())}|{purpose}"
        )
        ticket.token = f"{payload}.{_sign(payload, self._signing_key)}"
        return ticket

    def verify_stream_ticket(self, token: str) -> StreamTicket:
        """Validate signature + expiry and rebuild the :class:`StreamTicket`."""
        try:
            payload, signature = token.split(".", 1)
        except ValueError as exc:
            raise LinkNotFoundError("Malformed stream ticket") from exc

        expected = _sign(payload, self._signing_key)
        if not hmac.compare_digest(expected, signature):
            raise NotOwnerError("Invalid stream ticket signature")

        raw = _decode_payload(payload)
        file_id, owner_id, expires_ts, purpose = raw.split("|", 3)
        expires_at = datetime.fromtimestamp(int(expires_ts), tz=UTC)
        if datetime.now(UTC) >= expires_at:
            raise LinkExpiredError("Stream ticket expired, request a new one")

        return StreamTicket(
            file_id=uuid.UUID(file_id),
            owner_id=int(owner_id),
            expires_at=expires_at,
            purpose=purpose,
            token=token,
        )

    def stream_url(self, ticket: StreamTicket, *, download: bool = False) -> str:
        """Absolute URL of the streaming endpoint for a ticket."""
        base = self.settings.app.base_url.rstrip("/")
        prefix = self.settings.api.api_prefix
        # DIQQAT: bazaviy URL da query yo'q — shuning uchun "?" kerak.
        # "&" bo'lsa u ticket matniga ulanib qoladi va signature buziladi (403).
        suffix = "?download=1" if download else ""
        return f"{base}{prefix}/stream/{quote(ticket.token, safe='.-_~')}{suffix}"

    def share_url(self, token: str) -> str:
        """Public web page for a share token (``{BASE_URL}/s/{token}``)."""
        return f"{self.settings.app.base_url.rstrip('/')}/s/{quote(token, safe='')}"

    def deep_link(self, token: str) -> str:
        """Telegram deep link: ``t.me/<bot>?start=<token>``."""
        return self.settings.bot.start_link(token)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @property
    def _signing_key(self) -> bytes:
        """HMAC key: the JWT secret is reused (both are server-side secrets)."""
        return self.settings.security.jwt_secret.get_secret_value().encode("utf-8")

    async def _get_owned_file(self, file_uuid: uuid.UUID | str, telegram_id: int) -> File:
        """Load a file and assert ownership (admins bypass the check)."""
        parsed = _as_uuid(file_uuid)
        record = await self.session.scalar(
            select(File).where(File.id == parsed).options(selectinload(File.shared_links))
        )
        if record is None:
            raise FileNotFoundError(f"File {file_uuid} not found")
        if record.user_id != telegram_id and telegram_id not in self.settings.bot.admin_ids:
            raise NotOwnerError("You do not own this file", file_id=str(parsed))
        return record

    def _assert_can_manage(self, link: SharedLink, telegram_id: int) -> None:
        """Only the file owner (or an admin) may mutate a link."""
        owner = link.file.user_id if link.file is not None else link.created_by
        if owner != telegram_id and telegram_id not in self.settings.bot.admin_ids:
            raise NotOwnerError("You cannot manage this link")


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    """Coerce to :class:`uuid.UUID` with a clear error message."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise FileNotFoundError(f"Malformed file id: {value!r}") from exc



def _encode_payload(raw: str) -> str:
    """URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_payload(encoded: str) -> str:
    """Inverse of :func:`_encode_payload` (re-adds padding as needed)."""
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding).decode("utf-8")


def _sign(payload: str, key: bytes) -> str:
    """HMAC-SHA256 signature, URL-safe base64 encoded."""
    digest = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verify_signature(payload: str, signature: str, key: bytes) -> bool:
    """Public helper so tests/routes can verify arbitrary signed payloads."""
    return hmac.compare_digest(_sign(payload, key), signature)


