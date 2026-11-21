"""``/shares`` — public share links (token based) + the owner's link manager.

Legacy parity: the old bot generated ``t.me/<bot>?start=share_<token>`` deep links
and could protect them with a numeric pass code. Both are kept; the HTTP API adds
view counters, expiry, revocation and a JSON payload for the Mini App.

Status codes (mapped from :mod:`services.exceptions`):

=============================  ================================================
``404 LinkNotFoundError``      unknown token
``410 LinkExpiredError``       link past its expiry
``410 LinkRevokedError``       owner revoked it, or the file became private
``403 InvalidPassCodeError``   wrong (or missing) pass code
=============================  ================================================
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import (
    get_current_user,
    get_db,
    get_settings_dep,
    get_share_service,
    throttle,
)
from api.schemas import MessageResponse, PublicFileOut, ShareOut, ShareUnlock
from api.serializers import serialize_public_file, serialize_share
from config import Settings
from db.models import File, SharedLink, User
from services.exceptions import DomainError
from services.share import ShareService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shares", tags=["shares"])


def _raise(exc: DomainError) -> HTTPException:
    """Domain error -> HTTP error envelope."""
    return HTTPException(status_code=exc.status_code, detail=exc.to_dict())


def _public_payload(
    record: File,
    link: SharedLink,
    share: ShareService,
    settings: Settings,
) -> PublicFileOut:
    """Build the anonymous payload, attaching stream/download URLs.

    A ticket is only issued when the caller has already satisfied the pass-code
    check (``resolve_token`` would have raised otherwise), so a protected file
    cannot be opened without its code.

    Both URLs are issued for **any** file kind, mirroring the owner-side
    ``GET /files/{id}/stream`` contract: documents, archives and APK/IPA builds
    are not playable but must stay downloadable through the link. The payload's
    ``is_streamable`` flag is what the Mini App uses to decide between the
    "▶️ Ijro" and "⬇️ Yuklab olish" actions.
    """
    payload = serialize_public_file(record, link)
    if settings.storage.stream_proxy_enabled:
        ttl = link.seconds_left
        lifetime = settings.cache.ttl_stream_token
        ticket = share.issue_stream_ticket(
            record.id,
            link.created_by or record.user_id,
            ttl=min(lifetime, ttl) if ttl else lifetime,
        )
        payload.download_url = share.stream_url(ticket, download=True)
        payload.stream_url = share.stream_url(ticket)
    return payload


# ---------------------------------------------------------------------------
# Owner side (JWT required)
# ---------------------------------------------------------------------------
@router.get("", response_model=list[ShareOut], summary="My share links")
async def my_shares(
    user: Annotated[User, Depends(get_current_user)],
    share: Annotated[ShareService, Depends(get_share_service)],
    active_only: Annotated[bool, Query()] = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[ShareOut]:
    """Newest-first list of the caller's links (Mini App "Ulashishlar" tab)."""
    links = await share.list_user_links(user.telegram_id, active_only=active_only, limit=limit)
    return [serialize_share(link, share) for link in links]


@router.delete("/{token}", response_model=MessageResponse, summary="Revoke one of my links")
async def revoke(
    token: str,
    user: Annotated[User, Depends(get_current_user)],
    share: Annotated[ShareService, Depends(get_share_service)],
) -> MessageResponse:
    """Soft-delete a link; counters survive for statistics."""
    try:
        await share.revoke_link(token, user.telegram_id)
    except DomainError as exc:
        raise _raise(exc) from exc
    return MessageResponse(ok=True, message="Link revoked")


@router.post("/{token}/extend", response_model=ShareOut, summary="Extend a link's lifetime")
async def extend(
    token: str,
    user: Annotated[User, Depends(get_current_user)],
    share: Annotated[ShareService, Depends(get_share_service)],
    ttl_seconds: Annotated[int, Query(ge=60, le=60 * 60 * 24 * 365)] = 60 * 60 * 24 * 7,
) -> ShareOut:
    """Push ``expires_at`` forward from now and re-activate a revoked link."""
    try:
        link = await share.extend_link(token, user.telegram_id, ttl_seconds)
    except DomainError as exc:
        raise _raise(exc) from exc
    return serialize_share(link, share)


# ---------------------------------------------------------------------------
# Public side (no auth - the token is the credential)
# ---------------------------------------------------------------------------
@router.get(
    "/public/{token}/check",
    response_model=dict[str, Any],
    summary="Is this token alive / protected?",
)
async def check_token(
    token: str,
    share: Annotated[ShareService, Depends(get_share_service)],
) -> dict[str, Any]:
    """Cheap probe the Mini App runs before rendering a share page.

    Declared **before** ``/public/{token}`` so the literal ``check`` segment is
    not swallowed by the token parameter.
    """
    try:
        link = await share.get_link(token)
    except DomainError:
        return {"exists": False, "active": False, "requires_pass_code": False}
    return {
        "exists": True,
        "active": link.is_active_now,
        "requires_pass_code": link.has_pass_code,
        "expires_at": link.expires_at,
        "seconds_left": link.seconds_left,
        "views": int(link.views or 0),
    }


@router.get(
    "/public/{token}",
    response_model=PublicFileOut,
    summary="Public file payload",
    dependencies=[Depends(throttle("api"))],
)
async def public_file(
    token: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    pass_code: Annotated[str | None, Query(max_length=32)] = None,
    count_view: Annotated[bool, Query(description="Count this request as a view")] = True,
) -> PublicFileOut:
    """Resolve a public token into file metadata (+ stream URLs when allowed)."""
    share = ShareService(session, settings=settings)
    try:
        record, link = await share.resolve_token(
            token, pass_code=pass_code, register_view=count_view
        )
    except DomainError as exc:
        raise _raise(exc) from exc
    return _public_payload(record, link, share, settings)


@router.post(
    "/public/{token}/unlock",
    response_model=PublicFileOut,
    summary="Unlock a pass-code protected link",
    dependencies=[Depends(throttle("api"))],
)
async def unlock(
    token: str,
    payload: ShareUnlock,
    session: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> PublicFileOut:
    """Exchange a correct pass code for the file payload (rate limited)."""
    share = ShareService(session, settings=settings)
    try:
        record, link = await share.resolve_token(
            token, pass_code=payload.pass_code, register_view=False
        )
    except DomainError as exc:
        raise _raise(exc) from exc
    return _public_payload(record, link, share, settings)
