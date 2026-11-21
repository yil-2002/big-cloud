"""``/stream`` — HTTP Range-aware media proxy for the Mini App player.

Why a proxy at all?
-------------------
Telegram gives a bot a temporary download URL
(``https://api.telegram.org/file/bot<TOKEN>/<file_path>``). Handing that URL to a
browser would **leak the bot token**, and it cannot be embedded in a Mini App
webview anyway. So this endpoint:

1. verifies an HMAC-signed, short-lived **ticket** (no token in the URL);
2. resolves ``file_path`` via ``getFile`` (cached ~50 min in Redis);
3. streams bytes through, forwarding the client's ``Range`` header so seeking in
   ``<video>`` works, and forwarding Telegram's ``Content-Range`` back.

Nothing is written to disk: chunks are relayed and discarded
(``STREAM_PROXY_ENABLED=false`` turns the whole feature off).
"""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import AsyncIterator
from typing import Annotated, Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db, get_settings_dep, throttle
from config import Settings
from db.models import File
from services.cache import get_cache
from services.exceptions import LinkExpiredError, LinkNotFoundError, NotOwnerError
from services.share import ShareService, StreamTicket

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stream"])

#: Headers relayed from Telegram to the client.
#: Mijozga uzatiladigan upstream sarlavhalari.
#:
#: ``accept-ranges`` bu yerda **yo'q**: uni javobga o'zimiz aniq ``bytes`` qilib
#: yozamiz. Aks holda upstream ham ``Accept-Ranges`` yuborganida sarlavha
#: takrorlanib ``bytes, bytes`` bo'lib qoladi (ba'zi pleerlar buni noto'g'ri
#: talqin qiladi).
#:
#: ``content-type`` bu yerda **yo'q**: uni Telegram ko'pincha
#: ``application/octet-stream`` qilib qaytaradi. Uni ``X-Content-Type-Options:
#: nosniff`` bilan birga yuborsak, brauzer ``<img>`` ni render qilishdan bosh
#: tortadi — Mini App kartalarida preview/thumbnail aynan shu sababdan
#: ko'rinmasdi. Tur :func:`_guess_media_type` orqali aniqlanadi.
_PASS_THROUGH = (
    "content-length",
    "content-range",
    "last-modified",
    "etag",
)

#: Telegram ``getFile`` odatda shu turni qaytaradi — fayl turini bildirmaydi.
_OCTET_STREAM = "application/octet-stream"

#: Hop-by-hop headers that must never be forwarded.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


async def get_telegram_file_path(
    file_id: str, settings: Settings, client: httpx.AsyncClient
) -> str:
    """Resolve Telegram's temporary ``file_path`` for ``file_id`` (cached).

    Implemented with ``httpx`` (not an aiogram ``Bot``) so the HTTP layer stays
    independent from the bot process - important when the API runs standalone.
    """
    cache = get_cache()
    cache_key = cache.keys.tg_file_path(file_id)
    cached = await cache.get(cache_key)
    if isinstance(cached, str) and cached:
        return cached

    base = settings.storage.local_bot_api_server or "https://api.telegram.org"
    url = f"{base}/bot{settings.bot_token}/getFile"
    try:
        response = await client.get(url, params={"file_id": file_id}, timeout=15.0)
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("getFile failed for %s: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "telegram_unreachable", "message": "Telegram Bot API is not responding"},
        ) from exc

    if not payload.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "telegram_file_not_found",
                "message": str(payload.get("description") or "Telegram could not resolve this file"),
            },
        )

    path = (payload.get("result") or {}).get("file_path")
    if not path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "telegram_file_not_found", "message": "Telegram returned no file path"},
        )

    await cache.set(cache_key, path, settings.cache.ttl_telegram_file_path)
    return str(path)


def build_upstream_url(settings: Settings, file_path: str) -> str:
    """Absolute Telegram download URL for a resolved ``file_path``."""
    base = settings.storage.local_bot_api_server or "https://api.telegram.org"
    return f"{base}/file/bot{settings.bot_token}/{quote(file_path)}"


def _verify_ticket(token: str, session: AsyncSession, settings: Settings) -> StreamTicket:
    """Validate signature + expiry of a stream ticket."""
    share = ShareService(session, settings=settings)
    try:
        return share.verify_stream_ticket(token)
    except (LinkNotFoundError, LinkExpiredError, NotOwnerError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict()) from exc
    except Exception as exc:
        logger.debug("Bad stream ticket: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_ticket", "message": "Malformed stream ticket"},
        ) from exc


def ensure_streamable_size(record: File, settings: Settings) -> None:
    """20 MB devori: ``file_size`` chegaradan katta bo'lsa **413** ko'tarish.

    Nega kerak: rasmiy Bot API ``getFile`` faqat ~20 MB gacha fayl beradi
    (`Telegram hujjatlari <https://core.telegram.org/bots/api#getfile>`_).
    Kattaroq fayl uchun Telegram baribir xato qaytaradi — lekin u xato 400
    ko'rinishida, kech va tushunarsiz bo'ladi. Oldindan tekshirish esa:

    * Telegram'ga ortiqcha so'rov ketmaydi (getFile + kesh ifloslanmaydi);
    * mijoz **barqaror shartnoma** oladi: ``413`` +
      ``{"code": "file_too_large_for_stream"}`` — Mini App shu kod bo'yicha
      "Telegram'da ochish" tugmasini ko'rsatadi;
    * ``STREAM_MAX_SIZE=0`` (masalan, lokal Bot API server bilan) devorni
      butunlay o'chiradi.

    ``file_size`` noma'lum (``None``/0) bo'lsa tekshirmaymiz: eski yozuvlarda
    hajm bo'lmasligi mumkin, ularni bloklash noto'g'ri (Telegram o'zi hal
    qiladi).
    """
    limit = settings.storage.stream_max_size
    size = int(record.file_size or 0)
    if limit and size > limit:
        if settings.mt.configured and record.archive_message_id:
            return

        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": "file_too_large_for_stream",
                "message": (
                    "Bot API only serves files up to "
                    f"{limit} bytes for streaming; this file is {size} bytes. "
                    "Open it in Telegram instead."
                ),
                "size": size,
                "limit": limit,
            },
        )


#: ``/stream`` qabul qiladigan ticket maqsadlari. Boshqa qiymat — 403.
# Ticket'ning ruxsat etilgan maqsadlari. Har biri real oqimga mos:
#   stream   — serializer'lar beradigan odatiy pleer/preview havolasi;
#   thumb    — o'sha endpoint, lekin thumbnail_file_id ustidan (4-bosqich);
#   download — ?download=1 bilan attachment sifatida yuklab olish;
#   play     — bot ichidagi "▶️ Ijro" tugmasi chiqargan havola.
# Ro'yxatdan tashqaridagi (masalan, kelajakdagi "upload") maqsadlar rad etiladi.
_ALLOWED_PURPOSES = frozenset({"stream", "thumb", "download", "play"})


def _enforce_ticket_semantics(ticket: StreamTicket, record: File, settings: Settings) -> None:
    """Ticket ``purpose`` va ``owner_id`` semantikasini qat'iy tekshirish.

    * ``purpose`` faqat ``stream``/``thumb`` bo'lishi mumkin — imzolangan, lekin
      boshqa maqsadli token (masalan, kelajakdagi ``upload``) bu endpointdan
      foyda ko'rmasin;
    * ``owner_id`` fayl egasi (yoki admin) bo'lishi shart. Ommaviy havola
      chiptalari ham havola yaratuvchisi (fayl egasi) nomidan chiqariladi,
      shuning uchun qonuniy oqimlarning hech biri bu tekshiruvda sinmaydi —
      faqat "begona user_id bilan yasalgan" anomal chiptalar rad etiladi.
    """
    if ticket.purpose not in _ALLOWED_PURPOSES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "invalid_ticket",
                "message": f"Ticket purpose '{ticket.purpose}' is not valid for /stream",
            },
        )
    if ticket.owner_id != record.user_id and ticket.owner_id not in settings.bot.admin_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "invalid_ticket",
                "message": "Ticket owner does not match the file owner",
            },
        )


def _upstream_header(upstream: Any, name: str) -> str | None:
    """Upstream javobidan bitta sarlavhani olish (harf katta-kichikligiga befarq).

    ``httpx.Headers`` o'zi case-insensitive, lekin ba'zi proxy/mock obyektlari
    oddiy ``dict`` bo'lishi mumkin — shunda ``Content-Range`` yo'qolib, brauzer
    pleerida **seek ishlamay qoladi**. Shuning uchun ikkala holatni qoplaymiz.
    """
    headers = getattr(upstream, "headers", None) or {}
    getter = getattr(headers, "get", None)
    if getter is None:  # pragma: no cover - himoya
        return None
    value = getter(name)
    if value is None:
        lowered = name.lower()
        for key, candidate in dict(headers).items():
            if str(key).lower() == lowered:
                value = candidate
                break
    return str(value) if value else None


def _guess_media_type(record: File, *, is_thumb: bool, upstream: Any) -> str:
    """Javob uchun ``Content-Type`` ni aniqlash (rasm/video preview uchun muhim).

    Tartib:

    1. **Thumbnail** — Telegram thumbnail'lari har doim JPEG, shuning uchun
       ``image/jpeg`` (asosiy faylning ``video/mp4`` turini meros qilib
       olmasligi kerak — aks holda brauzer rasmni chizmaydi);
    2. upstream turi — agar u aniq tur bo'lsa (``video/mp4``, ``image/png``);
    3. bazadagi ``mime_type`` / kengaytma (``services.filetype.effective_type``);
    4. oxirgi chora — ``application/octet-stream``.
    """
    if is_thumb:
        return "image/jpeg"
    upstream_type = (_upstream_header(upstream, "content-type") or "").split(";")[0].strip()
    if upstream_type and upstream_type.lower() != _OCTET_STREAM:
        return upstream_type
    # ``effective_type`` bazadagi qiymatni saqlaydi, bo'lmasa kengaytmadan
    # hisoblaydi — kengaytmasiz ``tmp_3295`` uchun ham barqaror natija.
    from services.filetype import effective_type

    stored_mime, _category = effective_type(record)
    if stored_mime:
        return stored_mime
    guessed = mimetypes.guess_type(record.file_name or "")[0]
    return guessed or _OCTET_STREAM


def _response_headers(
    upstream: Any,
    *,
    download: bool,
    name: str,
    content_type: str | None = None,
    is_thumb: bool = False,
) -> dict[str, str]:
    """Build client-facing headers from the upstream response."""
    headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        # Thumbillar o'zgarmas (file_id ga bog'langan) — uzoq kesh: ro'yxat qayta
        # ochilganda rasm qaytadan yuklanmaydi ("miltillash"ning asosiy sababi).
        "Cache-Control": (
            "private, max-age=86400, immutable" if is_thumb else "private, max-age=300"
        ),
        "X-Content-Type-Options": "nosniff",
    }
    # Tur har doim aniq bo'lishi shart: ``nosniff`` bilan ``octet-stream``
    # brauzerga rasmni render qilishni taqiqlaydi.
    if content_type:
        headers["Content-Type"] = content_type
    for header in _PASS_THROUGH:
        value = _upstream_header(upstream, header)
        if value:
            headers[header] = value

    disposition = "attachment" if download else "inline"
    headers["Content-Disposition"] = f'{disposition}; filename*=UTF-8\'\'{quote(name)}'
    return headers


@router.head(
    "/stream/{ticket}",
    summary="Probe streamability (HEAD)",
    dependencies=[Depends(throttle("stream"))],
)
@router.get(
    "/stream/{ticket}",
    summary="Stream media with HTTP Range support",
    dependencies=[Depends(throttle("stream"))],
)
async def stream_file(
    ticket: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    download: Annotated[bool, Query(description="Force Content-Disposition: attachment")] = False,
) -> Response:
    """Relay Telegram bytes to the client (Range-aware, zero disk usage)."""
    if not settings.storage.stream_proxy_enabled:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "code": "streaming_disabled",
                "message": "STREAM_PROXY_ENABLED=false - use the bot to download",
            },
        )

    verified = _verify_ticket(ticket, session, settings)
    record: File | None = await session.get(File, verified.file_id)
    if record is None or record.deleted_at is not None:
        # Savatdagi (soft-deleted) fayl ham xuddi yo'q fayl kabi: eski ticket
        # bilan o'chirilgan kontentni oqizib bo'lmasin.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "file_not_found", "message": "This file no longer exists"},
        )

    # --- Ticket qat'iyligi: purpose + owner (6-bosqich) -----------------------
    # HMAC imzo soxtalashtirishdan himoya qiladi, lekin qat'iy semantika ham
    # kerak: (1) faqat ma'lum purpose qiymatlari o'tadi (kelajakda boshqa
    # maqsadli token bu endpointga adashib kelmasin); (2) ticket egasi fayl
    # egasi bo'lishi shart — egasi bo'lmagan foydalanuvchiga berilgan ticket
    # faqat share havolasi orqali (link yaratuvchisi nomidan) chiqariladi,
    # shuning uchun u yo'l ham shu tekshiruvdan o'tadi.
    _enforce_ticket_semantics(verified, record, settings)

    # --- Thumbnail rejimi (purpose="thumb") -----------------------------------
    # Xuddi shu endpoint, lekin asosiy fayl o'rniga **thumbnail** file_id
    # oqiziladi. Thumbnail'lar doim kichik (~KB), shuning uchun 20 MB devori
    # unga qo'llanmaydi — 100 MB video ham kartada rasmini ko'rsata oladi.
    is_thumb = verified.purpose == "thumb"
    if is_thumb and not record.thumbnail_file_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "thumbnail_not_found", "message": "This file has no thumbnail"},
        )

    # --- 20 MB devori (rasmiy Bot API cheklovi) ------------------------------
    # ``getFile`` 20 MB dan katta fayl uchun baribir xato qaytaradi; biz
    # Telegram'ga murojaat qilmasdan **oldin** aniq 413 beramiz, shunda Mini App
    # foydalanuvchiga "Telegram'da ochish" yo'lini darhol ko'rsatadi (bot
    # ``file_id`` bo'yicha faylni chatga cheklovsiz yubora oladi).
    if not is_thumb:
        ensure_streamable_size(record, settings)

    telegram_file_id = record.thumbnail_file_id if is_thumb else record.file_id

    client: httpx.AsyncClient = request.app.state.http
    
    size = int(record.file_size or 0)
    use_mtproto = (
        not is_thumb
        and settings.storage.stream_max_size
        and size > settings.storage.stream_max_size
        and settings.mt.configured
        and record.archive_message_id
    )

    if use_mtproto:
        from services.mtproto import MTProtoError, get_client, iter_range
        mt_client = await get_client(settings)
        if mt_client is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "mtproto_unavailable", "message": "MTProto service is unavailable"},
            )

        range_header = request.headers.get("range")
        offset = 0
        limit = size
        status_code = status.HTTP_200_OK
        content_range = None

        if range_header and range_header.startswith("bytes="):
            parts = range_header.replace("bytes=", "").split("-")
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if len(parts) > 1 and parts[1] else size - 1
            
            offset = start
            limit = min(end - start + 1, size - start)
            status_code = status.HTTP_206_PARTIAL_CONTENT
            content_range = f"bytes {start}-{start + limit - 1}/{size}"

        if request.method != "HEAD":
            try:
                record.views = int(record.views or 0) + 1
                if download:
                    record.downloads = int(record.downloads or 0) + 1
                session.add(record)
                await session.commit()
            except Exception:
                logger.debug("Could not update counters for %s", record.id)
                await session.rollback()

        media_type = _guess_media_type(record, is_thumb=is_thumb, upstream=None)
        
        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
            "Content-Type": media_type,
            "Content-Length": str(limit),
        }
        if content_range:
            headers["Content-Range"] = content_range
            
        disposition = "attachment" if download else "inline"
        headers["Content-Disposition"] = f'{disposition}; filename*=UTF-8\'\'{quote(record.file_name or "")}'

        if request.method == "HEAD":
            return Response(status_code=status_code, headers=headers, media_type=media_type)

        async def mt_body():
            try:
                chat_id = settings.archive.numeric_id or settings.archive.channel_id
                async for piece in iter_range(
                    mt_client, 
                    chat_id,
                    record.archive_message_id, 
                    offset, 
                    limit,
                    chunk=settings.mt.chunk_size
                ):
                    yield piece
            except ConnectionResetError:
                pass
            except MTProtoError as exc:
                logger.error("MTProto stream error: %s", exc)

        return StreamingResponse(
            mt_body(),
            status_code=status_code,
            headers=headers,
            media_type=media_type,
        )

    try:
        file_path = await get_telegram_file_path(telegram_file_id, settings, client)
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        # Telegram javob bermadi: mijozga aniq 502 (ichki traceback emas).
        logger.warning("Telegram getFile failed for %s: %s", record.id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "telegram_unreachable",
                "message": "Telegram Bot API is not responding",
            },
        ) from exc
    upstream_url = build_upstream_url(settings, file_path)

    range_header = request.headers.get("range")
    upstream_headers: dict[str, str] = {}
    if range_header:
        upstream_headers["Range"] = range_header

    # --- HEAD: answer with metadata only (players probe this first) -----------
    if request.method == "HEAD":
        head = await client.head(upstream_url, headers=upstream_headers, timeout=20.0)
        head_type = _guess_media_type(record, is_thumb=is_thumb, upstream=head)
        return Response(
            status_code=head.status_code,
            headers=_response_headers(
                head,
                download=download,
                name=record.file_name,
                content_type=head_type,
                is_thumb=is_thumb,
            ),
            media_type=head_type,
        )

    # --- GET: open the upstream stream and relay chunks -----------------------
    upstream_request = client.build_request("GET", upstream_url, headers=upstream_headers)
    upstream_response = await client.send(upstream_request, stream=True)

    if upstream_response.status_code >= 400:
        await upstream_response.aclose()
        status_code = (
            status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE
            if upstream_response.status_code == 416
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(
            status_code=status_code,
            detail={
                "code": "upstream_error",
                "message": f"Telegram returned {upstream_response.status_code}",
            },
        )

    # Count the delivery (best effort - a failed counter must not break playback).
    # Thumbnail'lar hisobga olinmaydi: ro'yxatni ochishning o'zi "ko'rish" emas.
    if not is_thumb:
        try:
            record.views = int(record.views or 0) + 1
            if download:
                record.downloads = int(record.downloads or 0) + 1
            session.add(record)
            await session.commit()
        except Exception:
            logger.debug("Could not update counters for %s", record.id)
            await session.rollback()

    async def body() -> AsyncIterator[bytes]:
        """Relay chunks, then always close the upstream response."""
        try:
            async for chunk in upstream_response.aiter_bytes(64 * 1024):
                yield chunk
        except httpx.HTTPError as exc:  # client aborted or network hiccup
            logger.info("Stream interrupted for %s: %s", record.id, exc)
        finally:
            await upstream_response.aclose()

    # Content-Type har doim bo'lishi kerak: aks holda brauzer pleeri media turini
    # aniqlay olmaydi (masalan Telegram javobida content-type bo'lmasa).
    # Thumbnail doim JPEG (Telegram shartnomasi) — asosiy faylning mime'ini
    # ishlatib yubormaslik kerak (video/mp4 sarlavhali rasm buziladi).
    media_type = _guess_media_type(record, is_thumb=is_thumb, upstream=upstream_response)
    return StreamingResponse(
        body(),
        status_code=upstream_response.status_code,
        headers=_response_headers(
            upstream_response,
            download=download,
            name=record.file_name,
            content_type=media_type,
            is_thumb=is_thumb,
        ),
        media_type=media_type,
    )
