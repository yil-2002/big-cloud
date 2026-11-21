"""``/files`` — listing, registration, metadata, pinning, sharing and deletion.

Design notes that preserve the legacy bot's behaviour:

* files are **never** uploaded through this API — the payload lives on Telegram's
  servers and we only register its ``file_id`` (``POST /files`` is the HTTP twin
  of the old ``save_file``);
* the default listing has **no folder filter** (the old bot listed everything);
  pass ``folder_id=root`` to list only root-level files;
* the legacy tabs are first-class query parameters: ``category``, ``apps_only``
  (🤖 APK/IPA) and ``pinned`` (📌 Muhimlar);
* ``GET /files/{id}/stream`` issues a signed ticket so the Mini App can play media
  with HTTP Range support without any byte ever touching this server's disk.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import uuid
from typing import Annotated, Any, Literal

from aiogram.types import FSInputFile
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)

from api.dependencies import (
    get_current_user,
    get_settings_dep,
    get_share_service,
    get_storage_service,
    throttle,
)
from api.schemas import (
    FileBulkDelete,
    FileCreate,
    FileOut,
    FilePage,
    FileUpdate,
    MessageResponse,
    ShareCreate,
    ShareOut,
    StreamOut,
)
from api.serializers import serialize_file, serialize_share
from config import Settings
from db.models import File as FileRecord
from db.models import FileCategory, User
from services.exceptions import (
    DomainError,
    FileNotFoundError,
    FolderNotFoundError,
    LinkNotFoundError,
    NotOwnerError,
)
from services.filetype import effective_type, is_streamable_category
from services.share import ShareService
from services.storage import _UNSET, StorageService
from services.telegram import extract_file_info
from services.vault import get_vault

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["files"])

#: Mini App'dan HTTP orqali yuklash chegarasi. Telegram Bot API ``sendDocument``
#: (cloud) bitta fayl uchun 50 MB qabul qiladi — server baytlarni shu yo'l bilan
#: Telegram'ga uzatadi. Kattaroq fayl uchun foydalanuvchi bot chatiga yuboradi.
MINIAPP_UPLOAD_LIMIT = 50 * 1024 * 1024


CategoryParam = Literal[
    "photo", "video", "audio", "voice", "animation", "document", "archive", "apk", "ipa", "other"
]
SortParam = Literal["created_at", "name", "file_name", "size", "file_size", "views"]
GroupParam = Literal["media", "audio", "document", "archive", "apps", "pinned", "all"]


def _raise_domain(exc: DomainError) -> HTTPException:
    """Convert any domain error into the project's HTTP error envelope."""
    return HTTPException(status_code=exc.status_code, detail=exc.to_dict())


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
@router.get("", response_model=FilePage, summary="List files (legacy-compatible filters)")
async def list_files(
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
    share: Annotated[ShareService, Depends(get_share_service)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    folder_id: Annotated[
        str | None, Query(description="Folder UUID, or 'root' to list root-level files only")
    ] = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
    category: Annotated[CategoryParam | None, Query()] = None,
    group: Annotated[
        GroupParam | None,
        Query(description="🎬 media / 🎵 audio / 📄 document / 📦 archive / 🤖 apps / 📌 pinned"),
    ] = None,
    apps_only: Annotated[bool, Query(description="🤖 APK/IPA tab of the legacy bot")] = False,
    pinned: Annotated[bool, Query(description="📌 Muhimlar tab")] = False,
    sort_by: Annotated[SortParam, Query()] = "created_at",
    sort_desc: Annotated[bool, Query()] = True,
    with_stream_urls: Annotated[bool, Query()] = False,
) -> FilePage:
    """Paginated listing with the legacy tabs (category / apps / pinned)."""
    folder_filter: Any
    if folder_id is None or not folder_id.strip():
        folder_filter = _UNSET          # legacy behaviour: no folder filtering
    elif folder_id.strip().lower() in {"root", "-", "umumiy"}:
        folder_filter = None            # only root-level files
    else:
        try:
            folder_filter = uuid.UUID(folder_id.strip())
        except ValueError as exc:
            # Noto'g'ri UUID server xatosiga (500) emas, aniq 422 ga aylanadi:
            # mijoz qaysi parametr buzilganini ko'radi.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "validation_error",
                    "message": "folder_id noto'g'ri: UUID, 'root' yoki bo'sh qiymat kutiladi.",
                    "details": {"folder_id": folder_id},
                },
            ) from exc

    items, total = await storage.list_files(
        user.telegram_id,
        folder_id=folder_filter,
        page=page,
        page_size=page_size,
        search=search,
        category=FileCategory(category) if category else None,
        group=group,
        apps_only=apps_only,
        pinned_only=pinned,
        sort_by=sort_by,
        sort_desc=sort_desc,
    )
    return FilePage(
        items=[
            serialize_file(
                record,
                settings=settings,
                share=share,
                telegram_id=user.telegram_id,
                with_stream=with_stream_urls,
            )
            for record in items
        ],
        total=total,
        page=page,
        page_size=page_size,
        pages=max(1, -(-total // page_size)),
    )


# ---------------------------------------------------------------------------
# Registration (the HTTP twin of the bot's save_file)
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=FileOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a file already stored by the bot",
    dependencies=[Depends(throttle("api"))],
)
async def register_file(
    payload: FileCreate,
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
    share: Annotated[ShareService, Depends(get_share_service)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> FileOut:
    """Persist Telegram media metadata — the bytes stay on Telegram's servers."""
    try:
        # ``add_file`` dedupe holatida mavjud qatorni qaytaradi (is_new=False),
        # shuning uchun HTTP mijoz bir xil faylni ikki marta yozmaydi.
        stored = await storage.add_file(
            telegram_id=user.telegram_id,
            file_id=payload.file_id,
            file_unique_id=payload.file_unique_id,
            file_name=payload.file_name,
            file_size=payload.file_size,
            mime_type=payload.mime_type,
            folder_id=payload.folder_id,
            caption=payload.caption,
            duration=payload.duration,
            width=payload.width,
            height=payload.height,
            thumbnail_file_id=payload.thumbnail_file_id,
            is_public=payload.is_public,
        )
    except DomainError as exc:  # duplicate / quota / too large / bad folder
        raise _raise_domain(exc) from exc

    return serialize_file(
        stored.record, settings=settings, share=share, telegram_id=user.telegram_id
    )


# ---------------------------------------------------------------------------
# Mini App'dan haqiqiy yuklash (multipart -> Telegram relay)
# ---------------------------------------------------------------------------
@router.post(
    "/upload",
    response_model=FileOut,
    status_code=status.HTTP_201_CREATED,
    summary="⬆️ Upload a file from the Mini App (relayed to Telegram)",
    dependencies=[Depends(throttle("api"))],
)
async def upload_file(
    request: Request,
    response: Response,
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
    share: Annotated[ShareService, Depends(get_share_service)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    file: Annotated[UploadFile, File(description="Yuklanadigan fayl")],
    folder_id: Annotated[str | None, Form(description="Papka UUID (ixtiyoriy)")] = None,
) -> FileOut:
    """Mini App'dan **haqiqiy** yuklash.

    Mini App brauzerdagidek ``multipart/form-data`` yuboradi; server baytlarni
    qabul qilib, Telegram Bot API (``sendDocument``) orqali foydalanuvchining
    bot bilan chatiga uzatadi va qaytgan ``file_id`` ni bazaga yozadi.

    Nega relay kerak: Telegram WebApp fayl baytlarini chatga to'g'ridan-to'g'ri
    yuborish API'sini bermaydi (``sendData`` faqat matn). ``sendDocument``
    chegarasi — 50 MB; kattaroq fayl uchun Mini App foydalanuvchini bot
    chatiga yo'naltiradi (u yerda 2 GB gacha mumkin).
    """
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "upload_unavailable",
                "message": "Upload relay ishlamayapti (bot ishga tushmagan).",
            },
        )

    # 1) Tez tekshiruv: multipart hajmi ma'lum bo'lsa darhol rad etamiz.
    declared = int(file.size or 0)
    if declared > MINIAPP_UPLOAD_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": "file_too_large_for_upload",
                "message": (
                    "Mini App orqali yuklash chegarasi "
                    f"{MINIAPP_UPLOAD_LIMIT // (1024 * 1024)} MB. "
                    "Kattaroq faylni bot chatiga yuboring."
                ),
                "limit": MINIAPP_UPLOAD_LIMIT,
            },
        )

    # 2) Papka (ixtiyoriy) — mavjudligi va egasi tekshiriladi.
    folder_uuid: uuid.UUID | None = None
    if folder_id and folder_id.strip():
        try:
            folder_uuid = uuid.UUID(folder_id.strip())
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "validation_error",
                    "message": "folder_id noto'g'ri: UUID kutiladi.",
                    "details": {"folder_id": folder_id},
                },
            ) from exc
        await storage.get_folder(folder_uuid, user.telegram_id)

    safe_name = FileRecord.sanitize_filename(
        file.filename or "upload.bin", fallback="upload.bin"
    )
    tmp_path: str | None = None
    try:
        # 3) Baytlarni vaqtinchalik faylga oqizamiz (xotirani to'ldirmaslik uchun).
        suffix = os.path.splitext(safe_name)[1][:16]
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            total = 0
            hasher = hashlib.sha256()
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                hasher.update(chunk)
                if total > MINIAPP_UPLOAD_LIMIT:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail={
                            "code": "file_too_large_for_upload",
                            "message": "Fayl 50 MB chegarasidan katta.",
                            "limit": MINIAPP_UPLOAD_LIMIT,
                        },
                    )
                tmp.write(chunk)

        if declared > 0 and declared != total:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "upload_size_mismatch",
                    "message": "E'lon qilingan hajm haqiqiy hajmga mos emas.",
                },
            )

        response.headers["X-File-SHA256"] = hasher.hexdigest()

        # 4) Kvota tekshiruvi (aniq hajm bilan).
        try:
            storage.ensure_fits(user, total)
        except DomainError as exc:
            raise _raise_domain(exc) from exc

        # 5) Telegram'ga uzatish — fayl foydalanuvchi chatiga qaytadi.
        try:
            sent = await bot.send_document(
                chat_id=user.telegram_id,
                document=FSInputFile(tmp_path, filename=safe_name),
            )
        except Exception as exc:  # Telegram xatosi (forbidden, too large, tarmoq)
            logger.warning("Mini App upload relay failed for %s: %s", user.telegram_id, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "code": "upload_failed",
                    "message": (
                        "Telegram faylni qabul qilmadi. "
                        "Botga /start yuborganingizni tekshiring."
                    ),
                },
            ) from exc

        info = extract_file_info(sent)
        if info is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": "upload_failed", "message": "Telegram faylni qabul qilmadi."},
            )

        # 6) Bazaga yozamiz (dublikat bo'lsa mavjud yozuv qaytadi). Temp fayl
        #    hali diskda: ``add_file`` kontent bo'yicha tur aniqlash uchun
        #    ishlatadi (kengaytmasiz ``tmp_3295`` + ``octet-stream`` holati).
        try:
            stored = await storage.add_file(
                telegram_id=user.telegram_id,
                folder_id=folder_uuid,
                content_path=tmp_path,
                **info.to_kwargs(),
            )
        except DomainError as exc:
            raise _raise_domain(exc) from exc

        vault = get_vault(settings=settings)
        if vault is not None and vault.enabled:
            result = await vault.archive_message(
                sent, file_uuid=stored.record.id, telegram_id=user.telegram_id,
                file_name=stored.record.file_name, caption=stored.record.caption,
            )
            if result.ok:
                try:
                    await bot.delete_message(chat_id=user.telegram_id, message_id=sent.message_id)
                except Exception as exc:
                    logger.warning("service message cleanup failed for %s: %s", user.telegram_id, exc)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return serialize_file(
        stored.record, settings=settings, share=share, telegram_id=user.telegram_id
    )


@router.post(
    "/bulk-delete", response_model=MessageResponse, summary="Delete many files at once"
)
async def bulk_delete(
    payload: FileBulkDelete,
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
) -> MessageResponse:
    """Batch delete for the Mini App multi-select.

    Declared **before** ``/{file_id}`` so the literal path wins over the UUID
    parameter during routing.
    """
    removed = await storage.delete_files_bulk(payload.ids, user.telegram_id)
    return MessageResponse(ok=True, message=f"{removed} file(s) deleted")


# ---------------------------------------------------------------------------
# Single file
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 🗑 Savat (trash): ro'yxat / tiklash / yakuniy o'chirish
# ---------------------------------------------------------------------------
@router.get("/trash", response_model=FilePage, summary="🗑 Savatdagi fayllar")
async def list_trash(
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
    share: Annotated[ShareService, Depends(get_share_service)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> FilePage:
    """Soft-delete qilingan fayllar (eng so'nggi o'chirilganlar birinchi)."""
    items, total = await storage.list_trash(user.telegram_id, page=page, page_size=page_size)
    pages = max(1, (total + page_size - 1) // page_size)
    return FilePage(
        items=[
            serialize_file(
                record,
                settings=settings,
                share=share,
                telegram_id=user.telegram_id,
                with_stream=False,  # savatdagi fayl oqizilmaydi (404 bo'lardi)
            )
            for record in items
        ],
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )



@router.get("/{file_id}", response_model=FileOut, summary="File metadata")
async def get_file(
    file_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
    share: Annotated[ShareService, Depends(get_share_service)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> FileOut:
    """Return one file with its share links and (when streamable) a play URL."""
    try:
        record = await storage.get_file(file_id, user.telegram_id)
    except (FileNotFoundError, NotOwnerError) as exc:
        raise _raise_domain(exc) from exc
    return serialize_file(record, settings=settings, share=share, telegram_id=user.telegram_id)


@router.patch("/{file_id}", response_model=FileOut, summary="Rename / move / visibility")
async def update_file(
    file_id: uuid.UUID,
    payload: FileUpdate,
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
    share: Annotated[ShareService, Depends(get_share_service)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> FileOut:
    """Apply a partial update; ownership is re-checked by the service layer."""
    try:
        record = await storage.get_file(file_id, user.telegram_id)

        if payload.name is not None:
            record = await storage.rename_file(file_id, user.telegram_id, payload.name)

        if payload.move_to_root or payload.folder_id is not None:
            destination = None if payload.move_to_root else payload.folder_id
            record = await storage.move_file(file_id, user.telegram_id, destination)

        if payload.is_public is not None:
            record = await storage.set_visibility(file_id, user.telegram_id, payload.is_public)

        if payload.caption is not None:
            record.caption = payload.caption[:1024]
            storage.session.add(record)
            await storage.session.commit()

        # UPDATE'dan keyin ``updated_at`` (server onupdate) expired bo'ladi;
        # uni sync o'qish async route'da MissingGreenlet bilan yiqilardi.
        # Bitta aniq refresh hamma mutatsiya yo'llarini qoplaydi.
        await storage.session.refresh(record)
    except (FileNotFoundError, FolderNotFoundError, NotOwnerError) as exc:
        raise _raise_domain(exc) from exc

    return serialize_file(record, settings=settings, share=share, telegram_id=user.telegram_id)


@router.delete("/{file_id}", response_model=MessageResponse, summary="Delete a file")
async def delete_file(
    file_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
) -> MessageResponse:
    """🗑 Savatga tashlash (soft delete) — ``restore`` bilan qaytariladi."""
    try:
        freed = await storage.delete_file(file_id, user.telegram_id)
    except (FileNotFoundError, NotOwnerError) as exc:
        raise _raise_domain(exc) from exc
    return MessageResponse(ok=True, message=f"Moved to trash, freed {freed} bytes")


@router.post("/{file_id}/restore", response_model=FileOut, summary="🗑→📁 Savatdan tiklash")
async def restore_file(
    file_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
    share: Annotated[ShareService, Depends(get_share_service)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> FileOut:
    """``deleted_at`` tozalanadi, kvota qayta band bo'ladi (idempotent)."""
    try:
        record = await storage.restore_file(file_id, user.telegram_id)
    except (FileNotFoundError, NotOwnerError) as exc:
        raise _raise_domain(exc) from exc
    return serialize_file(record, settings=settings, share=share, telegram_id=user.telegram_id)


@router.delete(
    "/{file_id}/purge",
    response_model=MessageResponse,
    summary="🗑❌ Savatdan butunlay o'chirish",
)
async def purge_file(
    file_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
) -> MessageResponse:
    """Qator butunlay o'chadi (Telegram'dagi obyekt saqlanib qoladi)."""
    try:
        freed = await storage.purge_file(file_id, user.telegram_id)
    except (FileNotFoundError, NotOwnerError) as exc:
        raise _raise_domain(exc) from exc
    return MessageResponse(ok=True, message=f"Purged, {freed} bytes")


@router.post("/{file_id}/pin", response_model=FileOut, summary="📌 Toggle 'muhim'")
async def toggle_pin(
    file_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
    share: Annotated[ShareService, Depends(get_share_service)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> FileOut:
    """The legacy ``pin:``/``unpin:`` action exposed over HTTP."""
    try:
        record = await storage.toggle_pin(file_id, user.telegram_id)
    except (FileNotFoundError, NotOwnerError) as exc:
        raise _raise_domain(exc) from exc
    return serialize_file(record, settings=settings, share=share, telegram_id=user.telegram_id)


@router.get("/{file_id}/stream", response_model=StreamOut, summary="Signed playback URLs")
async def file_stream_urls(
    request: Request,
    file_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
    share: Annotated[ShareService, Depends(get_share_service)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    ttl: Annotated[int, Query(ge=30, le=3600)] = 300,
) -> StreamOut:
    """Issue short-lived stream/download tickets for the Mini App player."""
    try:
        record = await storage.get_file(file_id, user.telegram_id)
    except (FileNotFoundError, NotOwnerError) as exc:
        raise _raise_domain(exc) from exc

    if not settings.storage.stream_proxy_enabled:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "code": "streaming_disabled",
                "message": "STREAM_PROXY_ENABLED=false - download through the bot instead",
            },
        )

    # 20 MB devori: chipta berishdan **oldin** rad etamiz — Mini App darhol
    # "Telegram'da ochish" tugmasini ko'rsatadi, keyinroq /stream'da kutilmagan
    # xato olmaydi (Bot API getFile ~20 MB dan katta faylni bermaydi).
    from api.routes.stream import ensure_streamable_size

    try:
        ensure_streamable_size(record, settings)
    except HTTPException as exc:
        # Faqat 20 MB devori uchun "o'rin boshlash" yo'li bor.
        if exc.status_code != status.HTTP_413_REQUEST_ENTITY_TOO_LARGE:
            raise
        bot = getattr(request.app.state, "bot", None)
        if not (settings.mt.configured and bot is not None):
            raise
            
        from services.vault import ensure_archived_for_stream
        
        archived = await ensure_archived_for_stream(
            record, settings=settings, storage=storage, bot=bot
        )
        if not archived:
            raise

    ticket = share.issue_stream_ticket(record.id, user.telegram_id, ttl=ttl)
    # Pleer ``<source type="...">`` uchun aniq mime; kengaytmasiz yozuvlarda ham
    # to'g'ri bo'lishi uchun ``effective_type`` dan foydalanamiz.
    mime, category = effective_type(record)
    return StreamOut(
        stream_url=share.stream_url(ticket),
        download_url=share.stream_url(ticket, download=True),
        ticket_expires_in=ticket.seconds_left,
        name=record.file_name,
        size=record.file_size,
        category=category.value,
        mime_type=mime,
        is_streamable=is_streamable_category(category),
    )


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------
@router.post(
    "/{file_id}/share",
    response_model=ShareOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a share link",
)
async def create_share(
    file_id: uuid.UUID,
    payload: ShareCreate,
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
    share: Annotated[ShareService, Depends(get_share_service)],
) -> ShareOut:
    """Create a public link, optionally protected by a generated pass code.

    The clear-text code is returned **once** here; only its salted hash is
    persisted, so it can never be recovered afterwards.
    """
    pass_code = payload.pass_code
    if payload.generate_pass_code and not pass_code:
        pass_code = share.new_pass_code(6)

    try:
        await storage.get_file(file_id, user.telegram_id)      # ownership check
        link = await share.create_link(
            file_id,
            user.telegram_id,
            ttl_seconds=payload.ttl_seconds,
            pass_code=pass_code,
            make_public=payload.make_public,
        )
    except DomainError as exc:
        raise _raise_domain(exc) from exc

    rendered = serialize_share(link, share)
    if pass_code:
        rendered = rendered.model_copy(update={"pass_code": pass_code})
    logger.info("Share link %s created for file %s", link.token[:8], file_id)
    return rendered


@router.get("/{file_id}/shares", response_model=list[ShareOut], summary="List share links")
async def list_shares(
    file_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
    share: Annotated[ShareService, Depends(get_share_service)],
    active_only: Annotated[bool, Query()] = False,
) -> list[ShareOut]:
    """All links of one file (newest first)."""
    try:
        record = await storage.get_file(file_id, user.telegram_id)
        links = await share.list_links(record.id, user.telegram_id, active_only=active_only)
    except (FileNotFoundError, NotOwnerError) as exc:
        raise _raise_domain(exc) from exc
    return [serialize_share(link, share) for link in links]


@router.delete(
    "/{file_id}/shares/{token}", response_model=MessageResponse, summary="Revoke a share link"
)
async def revoke_share(
    file_id: uuid.UUID,
    token: str,
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
    share: Annotated[ShareService, Depends(get_share_service)],
) -> MessageResponse:
    """Soft-delete a link (statistics survive)."""
    try:
        await storage.get_file(file_id, user.telegram_id)
        await share.revoke_link(token, user.telegram_id)
    except (LinkNotFoundError, FileNotFoundError, NotOwnerError) as exc:
        raise _raise_domain(exc) from exc
    return MessageResponse(ok=True, message="Link revoked")
