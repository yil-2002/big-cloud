"""🔐 Autentifikatsiya oqimi: /register, /login, qulflash, bloklash."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from db.models import AuditAction, AuditLog, File
from services.auth import AuthOutcome, AuthService, validate_login
from tests.conftest import make_file


@pytest.fixture()
def auth(storage, settings) -> AuthService:
    return AuthService(storage.session, storage=storage, settings=settings)


async def test_register_creates_authenticated_account(auth, storage, user) -> None:
    """Ro'yxatdan o'tish: login+parol saqlanadi va darhol autentifikatsiya."""
    result = await auth.register(user.telegram_id, "alisher", "parol123")
    assert result.ok, result.message
    assert user.login == "alisher"
    assert user.is_authenticated is True
    assert user.check_password("parol123") is True
    # Parol bazada ochiq holda saqlanmasligi shart.
    assert "parol123" not in (user.password_hash or "")
    assert user.password_hash.startswith("pbkdf2_sha256$")


async def test_register_rejects_taken_login(auth, storage, user) -> None:
    await auth.register(user.telegram_id, "alisher", "parol123")
    other, _ = await storage.get_or_create_user(telegram_id=888, first_name="Other")
    second = await auth.register(other.telegram_id, "alisher", "boshqa123")
    assert not second.ok
    assert "band" in (second.message or "")


async def test_register_validates_login_and_password(auth, user) -> None:
    assert not (await auth.register(user.telegram_id, "ab", "parol123")).ok      # juda qisqa login
    assert not (await auth.register(user.telegram_id, "admin", "parol123")).ok    # band qilingan
    assert not (await auth.register(user.telegram_id, "alisher", "123")).ok       # qisqa parol


async def test_login_success_and_audit(auth, user) -> None:
    await auth.register(user.telegram_id, "alisher", "parol123")
    user.is_authenticated = False                       # chiqib ketgan holat
    result = await auth.login("alisher", "parol123", telegram_id=user.telegram_id)
    assert result.ok
    assert user.is_authenticated is True
    assert user.last_login_at is not None

    actions = [row.action for row in (await storage_rows(user)) ]
    assert AuditAction.LOGIN_OK in actions


async def storage_rows(user):
    """Audit yozuvlarini o'qish yordamchisi."""
    from sqlalchemy import select


    session = user.session if hasattr(user, "session") else None
    del session
    from db.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        return list((await session.scalars(select(AuditLog))).all())


async def test_login_wrong_password_counts_attempts(auth, user, settings) -> None:
    await auth.register(user.telegram_id, "alisher", "parol123")
    await auth.logout(user.telegram_id)          # chiqib ketgan holatni tiklash
    result = await auth.login("alisher", "noto-gri", telegram_id=user.telegram_id)
    assert result.outcome is AuthOutcome.WRONG_PASSWORD
    assert result.attempts_left == settings.auth.max_attempts - 1
    assert user.is_authenticated is False


async def test_login_locks_after_max_attempts(auth, user, settings) -> None:
    await auth.register(user.telegram_id, "alisher", "parol123")
    for _ in range(settings.auth.max_attempts):
        await auth.login("alisher", "wrong", telegram_id=user.telegram_id)
    assert user.is_locked is True
    locked = await auth.login("alisher", "parol123", telegram_id=user.telegram_id)
    assert locked.outcome is AuthOutcome.LOCKED


async def test_login_unknown_and_blocked(auth, storage, user) -> None:
    unknown = await auth.login("yoq", "parol123")
    assert unknown.outcome is AuthOutcome.UNKNOWN_LOGIN

    await auth.register(user.telegram_id, "alisher", "parol123")
    await storage.set_blocked(user.telegram_id, True, note="spam")
    blocked = await auth.login("alisher", "parol123", telegram_id=user.telegram_id)
    assert blocked.outcome is AuthOutcome.BLOCKED


# --------------------------------------------------------------------------- #
# Ko'p profilli sessiyalar: profilga o'tish (fayllar KO'CHIRILMAYDI),
# muvaffaqiyatli logindan keyin eski sessiya yakunlanadi.
# --------------------------------------------------------------------------- #
async def _file_count(storage, owner_id: int) -> int:
    return int(
        await storage.session.scalar(
            select(func.count()).select_from(File).where(File.user_id == owner_id)
        )
        or 0
    )


async def _resolve(storage, telegram_id: int):
    """Middleware bilan bir xil: Telegram foydalanuvchisining aktiv profili."""
    from services.profiles import ProfileService

    home = await storage.get_user_any(telegram_id)
    return await ProfileService(storage.session).resolve(home)


async def test_login_from_other_telegram_switches_profile_without_merging(auth, storage, user) -> None:
    """B Telegram A profiliga kiradi: B o'sha profilga o'tadi, hech narsa birlashmaydi."""
    a_id, b_id = user.telegram_id, 888
    await auth.register(a_id, "alisher", "parol123")
    await make_file(storage, a_id, name="alisher.pdf")
    await storage.get_or_create_user(telegram_id=b_id, first_name="Bobur")
    await auth.register(b_id, "bobur", "parol456")
    await make_file(storage, b_id, name="bobur.pdf")

    result = await auth.login("alisher", "parol123", telegram_id=b_id)
    assert result.ok, result.message
    assert result.user.telegram_id == a_id              # profil o'zi, ko'chirilmagan
    assert await storage.get_user_any(a_id) is not None  # qator o'chmagan
    # Fayllar har profilda alohida qoladi.
    assert await _file_count(storage, a_id) == 1
    assert await _file_count(storage, b_id) == 1

    active_for_b = await _resolve(storage, b_id)
    assert active_for_b.telegram_id == a_id and active_for_b.is_signed_in
    # B ning o'z profili ("bobur") sessiyasi yakunlandi.
    assert (await storage.get_user_any(b_id)).is_authenticated is False


async def test_new_login_ends_the_old_session(auth, storage, user) -> None:
    """Profilga boshqa joydan kirilsa, eski egasining sessiyasi yakunlanadi."""
    a_id, b_id = user.telegram_id, 888
    await auth.register(a_id, "alisher", "parol123")
    assert (await _resolve(storage, a_id)).is_signed_in

    await storage.get_or_create_user(telegram_id=b_id, first_name="Bobur")
    assert (await auth.login("alisher", "parol123", telegram_id=b_id)).ok

    kicked = await _resolve(storage, a_id)
    assert kicked.is_signed_in is False                 # A darvozaga tushadi
    assert (await _resolve(storage, b_id)).is_signed_in is True

    # A qayta kirsa — endi B ning sessiyasi yakunlanadi.
    assert (await auth.login("alisher", "parol123", telegram_id=a_id)).ok
    assert (await _resolve(storage, a_id)).is_signed_in is True
    b_view = await _resolve(storage, b_id)
    assert b_view.is_signed_in is False


async def test_register_second_profile_is_isolated_and_active(auth, storage, user) -> None:
    """Hisob bor bo'lsa /register yangi, alohida profil ochadi va unga o'tadi."""
    a_id = user.telegram_id
    await auth.register(a_id, "alisher", "parol123")
    await make_file(storage, a_id, name="asosiy.pdf")

    second = await auth.register(a_id, "ish_profil", "parol789")
    assert second.ok, second.message
    profile = second.user
    assert profile.telegram_id < 0                       # sintetik ID (Telegram ID emas)
    assert profile.profile_creator_id == a_id
    assert profile.login == "ish_profil"

    active = await _resolve(storage, a_id)
    assert active.telegram_id == profile.telegram_id and active.is_signed_in
    # Oldingi profil sessiyasi yakunlandi, fayllari joyida.
    assert (await storage.get_user_any(a_id)).is_authenticated is False
    assert await _file_count(storage, a_id) == 1
    assert await _file_count(storage, profile.telegram_id) == 0
    await make_file(storage, profile.telegram_id, name="ish.pdf")
    assert await _file_count(storage, a_id) == 1


async def test_switch_back_and_forth_between_profiles(auth, storage, user) -> None:
    a_id = user.telegram_id
    await auth.register(a_id, "alisher", "parol123")
    extra = (await auth.register(a_id, "ish_profil", "parol789")).user

    assert (await auth.login("alisher", "parol123", telegram_id=a_id)).ok
    active = await _resolve(storage, a_id)
    assert active.telegram_id == a_id and active.is_signed_in
    assert (await storage.get_user_any(extra.telegram_id)).is_authenticated is False

    assert (await auth.login("ish_profil", "parol789", telegram_id=a_id)).ok
    active = await _resolve(storage, a_id)
    assert active.telegram_id == extra.telegram_id and active.is_signed_in
    assert (await storage.get_user_any(a_id)).is_authenticated is False


async def test_wrong_password_does_not_switch_profile(auth, storage, user) -> None:
    a_id, b_id = user.telegram_id, 888
    await auth.register(a_id, "alisher", "parol123")
    await storage.get_or_create_user(telegram_id=b_id, first_name="Bobur")
    await auth.register(b_id, "bobur", "parol456")

    bad = await auth.login("alisher", "noto-gri", telegram_id=b_id)
    assert bad.outcome is AuthOutcome.WRONG_PASSWORD
    assert (await _resolve(storage, a_id)).is_signed_in is True     # A tegilmagan
    b_view = await _resolve(storage, b_id)
    assert b_view.telegram_id == b_id and b_view.is_signed_in       # B o'z profilida


async def test_blocked_telegram_cannot_switch_into_other_profile(auth, storage, user) -> None:
    a_id, b_id = user.telegram_id, 888
    await auth.register(a_id, "alisher", "parol123")
    await storage.get_or_create_user(telegram_id=b_id, first_name="Bobur")
    await storage.set_blocked(b_id, True, note="spam")
    result = await auth.login("alisher", "parol123", telegram_id=b_id)
    assert result.outcome is AuthOutcome.BLOCKED
    assert (await _resolve(storage, a_id)).is_signed_in is True


async def test_login_telegram_switches_profile(auth, storage, user) -> None:
    a_id = user.telegram_id
    await auth.register(a_id, "alisher", "parol123")
    await storage.get_or_create_user(telegram_id=555002, first_name="Yangi")
    result = await auth.login_telegram(555002, "alisher", "parol123")
    assert result.ok, result.message
    assert (await _resolve(storage, 555002)).telegram_id == a_id


async def test_list_profiles_shows_own_profiles(auth, storage, user) -> None:
    from services.profiles import ProfileService

    a_id = user.telegram_id
    await auth.register(a_id, "alisher", "parol123")
    await auth.register(a_id, "ish_profil", "parol789")
    logins = {p.login for p in await ProfileService(storage.session).list_profiles(a_id)}
    assert logins == {"alisher", "ish_profil"}


async def test_login_writes_audit_record(auth, storage, user) -> None:
    await auth.register(user.telegram_id, "alisher", "parol123")
    await storage.get_or_create_user(telegram_id=888, first_name="Bobur")
    assert (await auth.login("alisher", "parol123", telegram_id=888)).ok
    rows = await storage_rows(user)
    assert any(r.action == AuditAction.LOGIN_OK and r.actor_id == 888 for r in rows)


async def test_logout_and_change_password(auth, user) -> None:
    await auth.register(user.telegram_id, "alisher", "parol123")
    assert await auth.logout(user.telegram_id) is True
    assert user.is_authenticated is False

    wrong = await auth.change_password(user.telegram_id, "xato", "yangi123")
    assert wrong.outcome is AuthOutcome.WRONG_PASSWORD

    ok = await auth.change_password(user.telegram_id, "parol123", "yangi123")
    assert ok.ok
    assert user.check_password("yangi123") is True


def test_validate_login_rules() -> None:
    assert validate_login("alisher") is None
    assert validate_login("ab") is not None
    assert validate_login("a" * 33) is not None
    assert validate_login("bot") is not None


def test_button_matched_handlers_tolerate_missing_command_object() -> None:
    """``F.text.in_`` (reply tugma) bilan mos kelgan handlerda ``command`` yo'q.

    aiogram ``CommandObject`` ni faqat ``Command(...)`` filtri o'tganda beradi.
    Shuning uchun "🔑 Login"/"Ro'yxatdan o'tish" tugmalari bosilganda handler
    ``TypeError`` bermasligi uchun parametr **ixtiyoriy** bo'lishi shart.
    """
    import inspect

    from bot.handlers import auth

    for name in ("cmd_register", "cmd_login"):
        parameters = inspect.signature(getattr(auth, name)).parameters
        assert "command" in parameters, f"{name}: command parametri yo'q"
        assert parameters["command"].default is None, (
            f"{name}: command majburiy — reply tugma bosilganda TypeError bo'ladi"
        )
