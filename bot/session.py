"""🚪 Sessiya interseptori — **har bir** sessiya darvozadan o'tadi.

Nima uchun alohida modul
------------------------
Talab: "yangi va mavjud barcha sessiyalarni ushlab, bot imkoniyatlarini
berishdan **oldin** ro'yxatdan o'tish / tizimga kirishni majburiy qilish".

Bu siyosat uchta joyda bir xil bo'lishi kerak:

1. :mod:`bot.handlers.start` — ``/start`` va deep-link (``/start <payload>``);
2. :mod:`bot.middlewares.auth_guard` — boshqa barcha update'lar (xabar, tugma,
   inline) uchun darvoza;
3. :mod:`bot.handlers.auth` — kirishdan **keyin** deep-link'ni davom ettirish.

Uchala joy ham shu moduldan foydalanadi, shuning uchun qoidalar ikki marta
yozilmaydi va bir-biriga zid kelmaydi.

Deep-link uzluksizligi
----------------------
Foydalanuvchi ``t.me/hugecloud_bot?start=<token>`` orqali kelsa va hisobi hali
tasdiqlanmagan bo'lsa, payload **yo'qolmaydi**: u FSM'ga "park" qilinadi
(:func:`park_payload`) va login/register muvaffaqiyatli tugagach darhol
ijro etiladi (:func:`take_parked_payload`). Shu sababli foydalanuvchi
havolani qayta so'ramaydi — oqim uzluksiz.

Xavfsizlik
----------
Park qilingan payload **hech qachon** ruxsat qarori emas: ijro paytida har bir
tarmoq egalik/ko'rinarlilikni servis qatlamida qayta tekshiradi
(``storage.get_file(..., telegram_id)``, ``share.resolve_token(...)``).
Shuning uchun bu modul faqat *uzluksizlik* uchun javob beradi.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any

from aiogram.fsm.context import FSMContext

from db.models import User

logger = logging.getLogger(__name__)

__all__ = [
    "PARKED_PAYLOAD_KEY",
    "SessionDecision",
    "SessionKind",
    "has_parked_payload",
    "is_allowed_without_auth",
    "park_payload",
    "resolve_session",
    "take_parked_payload",
]

#: FSM'da park qilingan deep-link payload saqlanadigan kalit.
PARKED_PAYLOAD_KEY = "pending_start_payload"

#: Payload uzunligi chegarasi (Telegram deep-link'i 64 belgidan oshmaydi, lekin
#: FSM'ga axlat yozilmasligi uchun qat'iy chegaralab qo'yamiz).
MAX_PAYLOAD_LENGTH = 256


class SessionKind(str, enum.Enum):
    """Sessiya turi — darvoza qarorining asosi."""

    #: Hisob bloklangan (``users.is_blocked``) — hech narsa ishlamaydi.
    BLOCKED = "blocked"
    #: Hisob tasdiqlangan — to'liq ruxsat.
    AUTHENTICATED = "authenticated"
    #: Mavjud sessiya, lekin hisob tasdiqlanmagan ("qaytib kelgan mehmon").
    RETURNING_GUEST = "returning_guest"
    #: Birinchi marta kelgan foydalanuvchi (hech qanday fayl/metadata yo'q).
    NEW = "new"


class SessionDecision(str, enum.Enum):
    """Darvoza qarori: o'tkazish yoki to'sish."""

    #: Handler'ni chaqirish mumkin.
    ALLOW = "allow"
    #: Ro'yxatdan o'tish / kirish ekrani ko'rsatilishi kerak.
    REQUIRE_AUTH = "require_auth"
    #: Bloklash xabari ko'rsatilishi kerak.
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class SessionResolution:
    """Sessiya tahlili natijasi (middleware va handler'lar uchun)."""

    kind: SessionKind
    decision: SessionDecision
    #: Foydalanuvchi birinchi marta keldimi (yangi hisob + hech qanday fayl yo'q).
    is_first_contact: bool
    #: ``/diyor`` paneliga ruxsati bormi.
    is_admin: bool
    #: Bloklash sababi (admin izohi) — faqat ``kind is BLOCKED`` da.
    block_reason: str | None = None

    @property
    def allowed(self) -> bool:
        """Handler'ni chaqirish mumkinmi."""
        return self.decision is SessionDecision.ALLOW


#: Darvoza o'chirilgan holat uchun "hamma o'tadi" qarori.
def _allow(kind: SessionKind, *, is_admin: bool, is_first_contact: bool = False) -> SessionResolution:
    return SessionResolution(
        kind=kind,
        decision=SessionDecision.ALLOW,
        is_first_contact=is_first_contact,
        is_admin=is_admin,
    )


def resolve_session(
    user: User | None,
    *,
    gate_enabled: bool,
    is_admin: bool = False,
    has_any_activity: bool = True,
) -> SessionResolution:
    """Sessiya holatini darvoza qaroriga aylantirish (sof funksiya — test qilinadi).

    :param user: ``users`` qatori (``None`` bo'lsa — hodisa foydalanuvchisiz,
        masalan kanal posti; bunday hodisalar tekshirilmaydi).
    :param gate_enabled: ``AUTH_REQUIRED`` (runtime ``system_flags`` ustun).
    :param is_admin: ``ADMIN_ID`` ro'yxatida borligi.
    :param has_any_activity: foydalanuvchida hech bo'lmasa bitta fayl/metadata
        bormi — ``NEW`` va ``RETURNING_GUEST`` ni farqlash uchun.
    """
    if user is None:
        # Foydalanuvchisiz hodisa: darvoza qo'llanilmaydi.
        return _allow(SessionKind.AUTHENTICATED, is_admin=is_admin)

    # 1) Bloklash — eng qat'iy qoida (admin ham tekshiradi, lekin bloklangan
    #    hisob admin bo'la olmaydi; baribir birinchi navbatda ko'ramiz).
    if user.is_blocked:
        return SessionResolution(
            kind=SessionKind.BLOCKED,
            decision=SessionDecision.BLOCKED,
            is_first_contact=not has_any_activity,
            is_admin=is_admin,
            block_reason=user.admin_note,
        )

    # 2) Admin har doim o'tadi (bitta egali deployment uchun muhim).
    if is_admin:
        return _allow(SessionKind.AUTHENTICATED, is_admin=True, is_first_contact=not has_any_activity)

    # 3) Darvoza o'chirilgan — ochiq bot (legacy xatti-harakat).
    if not gate_enabled:
        kind = SessionKind.AUTHENTICATED if user.is_authenticated else SessionKind.RETURNING_GUEST
        return _allow(kind, is_admin=False, is_first_contact=not has_any_activity)

    # 4) Tasdiqlangan hisob — to'liq ruxsat.
    if user.is_authenticated:
        return _allow(
            SessionKind.AUTHENTICATED,
            is_admin=False,
            is_first_contact=not has_any_activity,
        )

    # 5) Tasdiqlanmagan: yangi yoki qaytib kelgan mehmon — darvoza ko'rsatiladi.
    kind = SessionKind.NEW if not has_any_activity else SessionKind.RETURNING_GUEST
    return SessionResolution(
        kind=kind,
        decision=SessionDecision.REQUIRE_AUTH,
        is_first_contact=not has_any_activity,
        is_admin=False,
    )


# --------------------------------------------------------------------------- #
# Deep-link parklash (uzluksiz oqim)
# --------------------------------------------------------------------------- #
def _sanitize_payload(payload: Any) -> str:
    """Payload'ni xavfsiz satrga aylantirish (bo'sh/buzilgan qiymatlar → "")."""
    if payload is None:
        return ""
    text = str(payload).strip()
    # Boshqaruv belgilari va yangi qatorlarni kesamiz: bu qiymat keyinchalik
    # deep-link router'iga beriladi, log'ga ham tushadi.
    text = "".join(char for char in text if char.isprintable())
    return text[:MAX_PAYLOAD_LENGTH]


async def park_payload(state: FSMContext | None, payload: Any) -> str:
    """Deep-link payload'ni login/register tugaguncha saqlab qo'yish.

    Bo'sh payload saqlanmaydi (keyin ``take_parked_payload`` "hech narsa yo'q"
    deb javob beradi va foydalanuvchi oddiy bosh ekranga tushadi).
    """
    cleaned = _sanitize_payload(payload)
    if state is None or not cleaned:
        return ""
    await state.update_data({PARKED_PAYLOAD_KEY: cleaned})
    logger.info("Deep-link payload parked for post-auth replay (%d chars)", len(cleaned))
    return cleaned


async def take_parked_payload(state: FSMContext | None) -> str:
    """Park qilingan payload'ni **o'chirib** qaytarish (bir marta ishlatiladi).

    Login muvaffaqiyatli tugagach chaqiriladi; agar payload bo'lmasa bo'sh satr
    qaytadi va handler oddiy "Mini App bosh ekrani"ni ko'rsatadi.
    """
    if state is None:
        return ""
    data = await state.get_data()
    payload = _sanitize_payload(data.get(PARKED_PAYLOAD_KEY))
    if payload:
        # Bir marta ishlatiladi: takroriy login'da eski havola qayta ochilmasin.
        await state.update_data({PARKED_PAYLOAD_KEY: ""})
        logger.info("Parked deep-link payload replayed (%d chars)", len(payload))
    return payload


async def has_parked_payload(state: FSMContext | None) -> bool:
    """Park qilingan payload bormi (matnda "havolangiz saqlanib turibdi" deyish uchun)."""
    if state is None:
        return False
    data = await state.get_data()
    return bool(_sanitize_payload(data.get(PARKED_PAYLOAD_KEY)))


#: Ruxsat etilgan buyruqlar — darvoza ishlasa ham ishlaydi.
#: ``/start`` deep-link bilan kelishi mumkin, shuning uchun u ham bu yerda.
GUEST_COMMANDS: frozenset[str] = frozenset(
    {
        "/start",
        "/login",
        "/register",
        "/logout",
        "/cancel",
        "/help",
        "/id",
        # ``/diyor`` — panel eshigi; parol sozlanmagan bo'lsa jim qoladi,
        # parol sozlangan bo'lsa parol so'raladi (qarang: auth_guard).
        "/diyor",
    }
)


def is_allowed_without_auth(text: str | None) -> bool:
    """Matn ruxsat etilgan buyruq bilan boshlanadimi (``/start@Huge_cloud_bot`` ham)."""
    if not text:
        return False
    stripped = text.strip()
    if not stripped.startswith("/"):
        return False
    command = stripped.split(maxsplit=1)[0]
    # ``/start@botname`` → ``/start``
    command = command.split("@", 1)[0].lower()
    return command in GUEST_COMMANDS
