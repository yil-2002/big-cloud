"""👑 Admin qatlami: /diyor statistikasi, foydalanuvchilar, broadcast, bayroqlar."""

from __future__ import annotations

from config import Settings, get_settings
from db.models import (
    FLAG_ALLOW_REGISTER,
    FLAG_AUTH_REQUIRED,
    AuditAction,
    get_flag,
    set_flag,
)
from services.auth import AuthService
from tests.conftest import make_file


# --------------------------------------------------------------------------- #
# Ruxsat tekshiruvi
# --------------------------------------------------------------------------- #
def test_admin_id_from_env(settings: Settings) -> None:
    """``ADMIN_ID`` .env'dan o'qiladi va ``is_admin`` uni taniydi."""
    assert settings.bot.is_admin(111) is True
    assert settings.bot.is_admin(222) is False
    assert 111 in settings.bot.all_admin_ids


def test_extra_admin_ids_merge(settings: Settings) -> None:
    """``ADMIN_ID`` va ``ADMIN_IDS`` bitta ro'yxatga birlashadi."""
    assert all(admin_id in settings.bot.all_admin_ids for admin_id in (111,))


# --------------------------------------------------------------------------- #
# 📊 Tizim statistikasi
# --------------------------------------------------------------------------- #
async def test_admin_stats_reports_users_files_and_size(storage, user) -> None:
    """📊 Foydalanuvchilar, fayllar va indekslangan hajm."""
    other, _ = await storage.get_or_create_user(telegram_id=888, first_name="Other")
    await make_file(storage, user.telegram_id, name="a.mp4", size=1500, category="video")
    await make_file(storage, other.telegram_id, name="b.mp3", size=500, category="audio")

    stats = await storage.admin_stats()
    assert stats["users"]["total"] == 2
    assert stats["files"]["total"] == 2
    assert stats["files"]["size"] == 2000
    assert stats["by_category"]["video"]["count"] == 1
    assert stats["by_category"]["audio"]["size"] == 500
    # Har bir statistika javobida vaqt belgisi bo'ladi (panel "yangilangan" ko'rsatadi).
    assert stats["generated_at"]


async def test_admin_stats_active_sessions_window(storage, user) -> None:
    """Faol sessiyalar = so'nggi oynada ko'ringan foydalanuvchilar."""
    import datetime as _dt

    user.last_seen_at = _dt.datetime.now(_dt.UTC)
    storage.session.add(user)
    await storage.session.commit()

    stale, _ = await storage.get_or_create_user(telegram_id=999, first_name="Eski")
    stale.last_seen_at = _dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=72)
    storage.session.add(stale)
    await storage.session.commit()

    stats = await storage.admin_stats(active_window_hours=24)
    assert stats["active_sessions"] == 1
    assert stats["active_window_hours"] == 24


async def test_admin_stats_archive_counters(storage, user, settings) -> None:
    """Arxivlangan va kutilayotgan fayllar soni (vault salomatligi uchun)."""
    first = await make_file(storage, user.telegram_id, name="arxivlangan.pdf")
    await make_file(storage, user.telegram_id, name="kutilmoqda.pdf")
    first.mark_archived(chat_id=settings.archive.channel_id, message_id=31)
    storage.session.add(first)
    await storage.session.commit()

    stats = await storage.admin_stats()
    assert stats["files"]["archived"] == 1
    assert stats["files"]["pending_archive"] == 1


# --------------------------------------------------------------------------- #
# 👥 Foydalanuvchilarni boshqarish
# --------------------------------------------------------------------------- #
async def test_list_users_pagination_and_search(storage) -> None:
    """Sahifalangan ro'yxat + qidiruv (login/username/telegram_id)."""
    for index in range(12):
        await storage.get_or_create_user(
            telegram_id=1000 + index, username=f"user{index}", first_name=f"User {index}"
        )

    first_page, total = await storage.list_users(page=1, page_size=5)
    second_page, _ = await storage.list_users(page=2, page_size=5)
    assert total == 12
    assert len(first_page) == 5 and len(second_page) == 5
    assert {row.telegram_id for row in first_page}.isdisjoint(
        {row.telegram_id for row in second_page}
    )

    found, found_total = await storage.list_users(search="user3")
    assert found_total == 1
    assert found[0].username == "user3"

    by_id, by_id_total = await storage.list_users(search="1005")
    assert by_id_total == 1
    assert by_id[0].telegram_id == 1005


async def test_list_users_filters(storage, user) -> None:
    """``blocked_only`` va ``authenticated_only`` filtrlari."""
    other, _ = await storage.get_or_create_user(telegram_id=888, first_name="Blocked")
    await storage.set_blocked(other.telegram_id, True, note="spam")

    blocked, blocked_total = await storage.list_users(blocked_only=True)
    assert blocked_total == 1
    assert blocked[0].telegram_id == 888

    await AuthService(storage.session, storage=storage, settings=get_settings()).register(
        user.telegram_id, "alisher", "parol123"
    )
    authed, authed_total = await storage.list_users(authenticated_only=True)
    assert authed_total == 1
    assert authed[0].login == "alisher"


async def test_block_and_unblock(storage, user) -> None:
    """Bloklash: kirish yo'q; blokdan chiqarish: qayta ishlaydi."""
    await storage.set_blocked(user.telegram_id, True, note="qoidabuzarlik")
    refreshed = await storage.get_user_any(user.telegram_id)
    assert refreshed is not None
    assert refreshed.is_blocked is True
    assert refreshed.admin_note == "qoidabuzarlik"

    await storage.set_blocked(user.telegram_id, False)
    assert (await storage.get_user_any(user.telegram_id)).is_blocked is False


async def test_blocked_user_cannot_login(storage, user) -> None:
    """Bloklangan hisob parolni to'g'ri bo'lsa ham kirmaydi."""
    auth = AuthService(storage.session, storage=storage, settings=get_settings())
    await auth.register(user.telegram_id, "alisher", "parol123")
    await storage.set_blocked(user.telegram_id, True)

    result = await auth.login("alisher", "parol123", telegram_id=user.telegram_id)
    assert result.ok is False


async def test_reset_password_generates_one_time_secret(storage, user) -> None:
    """🔑 Parolni tiklash: tasodifiy parol bir marta ko'rsatiladi."""
    auth = AuthService(storage.session, storage=storage, settings=get_settings())
    await auth.register(user.telegram_id, "alisher", "eski1234")

    _user, new_password = await storage.reset_password(user.telegram_id)
    assert len(new_password) >= 10
    assert new_password != "eski1234"
    assert user.check_password(new_password) is True
    # Eski parol endi ishlamaydi, yangi ishlaydi.
    assert (await auth.login("alisher", "eski1234", telegram_id=user.telegram_id)).ok is False
    assert (await auth.login("alisher", new_password, telegram_id=user.telegram_id)).ok is True


async def test_reset_password_unlocks_account(storage, user) -> None:
    """Qulflangan hisob parol tiklanganda ochiladi (failed_login_attempts=0)."""
    import datetime as _dt

    user.failed_login_attempts = 5
    user.locked_until = _dt.datetime.now(_dt.UTC) + _dt.timedelta(minutes=10)
    storage.session.add(user)
    await storage.session.commit()
    assert user.is_locked is True

    await storage.reset_password(user.telegram_id, "yangi1234")
    assert user.is_locked is False
    assert user.failed_login_attempts == 0


async def test_set_custom_password(storage, user) -> None:
    """Admin aniq parol ham berishi mumkin."""
    await storage.reset_password(user.telegram_id, "AniqParol1")
    assert user.check_password("AniqParol1") is True


async def test_per_user_quota_override(storage, user) -> None:
    """Foydalanuvchiga alohida kvota (0 = cheksiz)."""
    await storage.set_user_quota(user.telegram_id, 5 * 1024 ** 3)
    assert user.storage_quota == 5 * 1024 ** 3
    assert storage.quota_label(user) != "Cheksiz (Unlimited)"

    await storage.set_user_quota(user.telegram_id, 0)
    assert user.quota_is_unlimited(0) is True
    assert storage.quota_label(user) == "Cheksiz (Unlimited)"


async def test_user_files_count(storage, user) -> None:
    """Admin kartasi: bitta foydalanuvchining fayllari va hajmi."""
    other, _ = await storage.get_or_create_user(telegram_id=888, first_name="Other")
    await make_file(storage, user.telegram_id, name="a.bin", size=1000)
    await make_file(storage, user.telegram_id, name="b.bin", size=2000)
    await make_file(storage, other.telegram_id, name="c.bin", size=9999)

    count, size = await storage.user_files_count(user.telegram_id)
    assert count == 2 and size == 3000


# --------------------------------------------------------------------------- #
# 📢 E'lon yuborish
# --------------------------------------------------------------------------- #
async def test_broadcast_targets_exclude_blocked(storage, user) -> None:
    """Bloklanganlar broadcast ro'yxatiga kirmaydi."""
    other, _ = await storage.get_or_create_user(telegram_id=888, first_name="Blocked")
    await storage.set_blocked(other.telegram_id, True)

    targets = await storage.broadcast_targets()
    assert user.telegram_id in targets
    assert other.telegram_id not in targets

    including = await storage.broadcast_targets(include_blocked=True)
    assert other.telegram_id in including


async def test_broadcast_targets_authenticated_only(storage, user) -> None:
    auth = AuthService(storage.session, storage=storage, settings=get_settings())
    await auth.register(user.telegram_id, "alisher", "parol123")
    await storage.get_or_create_user(telegram_id=888, first_name="Hisobsiz")

    targets = await storage.broadcast_targets(only_authenticated=True)
    assert targets == [user.telegram_id]


# --------------------------------------------------------------------------- #
# ⚙️ Dinamik sozlamalar (system_flags)
# --------------------------------------------------------------------------- #
async def test_flag_roundtrip(session) -> None:
    """Bayroq yoziladi va o'qiladi (runtime toggle DB'da saqlanadi)."""
    await set_flag(session, FLAG_AUTH_REQUIRED, "false")
    assert await get_flag(session, FLAG_AUTH_REQUIRED) == "false"

    await set_flag(session, FLAG_ALLOW_REGISTER, True, updated_by=111)
    assert await get_flag(session, FLAG_ALLOW_REGISTER) == "1"   # bool -> "1"/"0"

    # Mavjud bo'lmagan bayroq -> default
    assert await get_flag(session, "yoq", default="standart") == "standart"


async def test_flags_do_not_touch_env_defaults(session, settings) -> None:
    """.env qiymati bayroq yozilmaguncha o'zgarmaydi."""
    assert settings.auth.required is True
    assert await get_flag(session, FLAG_AUTH_REQUIRED) is None


async def test_audit_trail_is_readable(storage, user) -> None:
    """Audit yozuvlari admin panelda o'qiladi (recent_audit)."""
    auth = AuthService(storage.session, storage=storage, settings=get_settings())
    await auth.register(user.telegram_id, "alisher", "parol123")
    await auth.login("alisher", "noto-gri", telegram_id=user.telegram_id)

    rows, total = await storage.recent_audit(limit=10)
    assert total >= 2
    actions = [row.action for row in rows]
    assert AuditAction.REGISTER in actions
    assert AuditAction.LOGIN_FAIL in actions
