"""📱 Mini App ko'prigi — ``web_app_data`` va **Import** oqimi.

Mini App Pivot talabiga ko'ra asosiy UX ilovaga ko'chdi. Lekin fayl baytlari
**Telegram serverlarida** qolishi kerak (loyihaning o'zgarmas qoidasi: bizda
faqat ``file_id`` saqlanadi). Shu sababli Mini App ikki xil so'rov yuboradi:

=========================  =====================================================
``web_app_data`` turi        bot harakati
=========================  =====================================================
``hugecloud.upload``        Yuklash yo'lini tushuntiradi: foydalanuvchi faylni
                           shu chatga yuboradi, ``upload`` handler'i esa uni
                           ``file_unique_id`` bo'yicha **dedup** bilan yozadi.
``hugecloud.import``        :class:`~bot.states.ImportState` ga o'tadi va
                           ulashish havolasi/token'ini kutadi; kelgach fayl
                           ochiladi **va nusxasi bulutga saqlanadi**.
boshqa                     Xato emas: "amalni yangilang" xabari (eski ilova
                           versiyasi yangi bot bilan ishlashi uchun).
=========================  =====================================================

Import qanday ishlaydi
----------------------
Havola Huge Cloud'ning o'zi yaratgan deep link (``t.me/<bot>?start=<token>``)
yoki API havolasi (``/api/v1/shares/<token>``) bo'lishi mumkin — ikkalasidan
ham token ajratiladi (:func:`extract_share_token`). Keyin
:func:`bot.handlers.start._open_share_token` chaqiriladi: u faylni yuboradi,
nusxasini saqlaydi, takroriy bo'lsa ``dedupe_hit`` xabarini ko'rsatadi va
himoyalangan havola uchun parol so'raydi.

Xavfsizlik
----------
Import **faqat tasdiqlangan** hisoblar uchun ishlaydi (``AuthGuardMiddleware``
``ImportState`` dialogini ruxsat etilgan deb hisoblaydi, lekin hisob
tasdiqlanmagan bo'lsa darvoza uni ushlaydi). Token egaligi va ko'rinarlilik
servis qatlamida qayta tekshiriladi — bu modul ruxsat qarori qabul qilmaydi.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot import texts
from bot.keyboards.reply import cancel_reply_keyboard
from bot.states import ImportState
from bot.utils.formatters import format_size
from config import Settings
from db.models import User
from services.exceptions import DomainError
from services.share import ShareService
from services.storage import StorageService
from services.telegram import TelegramStorage

logger = logging.getLogger(__name__)

router = Router(name="miniapp")

__all__ = [
    "DATA_IMPORT",
    "DATA_PLAY",
    "DATA_UPLOAD",
    "extract_share_token",
    "normalize_data_type",
    "router",
]

#: Mini App yuboradigan ``web_app_data`` turlari (ilova bilan kelishilgan).
DATA_UPLOAD = "hugecloud.upload"
DATA_IMPORT = "hugecloud.import"
#: Faylni chatga yuborish — Telegram native pleerida (fon/qulf ekrani) ijro.
DATA_PLAY = "hugecloud.play"

#: Ilova avvalgi brend nomi bilan yuborgan niyatlar. Telegram WebView sahifani
#: keshlab qolishi mumkin, shuning uchun eski nom bilan kelgan so'rov ham
#: qabul qilinadi (aks holda yuklash/import jimgina ishlamay qolardi).
_LEGACY_DATA_PREFIX = "bigcloud."
_DATA_PREFIX = "hugecloud."


def normalize_data_type(data_type: str) -> str:
    """Eski brend prefiksi bilan kelgan niyatni yangi nomga o'giradi."""
    if data_type.startswith(_LEGACY_DATA_PREFIX):
        return _DATA_PREFIX + data_type[len(_LEGACY_DATA_PREFIX) :]
    return data_type

#: Bekor qilish matnlari (reply klaviatura va qo'lda yozilgan variantlar).
_CANCEL_WORDS: frozenset[str] = frozenset({"cancel", "bekor qilish", "bekor", "отмена"})

#: Token belgilari: URL-safe base64 + chiziqcha (``services.share`` shakli).
_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9_\-]{6,128}$")

#: Ulashish havolasi yo'lida token oldidan keladigan bo'laklar.
_SHARE_PATH_SEGMENTS = frozenset({"shares", "share", "s", "l"})


def extract_share_token(value: str | None) -> str:
    """Foydalanuvchi yuborgan satrdan ulashish token'ini ajratish.

    Qabul qilinadigan shakllar::

        t.me/Huge_cloud_bot?start=AbC123_-xY
        https://t.me/Huge_cloud_bot?start=AbC123_-xY
        https://cloud.example.com/api/v1/shares/AbC123_-xY
        AbC123_-xY                     (shunchaki token)

    Topilmasa bo'sh satr qaytadi (handler "noto'g'ri havola" deb javob beradi).
    """
    if not value:
        return ""
    raw = value.strip()
    if not raw:
        return ""

    # 1) URL bo'lsa — ``?start=`` yoki yo'lning oxirgi bo'lagidan olamiz.
    if "://" in raw or raw.startswith("t.me"):
        candidate = raw if "://" in raw else f"https://{raw.lstrip('/')}"
        try:
            parsed = urlparse(candidate)
        except ValueError:  # pragma: no cover - urlparse kamdan-kam xato qiladi
            parsed = None
        if parsed is not None:
            for token in parse_qs(parsed.query).get("start") or []:
                token = token.strip()
                if _TOKEN_CHARS.match(token):
                    return token
            # Yo'lning oxirgi bo'lagi faqat **ulashish manzili** bo'lsa token
            # hisoblanadi (``/shares/<token>``, ``/s/<token>``). Aks holda
            # ``t.me/Huge_cloud_bot`` kabi havoladan bot nomi "token" bo'lib
            # chiqadi va foydalanuvchi chalkash xato oladi.
            parts = [part for part in parsed.path.split("/") if part]
            if (
                len(parts) >= 2
                and parts[-2].lower() in _SHARE_PATH_SEGMENTS
                and _TOKEN_CHARS.match(parts[-1])
            ):
                return parts[-1]

    # 2) URL emas — ehtimol token'ning o'zi (bo'sh joy bilan ajratilgan bo'lsa,
    #    oxirgi bo'lakni olamiz: "havola: AbC123" kabi matn ham ishlaydi).
    token = raw.split()[-1].strip()
    if _TOKEN_CHARS.match(token):
        return token
    return ""


def _parse_json(raw: str) -> dict[str, Any]:
    """``sendData`` payload'ini xavfsiz o'qish (buzilgan JSON → bo'sh lug'at)."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.debug("web_app_data payload is not valid JSON (%d chars)", len(raw))
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --------------------------------------------------------------------------- #
# Mini App -> bot
# --------------------------------------------------------------------------- #
@router.message(F.web_app_data)
async def on_web_app_data(
    message: Message,
    user: User,
    storage: StorageService,
    share: ShareService,
    tg_storage: TelegramStorage,
    settings: Settings,
    state: FSMContext,
) -> None:
    """``WebApp.sendData(...)`` orqali kelgan so'rovni qayta ishlash."""
    web_app_data = message.web_app_data
    data_type = (web_app_data.data_type or "").strip() if web_app_data else ""
    data_type = normalize_data_type(data_type)
    raw_data = (web_app_data.data or "") if web_app_data else ""
    payload = _parse_json(raw_data)

    logger.info(
        "web_app_data from %s: type=%s bytes=%d",
        user.telegram_id,
        data_type or "<empty>",
        len(raw_data),
    )

    if data_type == DATA_UPLOAD:
        await _handle_upload_intent(message, user, settings, payload)
        return

    if data_type == DATA_IMPORT:
        await _start_import(message, user, state, payload, share, storage, tg_storage, settings)
        return

    if data_type == DATA_PLAY:
        await _handle_play_intent(message, user, storage, tg_storage, payload)
        return

    # Noma'lum tur: xato emas (eski ilova versiyasi yangi bot bilan ishlaydi).
    await message.answer(texts.miniapp_unknown_action())


async def _handle_play_intent(
    message: Message,
    user: User,
    storage: StorageService,
    tg_storage: TelegramStorage,
    payload: dict[str, Any],
) -> None:
    """▶️ "Telegram'da ochish": faylni chatga yuborish.

    Nega kerak: Mini App WebView'i fon rejimida (ekran qulfi, boshqa ilova)
    media ijro eta olmaydi — Telegram buni taqiqlaydi va rasmiy API ham yo'q.
    Yagona ishonchli yo'l: faylni chatga yuborish, shunda Telegram'ning
    **native pleeri** qulf ekranida va fon rejimida davom etadi.

    Xavfsizlik: egalik :meth:`StorageService.get_file` da tekshiriladi — u
    faqat shu foydalanuvchiga tegishli faylni qaytaradi.
    """
    raw_id = payload.get("file_id") or payload.get("id")
    if not raw_id:
        await message.answer(texts.miniapp_play_invalid())
        return

    try:
        record = await storage.get_file(str(raw_id), user.telegram_id)
    except (FileNotFoundError, DomainError, ValueError):
        logger.info(
            "Mini App play request for unknown file %r by %s",
            raw_id,
            user.telegram_id,
        )
        await message.answer(texts.miniapp_play_invalid())
        return

    await message.answer(texts.miniapp_play_ack(record.file_name))

    try:
        await tg_storage.send_stored_file(
            message.chat.id,
            record.best_file_id,
            category=record.category,
            record=record,
        )
    except Exception as exc:  # foydalanuvchiga xabar beramiz
        logger.warning(
            "Could not send %s to chat for native playback: %s", record.id, exc
        )
        await message.answer(texts.miniapp_play_invalid())


async def _handle_upload_intent(
    message: Message, user: User, settings: Settings, payload: dict[str, Any]
) -> None:
    """⬆️ "Yuklash" niyati: faylni chatga yuborishni so'rash.

    Mini App fayl baytlarini to'g'ridan-to'g'ri serverga yuklay olmaydi: bulut
    **Telegram** ustida qurilgan. Shu sababli ilova fayl metadata'sini yuboradi
    (nom/hajm/tur), bot esa aniq ko'rsatma beradi; fayl chatga kelgach
    ``upload`` handler'i uni ``file_unique_id`` bo'yicha dedup bilan yozadi
    (takroriy fayl qayta yuklanmaydi).
    """
    name = str(payload.get("name") or "fayl")[:200]
    size = _as_int(payload.get("size"))
    limit = int(settings.storage.max_file_size or 0)

    if limit and size > limit:
        await message.answer(
            texts.miniapp_upload_limit(format_size(limit)),
            reply_markup=cancel_reply_keyboard(user),
        )
        return

    await message.answer(
        texts.miniapp_upload_ack(name, format_size(size) if size else "—"),
        reply_markup=cancel_reply_keyboard(user),
    )


async def _start_import(
    message: Message,
    user: User,
    state: FSMContext,
    payload: dict[str, Any],
    share: ShareService,
    storage: StorageService,
    tg_storage: TelegramStorage,
    settings: Settings,
) -> None:
    """📥 Import oqimini boshlash (yoki tayyor token bo'lsa darhol bajarish)."""
    # Mini App token'ni oldindan biladigan bo'lsa (masalan "Ulashish" bo'limidan)
    # uni payload'da yuborishi mumkin — bu holda so'rovni takrorlamaymiz.
    token = extract_share_token(str(payload.get("token") or ""))
    await state.clear()

    if not token:
        await state.set_state(ImportState.waiting_token)
        await message.answer(
            texts.miniapp_import_prompt(), reply_markup=cancel_reply_keyboard(user)
        )
        return

    await _open_token(message, token, user, storage, share, tg_storage, settings, state)


async def _open_token(
    message: Message,
    token: str,
    user: User,
    storage: StorageService,
    share: ShareService,
    tg_storage: TelegramStorage,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Import = faylni ochish + nusxasini saqlash (dedup bilan).

    Deep-link yo'lini qayta ishlatamiz: u barcha xato holatlarini (muddati
    tugagan, bekor qilingan, topilmagan, himoyalangan → parol so'rash) o'zi
    boshqaradi, shuning uchun import va deep-link xatti-harakati bir xil.
    """
    from bot.handlers.start import _open_share_token  # lokal import: siklni oldini oladi

    try:
        await _open_share_token(message, token, user, storage, share, tg_storage, settings, state)
    except Exception:
        logger.exception("Import failed for %s", user.telegram_id)
        await message.answer(
            texts.miniapp_import_invalid(), reply_markup=cancel_reply_keyboard(user)
        )


def _as_int(value: Any) -> int:
    """Payload sonini xavfsiz ``int`` ga aylantirish (ilovadan kelgan qiymat)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _is_cancel(text: str) -> bool:
    """Bekor qilish matnimi (buyruq yoki reply klaviatura tugmasi)."""
    if text.startswith("/"):
        return True
    return text.strip().lower().strip("❌ ").lower() in _CANCEL_WORDS


# --------------------------------------------------------------------------- #
# Import FSM: foydalanuvchi havola yubordi
# --------------------------------------------------------------------------- #
@router.message(ImportState.waiting_token, F.text)
async def on_import_token(
    message: Message,
    user: User,
    storage: StorageService,
    share: ShareService,
    tg_storage: TelegramStorage,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Import dialogi: havola/token qabul qilindi."""
    raw = (message.text or "").strip()

    if not raw or _is_cancel(raw):
        await state.clear()
        await message.answer(
            texts.operation_cancelled(), reply_markup=cancel_reply_keyboard(user)
        )
        return

    token = extract_share_token(raw)
    if not token:
        # Holatni saqlab qolamiz: foydalanuvchi havolani qayta yuborishi mumkin.
        await message.answer(
            texts.miniapp_import_invalid(), reply_markup=cancel_reply_keyboard(user)
        )
        return

    await state.clear()
    await _open_token(message, token, user, storage, share, tg_storage, settings, state)
