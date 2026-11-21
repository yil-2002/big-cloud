"""🔐 Boshqaruv paneli oddiy foydalanuvchidan **yashiringanini** tekshirish.

Panel parol bilan ochiladi va uning mavjudligi foydalanuvchiga hech qanday
yo'l bilan bildirilmasligi kerak:

* ``/diyor`` Telegram buyruqlar menyusida **ko'rinmaydi**;
* ``/help`` ro'yxatida yo'q;
* parol so'rovi "admin" so'zini ishlatmaydi;
* asosiy ekranda admin yorlig'i chiqmaydi;
* sessiya faqat parol to'g'ri kiritilgandan keyin beriladi va muddati tugaydi.
"""

from __future__ import annotations

import time

import pytest

from bot import admin_session, texts
from bot.setup import BOT_COMMANDS


@pytest.fixture(autouse=True)
def _clean_sessions():
    """Har testdan oldin/keyin sessiyalarni tozalash (testlar bir-biriga ta'sir qilmasin)."""
    admin_session.clear()
    yield
    admin_session.clear()


# ---------------------------------------------------------------------------
# 1. Buyruq menyusi va yordam
# ---------------------------------------------------------------------------
def test_panel_command_is_not_advertised_in_bot_menu() -> None:
    """``/diyor`` ``set_my_commands`` ro'yxatida bo'lmasligi kerak."""
    names = [name for name, _ in BOT_COMMANDS]
    assert "diyor" not in names
    assert "admin" not in names


def test_help_list_has_no_panel_command() -> None:
    """``/help`` matni ``COMMANDS`` dan quriladi — unda panel buyrug'i yo'q."""
    assert "diyor" not in [name for name, _ in texts.COMMANDS]


def test_no_command_description_mentions_administrator() -> None:
    """Birorta buyruq tavsifida "administrator"/"admin" so'zi bo'lmasin."""
    blob = " ".join(description for _, description in BOT_COMMANDS).lower()
    assert "administrator" not in blob
    assert "admin" not in blob


# ---------------------------------------------------------------------------
# 2. Parol so'rovi panelni oshkor qilmaydi
# ---------------------------------------------------------------------------
def test_password_prompt_does_not_reveal_the_panel() -> None:
    """Parol so'rovi va xato matni "admin" so'zini ishlatmasligi kerak."""
    assert "admin" not in texts.admin_password_needed().lower()
    assert "admin" not in texts.admin_password_wrong().lower()


def test_dashboard_shows_no_admin_mark(user) -> None:
    """Asosiy ekran admin yorlig'ini chiqarmasligi kerak (``is_admin=True`` bo'lsa ham)."""
    from bot import texts_uz

    body = texts_uz.dashboard(
        user, {"quota": 0, "used": 0}, bot_username="testbot", is_admin=True
    )
    assert "admin" not in body.lower()


# ---------------------------------------------------------------------------
# 3. Sessiya mexanikasi
# ---------------------------------------------------------------------------
def test_session_is_inactive_by_default() -> None:
    assert admin_session.is_active(987654321) is False


def test_grant_makes_session_active() -> None:
    admin_session.grant(987654321)
    assert admin_session.is_active(987654321) is True


def test_revoke_ends_session() -> None:
    admin_session.grant(987654321)
    admin_session.revoke(987654321)
    assert admin_session.is_active(987654321) is False


def test_zero_ttl_does_not_grant_access() -> None:
    admin_session.grant(987654321, ttl=0)
    assert admin_session.is_active(987654321) is False


def test_session_expires_after_ttl() -> None:
    admin_session.grant(987654321, ttl=1)
    assert admin_session.is_active(987654321) is True
    time.sleep(1.05)
    assert admin_session.is_active(987654321) is False


def test_none_is_never_active() -> None:
    assert admin_session.is_active(None) is False


def test_ttl_remaining_reports_countdown() -> None:
    admin_session.grant(987654321, ttl=60)
    assert 0 < admin_session.ttl_remaining(987654321) <= 60
    assert admin_session.ttl_remaining(111222333) == 0


def test_active_count_ignores_expired_sessions() -> None:
    admin_session.grant(111)
    admin_session.grant(222)
    admin_session.grant(333, ttl=0)
    assert admin_session.active_count() == 2


# ---------------------------------------------------------------------------
# 4. Panelga ruxsat faqat sessiya bilan
# ---------------------------------------------------------------------------
def test_panel_access_follows_the_password_session(settings) -> None:
    """``_is_admin`` parol sessiyasini hisobga oladi (``ADMIN_ID`` bo'sh bo'lsa ham)."""
    from bot.handlers.admin import _is_admin

    telegram_id = 909090909
    assert _is_admin(telegram_id, settings) is False
    admin_session.grant(telegram_id)
    assert _is_admin(telegram_id, settings) is True
    admin_session.revoke(telegram_id)
    assert _is_admin(telegram_id, settings) is False


# ---------------------------------------------------------------------------
# 5. ``/diyor`` oqimi: parol so'raydi, admin haqida hech narsa demaydi
# ---------------------------------------------------------------------------
class _FakeMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs) -> None:
        self.answers.append(text)


class _FakeState:
    def __init__(self) -> None:
        self.current: object = None

    async def set_state(self, value: object) -> None:
        self.current = value


class _FakeUser:
    def __init__(self, telegram_id: int = 424242) -> None:
        self.telegram_id = telegram_id
        self.is_premium = False


async def test_diyor_asks_for_a_password_instead_of_rejecting(settings, monkeypatch) -> None:
    """Parol sozlangan bo'lsa, hatto notanish foydalanuvchi ham parol so'rovini oladi."""
    from pydantic import SecretStr

    from bot.handlers.admin import cmd_diyor
    from bot.states import AdminState

    monkeypatch.setattr(settings.bot, "admin_password", SecretStr("test-parol"))
    message = _FakeMessage()
    state = _FakeState()

    await cmd_diyor(message, _FakeUser(), settings, object(), state)

    assert state.current == AdminState.waiting_password
    assert len(message.answers) == 1
    assert "admin" not in message.answers[0].lower()


async def test_diyor_is_silent_when_the_panel_is_not_configured(settings, monkeypatch) -> None:
    """Parol ham, ``ADMIN_ID`` ham bo'lmasa javob yo'q — panel borligi oshkor bo'lmasin."""
    from pydantic import SecretStr

    from bot.handlers.admin import cmd_diyor

    monkeypatch.setattr(settings.bot, "admin_password", SecretStr(""))
    monkeypatch.setattr(settings.bot, "admin_ids", ())
    monkeypatch.setattr(settings.bot, "admin_id", None)

    message = _FakeMessage()
    state = _FakeState()

    await cmd_diyor(message, _FakeUser(1234567), settings, object(), state)

    assert message.answers == [], "panel sozlanmagan bo'lsa jim qolish kerak"
    assert state.current is None


async def test_diyor_opens_directly_for_a_granted_session(settings, monkeypatch) -> None:
    """Parol bir marta kiritilgach, sessiya davomida qayta so'ralmaydi."""
    from pydantic import SecretStr

    from bot.handlers.admin import cmd_diyor

    monkeypatch.setattr(settings.bot, "admin_password", SecretStr("test-parol"))
    telegram_id = 555000111
    admin_session.grant(telegram_id)

    message = _FakeMessage()
    state = _FakeState()

    await cmd_diyor(message, _FakeUser(telegram_id), settings, object(), state)

    assert state.current is None, "parol qayta so'ralmasligi kerak"
    assert len(message.answers) == 1, "panel menyusi yuborilishi kerak"
