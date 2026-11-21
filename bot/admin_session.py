"""Vaqtinchalik boshqaruv sessiyasi (``/diyor`` parol darvozasi).

Nega kerak
----------
Panelga kirish ikki xil bo'lishi mumkin:

1. **Telegram ID** orqali (``ADMIN_ID``/``ADMIN_IDS``) — doimiy, ``.env`` da
   saqlanadi va sukut bo'yicha **bo'sh**. Bo'sh bo'lsa hech kim admin emas.
2. **Parol** orqali (``ADMIN_PASSWORD``) — foydalanuvchi ``/diyor`` yozadi, bot
   parol so'raydi, to'g'ri kiritilsa shu sessiyaga ruxsat beriladi.

Ikkinchi yo'l uchun "kim parol kiritgan" ma'lumotini qayerdadir saqlash kerak:
panel tugmalari (``callback_query``) keyinroq keladi va ularda FSM holati
ishonchsiz. Shu sababli har bir tasdiqlangan foydalanuvchi uchun TTL'li yozuv
saqlanadi.

Cheklov
-------
Bu **jarayon ichidagi** (in-memory) saqlash. Bot bitta jarayonda va bitta
worker bilan ishlaydi (``RUN_MODE=all``, ``API_WORKERS=1``), shuning uchun bu
yetarli. Bir nechta worker'ga o'tsangiz, sessiyani Redis'ga
(:func:`services.cache.get_cache`) ko'chirish kerak — aks holda tugma bosgan
worker sessiyani ko'rmaydi.

Xavfsizlik
----------
* ``revoke()`` — sessiyani darhol bekor qilish (panel ichidagi "chiqish").
* Bot qayta ishga tushsa barcha sessiyalar yo'qoladi — bu **xavfsiz** yo'nalish:
  har restartdan keyin parol qaytadan so'raladi.
"""

from __future__ import annotations

import threading
import time

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "active_count",
    "clear",
    "grant",
    "is_active",
    "revoke",
    "ttl_remaining",
]

#: Sessiya amal qilish muddati (sekund). 12 soat — bir ish kuni uchun qulay,
#: lekin cheksiz emas.
DEFAULT_TTL_SECONDS: int = 12 * 60 * 60

#: ``telegram_id -> tugash vaqti (monotonic)``.
_sessions: dict[int, float] = {}
_lock = threading.Lock()


def grant(telegram_id: int, ttl: int = DEFAULT_TTL_SECONDS) -> None:
    """Foydalanuvchiga vaqtinchalik boshqaruv ruxsatini berish.

    Parol to'g'ri kiritilgandan **keyin** chaqiriladi. ``ttl`` sekundlarda;
    ``0`` yoki manfiy qiymat sessiyani darhol tugatadi.
    """
    expires_at = time.monotonic() + ttl
    with _lock:
        if ttl <= 0:
            _sessions.pop(telegram_id, None)
            return
        _sessions[telegram_id] = expires_at


def revoke(telegram_id: int) -> None:
    """Sessiyani bekor qilish (foydalanuvchi panelni tark etganda)."""
    with _lock:
        _sessions.pop(telegram_id, None)


def is_active(telegram_id: int | None) -> bool:
    """Shu foydalanuvchida amal qiluvchi sessiya bormi.

    Muddati o'tgan yozuv shu yerda tozalanadi (fon vazifasi kerak emas).
    """
    if telegram_id is None:
        return False
    now = time.monotonic()
    with _lock:
        expires_at = _sessions.get(telegram_id)
        if expires_at is None:
            return False
        if expires_at <= now:
            _sessions.pop(telegram_id, None)
            return False
        return True


def ttl_remaining(telegram_id: int) -> int:
    """Sessiya tugashiga qolgan sekund (sessiya yo'q bo'lsa ``0``)."""
    now = time.monotonic()
    with _lock:
        expires_at = _sessions.get(telegram_id)
        if expires_at is None or expires_at <= now:
            return 0
        return max(0, int(expires_at - now))


def active_count() -> int:
    """Amal qiluvchi sessiyalar soni (admin statistikasi uchun)."""
    now = time.monotonic()
    with _lock:
        stale = [key for key, expires_at in _sessions.items() if expires_at <= now]
        for key in stale:
            _sessions.pop(key, None)
        return len(_sessions)


def clear() -> None:
    """Barcha sessiyalarni tozalash (testlar va qayta ishga tushirish uchun)."""
    with _lock:
        _sessions.clear()
