"""FSM states shared by handlers and middlewares.

Kept in one module so middlewares (which cannot import handlers) and handlers
agree on the same state objects - aiogram compares states by identity.

Three groups exist:

* :class:`PasswordState` - the legacy shared-password gate (``BOT_PASSWORD``);
* :class:`AuthState`     - per-account ``/register`` + ``/login`` dialogs;
* :class:`AdminState`    - the ``/diyor`` admin panel flows (broadcast, prompts);
* :class:`ImportState`   - the Mini App "Import" button flow (share link/token).

:class:`~bot.middlewares.auth_guard.AuthGuardMiddleware` lets updates through
while any of these groups is active, otherwise a dialog would block itself.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup

__all__ = ["AdminState", "AuthState", "ImportState", "PasswordState"]


class PasswordState(StatesGroup):
    """🔐 Legacy password gate (``BOT_PASSWORD``).

    The original bot kept an in-memory ``authorized_users`` set and asked for the
    password on every restart. Here the same flow persists the flag on the user
    row (``users.is_authorized``), so a redeploy does not lock everybody out.
    """

    waiting_password = State()


class AuthState(StatesGroup):
    """👤 Per-account authentication dialogs (``/register``, ``/login``).

    Both commands accept arguments inline (``/login alisher parol``); the states
    below are only used when the arguments are missing, so the bot can ask for
    the login and then the password in two steps.
    """

    #: Kutish: foydalanuvchi login nomini yozadi.
    waiting_login = State()
    #: Kutish: foydalanuvchi parolni yozadi.
    waiting_password = State()
    #: Joriy parolni tasdiqlash (``/password`` oqimi).
    waiting_old_password = State()
    #: Yangi parol kiritilmoqda.
    waiting_new_password = State()


class AdminState(StatesGroup):
    """🛡️ ``/diyor`` admin panel flows."""

    #: Admin paroli so'ralmoqda (``ADMIN_PASSWORD`` sozlangan bo'lsa).
    waiting_password = State()
    #: 📢 E'lon: administrator xabar/matn yuborishini kutamiz.
    waiting_broadcast = State()
    #: 👥 Foydalanuvchi ID sini kutamiz (bloklash/parol tiklash).
    waiting_user_id = State()
    #: Tanlangan foydalanuvchi uchun yangi parol kiritilmoqda.
    waiting_new_password = State()
    #: ⚙️ Sozlamalar: kvota qiymati kiritilmoqda.
    waiting_quota = State()


class ImportState(StatesGroup):
    """📥 Mini App -> Import oqimi.

    Mini App'dagi "Import" tugmasi bot'ga ``web_app_data`` yuboradi; bot
    foydalanuvchidan ulashish havolasi yoki token'ini so'raydi va shu holatga
    o'tadi. Havola kelgach fayl ochiladi va **nusxasi bulutga saqlanadi**
    (dedup: ``file_unique_id`` mavjud bo'lsa qayta yuklanmaydi).

    :class:`~bot.middlewares.auth_guard.AuthGuardMiddleware` bu holatni ruxsat
    etilgan dialog deb hisoblaydi — aks holda import oqimi o'zi darvozaga
    tiqilib qolardi.
    """

    #: Kutish: foydalanuvchi havola yoki token yuboradi.
    waiting_token = State()
