"""🔐 Autentifikatsiya oqimi: /register, /login, qulflash, bloklash."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from db.models import AuditAction, AuditLog, File, Folder
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


async def test_login_from_other_telegram_id_transfers_account(auth, storage, user) -> None:
    """Boshqa Telegram ID dan to'g'ri parol — hisob joriy akkauntga o'tadi."""
    old_id = user.telegram_id
    await auth.register(old_id, "alisher", "parol123")
    result = await auth.login("alisher", "parol123", telegram_id=999999)
    assert result.ok, result.message
    assert result.user is not None
    assert result.user.telegram_id == 999999
    assert result.user.login == "alisher"
    assert result.user.check_password("parol123") is True
    # Yangi qator bor, eski qator o'chirilgan.
    assert await storage.get_user_any(999999) is not None
    assert await storage.get_user_any(old_id) is None


async def test_transfer_moves_files_folders_and_storage_used(auth, storage, user) -> None:
    """Fayllar, papkalar va ishlatilgan hajm yangi akkauntga o'tadi."""
    old_id = user.telegram_id
    new_id = 555001
    await auth.register(old_id, "alisher", "parol123")
    folder = await storage.create_folder("Hujjatlar", old_id)
    record = await make_file(storage, old_id, name="hisobot.pdf", size=2048)
    await storage.move_file(record.id, old_id, folder.id)

    result = await auth.login("alisher", "parol123", telegram_id=new_id)
    assert result.ok, result.message

    moved_file = await storage.session.scalar(select(File).where(File.id == record.id))
    assert moved_file is not None
    assert moved_file.user_id == new_id
    assert moved_file.folder_id == folder.id

    moved_folder = await storage.session.get(Folder, folder.id)
    assert moved_folder is not None
    assert moved_folder.user_id == new_id

    new_user = await storage.get_user_any(new_id)
    assert new_user is not None
    assert new_user.storage_used == 2048
    assert new_user.login == "alisher"


async def test_transfer_removes_old_row_and_can_transfer_back(auth, storage, user) -> None:
    """Eski qator yo'q; eski ID dan kirish hisobni qaytarib o'tkazadi."""
    old_id = user.telegram_id
    await auth.register(old_id, "alisher", "parol123")

    first = await auth.login("alisher", "parol123", telegram_id=888001)
    assert first.ok and first.user is not None
    assert first.user.telegram_id == 888001
    assert await storage.get_user_any(old_id) is None

    back = await auth.login("alisher", "parol123", telegram_id=old_id)
    assert back.ok and back.user is not None
    assert back.user.telegram_id == old_id
    assert await storage.get_user_any(888001) is None
    assert back.user.check_password("parol123") is True


async def test_transfer_preserves_duplicate_file_rows(auth, storage, user) -> None:
    """Bir xil ``file_unique_id`` bo'lsa ham ikkala fayl qatori saqlanib qoladi."""
    old_id = user.telegram_id
    target_id = 888002
    await auth.register(old_id, "alisher", "parol123")
    target, _ = await storage.get_or_create_user(telegram_id=target_id, first_name="New")
    source_file = await make_file(storage, old_id, name="bir.txt", size=100, unique_id="UNIQ-SAME")
    target_file = await make_file(storage, target.telegram_id, name="bir.txt", size=100, unique_id="UNIQ-SAME")

    result = await auth.login("alisher", "parol123", telegram_id=target_id)
    assert result.ok, result.message

    rows = list(await storage.session.scalars(select(File).where(File.user_id == target_id)))
    assert len(rows) == 2, "ikkala fayl qatori ham saqlanishi shart"
    by_id = {row.id: row for row in rows}
    # Faylning o'zi (file_id, hajm) o'zgarmagan.
    assert by_id[source_file.id].file_id == source_file.file_id
    assert by_id[source_file.id].file_size == 100
    assert by_id[target_file.id].file_id == target_file.file_id
    # Unique constraint: ikkala fingerprint endi har xil.
    fingerprints = {row.file_unique_id for row in rows}
    assert len(fingerprints) == 2
    assert "UNIQ-SAME" in fingerprints
    new_user = await storage.get_user_any(target_id)
    assert new_user.storage_used == 200


async def test_transfer_preserves_duplicate_folder_rows(auth, storage, user) -> None:
    """Bir xil nomli papka bo'lsa — qator o'chmaydi, nom noyob qilinadi."""
    old_id = user.telegram_id
    target_id = 888003
    await auth.register(old_id, "alisher", "parol123")
    await storage.get_or_create_user(telegram_id=target_id)
    old_folder = await storage.create_folder("Rasmlar", old_id)
    target_folder = await storage.create_folder("Rasmlar", target_id)
    record = await make_file(storage, old_id, name="rasm.jpg", size=300)
    await storage.move_file(record.id, old_id, old_folder.id)

    result = await auth.login("alisher", "parol123", telegram_id=target_id)
    assert result.ok, result.message

    folders = list(await storage.session.scalars(select(Folder).where(Folder.user_id == target_id)))
    assert len(folders) == 2, "papka qatorlari soni o'zgarmasligi kerak"
    by_id = {folder.id: folder for folder in folders}
    assert by_id[target_folder.id].name == "Rasmlar"
    assert by_id[old_folder.id].name != "Rasmlar"
    assert by_id[old_folder.id].name.startswith("Rasmlar")
    moved = await storage.session.scalar(select(File).where(File.id == record.id))
    assert moved is not None
    assert moved.folder_id == old_folder.id  # fayl o'z papkasida qoldi
    assert moved.user_id == target_id


async def test_transfer_preserves_folder_tree(auth, storage, user) -> None:
    """Papka daraxti (parent_id zanjiri) ko'chishdan keyin buzilmaydi."""
    old_id = user.telegram_id
    target_id = 888004
    await auth.register(old_id, "alisher", "parol123")
    await storage.get_or_create_user(telegram_id=target_id)
    root = await storage.create_folder("Loyiha", old_id)
    child = await storage.create_folder("2026", old_id, root.id)
    grand = await storage.create_folder("Yanvar", old_id, child.id)

    result = await auth.login("alisher", "parol123", telegram_id=target_id)
    assert result.ok, result.message

    moved_root = await storage.session.get(Folder, root.id)
    moved_child = await storage.session.get(Folder, child.id)
    moved_grand = await storage.session.get(Folder, grand.id)
    assert moved_root.user_id == target_id
    assert moved_child.parent_id == root.id
    assert moved_grand.parent_id == child.id
    assert moved_child.user_id == target_id


async def test_transfer_keeps_tree_for_duplicate_folder_name(auth, storage, user) -> None:
    """Dublikat papka nomi o'zgarsa ham daraxt (parent_id) saqlanadi."""
    old_id = user.telegram_id
    target_id = 888005
    await auth.register(old_id, "alisher", "parol123")
    await storage.get_or_create_user(telegram_id=target_id)
    target_root = await storage.create_folder("Loyiha", target_id)
    old_root = await storage.create_folder("Loyiha", old_id)
    old_child = await storage.create_folder("2026", old_id, old_root.id)

    result = await auth.login("alisher", "parol123", telegram_id=target_id)
    assert result.ok, result.message

    moved_root = await storage.session.get(Folder, old_root.id)
    moved_child = await storage.session.get(Folder, old_child.id)
    assert moved_root is not None and moved_child is not None
    assert moved_root.name == "Loyiha (2)"
    assert moved_child.parent_id == old_root.id  # daraxt buzilmagan
    assert moved_child.user_id == target_id
    folders = list(await storage.session.scalars(select(Folder).where(Folder.user_id == target_id)))
    assert {folder.id for folder in folders} == {target_root.id, old_root.id, old_child.id}


async def test_transfer_wrong_password_moves_nothing(auth, storage, user) -> None:
    """Noto'g'ri parol — rad etiladi va hech narsa ko'chmaydi."""
    old_id = user.telegram_id
    target_id = 888006
    await auth.register(old_id, "alisher", "parol123")
    record = await make_file(storage, old_id, name="muhim.bin", size=512)
    folder = await storage.create_folder("Saqlangan", old_id)

    result = await auth.login("alisher", "notogri", telegram_id=target_id)
    assert result.outcome is AuthOutcome.WRONG_PASSWORD

    still = await storage.get_user_any(old_id)
    assert still is not None and still.login == "alisher"
    assert await storage.get_user_any(target_id) is None
    kept_file = await storage.session.get(File, record.id)
    kept_folder = await storage.session.get(Folder, folder.id)
    assert kept_file.user_id == old_id
    assert kept_folder.user_id == old_id


async def test_transfer_writes_audit_record(auth, storage, user) -> None:
    """O'tkazish audit jurnaliga yozuv qoldiradi."""
    old_id = user.telegram_id
    new_id = 888007
    await auth.register(old_id, "alisher", "parol123")
    await auth.login("alisher", "parol123", telegram_id=new_id, source="api", ip_address="10.0.0.9")

    rows = list(
        await storage.session.scalars(select(AuditLog).where(AuditLog.target_id == old_id))
    )
    transferred = [row for row in rows if row.detail and "account transferred" in row.detail]
    assert transferred, "o'tkazish audit yozuvi topilmadi"
    entry = transferred[-1]
    assert entry.actor_id == new_id
    assert entry.success is True
    assert entry.source == "api"
    assert f"from {old_id} to {new_id}" in (entry.detail or "")


async def test_transfer_preserves_trashed_duplicate_too(auth, storage, user) -> None:
    """Savatdagi dublikat ham saqlanadi; faol nusxa yo'qolmaydi."""
    old_id = user.telegram_id
    target_id = 888009
    await auth.register(old_id, "alisher", "parol123")
    await storage.get_or_create_user(telegram_id=target_id)
    source_file = await make_file(storage, old_id, name="fayl.bin", size=700, unique_id="UNIQ-TRASH")
    trashed = await make_file(storage, target_id, name="fayl.bin", size=700, unique_id="UNIQ-TRASH")
    await storage.delete_file(trashed.id, target_id)

    result = await auth.login("alisher", "parol123", telegram_id=target_id)
    assert result.ok, result.message

    rows = list(await storage.session.scalars(select(File).where(File.user_id == target_id)))
    assert len(rows) == 2, "ikkala qator ham saqlanishi shart"
    by_id = {row.id: row for row in rows}
    assert by_id[trashed.id].deleted_at is not None, "savatdagi holat saqlanadi"
    assert by_id[source_file.id].deleted_at is None, "faol nusxa yo'qolmaydi"
    new_user = await storage.get_user_any(target_id)
    assert new_user.storage_used == 700


async def test_transfer_preserves_row_counts_and_file_fields(auth, storage, user) -> None:
    """O'tkazishdan oldin/keyin ``files`` va ``folders`` qatorlari soni bir xil."""
    old_id = user.telegram_id
    target_id = 888010
    await auth.register(old_id, "alisher", "parol123")
    await storage.get_or_create_user(telegram_id=target_id)

    await storage.create_folder("Papka", old_id)
    await storage.create_folder("Papka", target_id)
    source_file = await make_file(storage, old_id, name="a.bin", size=111)
    await make_file(storage, target_id, name="b.bin", size=222)
    await make_file(storage, old_id, name="c.bin", size=333, unique_id="UNIQ-COUNT")
    await make_file(storage, target_id, name="d.bin", size=444, unique_id="UNIQ-COUNT")

    files_before = int(await storage.session.scalar(select(func.count()).select_from(File)))
    folders_before = int(await storage.session.scalar(select(func.count()).select_from(Folder)))

    result = await auth.login("alisher", "parol123", telegram_id=target_id)
    assert result.ok, result.message

    files_after = int(await storage.session.scalar(select(func.count()).select_from(File)))
    folders_after = int(await storage.session.scalar(select(func.count()).select_from(Folder)))
    assert files_after == files_before, "hech bir fayl qatori yo'qolmasligi shart"
    assert folders_after == folders_before, "hech bir papka qatori yo'qolmasligi shart"

    kept = await storage.session.get(File, source_file.id)
    assert kept is not None and kept.user_id == target_id
    assert kept.file_id == source_file.file_id
    assert kept.file_size == 111
    assert kept.file_name == "a.bin"


async def test_login_telegram_supports_transfer(auth, storage, user) -> None:
    """``login_telegram`` ham hisobni o'tkazishni qo'llab-quvvatlaydi."""
    old_id = user.telegram_id
    new_id = 888008
    await auth.register(old_id, "alisher", "parol123")

    result = await auth.login_telegram(new_id, "alisher", "parol123")
    assert result.ok, result.message
    assert result.user is not None and result.user.telegram_id == new_id
    assert await storage.get_user_any(new_id) is not None
    assert await storage.get_user_any(old_id) is None


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
