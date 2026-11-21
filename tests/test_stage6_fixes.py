"""🔧 6-bosqich: kichik tuzatishlar to'plami.

To'rt mustaqil himoya, bitta bosqich:

1. **🗑 Savat (soft delete)** — DELETE endi qatorni o'chirmaydi:
   ``deleted_at`` to'ldiriladi, ro'yxatlar filtrlaydi, ``restore`` qaytaradi,
   ``purge`` butunlay o'chiradi. Kvota: savatga tushganda bo'shaydi,
   tiklanganda qayta band bo'ladi. Savatdagi dublikat qayta yuklansa —
   avtomatik tiklash (UniqueConstraint bilan to'qnashmaslik uchun).
2. **⏱ /stream throttle** — bandwidth-DoS himoyasi: alohida keng ``stream``
   scope (pleer seek'lari uchun 120/60s sukut), 429 + Retry-After.
3. **🎫 Ticket qat'iyligi** — ``purpose`` faqat ``stream``/``thumb``;
   ``owner_id`` fayl egasi (yoki admin) bo'lishi shart.
4. **🚪 API darvozalari** — bloklangan foydalanuvchi hamma joyda 403;
   tasdiqlanmagan hisob (AUTH_REQUIRED=true) faqat ``/auth/*`` ga kiradi.
"""

# ``stream_client`` fixture'i ``tests.test_stream`` dan import qilinadi; pytest
# uni parametr nomi orqali topadi — ruff buni "qayta ta'rif" deb o'ylaydi.
# ruff: noqa: F811

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from db.models import File
from services.share import ShareService
from tests.conftest import make_file
from tests.test_stream import stream_client  # noqa: F401 - fixture importi


@pytest.fixture()
def share(storage, settings) -> ShareService:
    return ShareService(storage.session, settings=settings)


@pytest.fixture()
def auth_client(stream_client, user, init_data):
    """JWT bilan tayyor TestClient (UI bilan bir xil yo'l)."""
    client = stream_client()
    response = client.post(
        "/api/v1/auth/telegram", json={"init_data": init_data(user.telegram_id)}
    )
    assert response.status_code == 200, response.text
    client.headers.update(
        {"Authorization": f"Bearer {response.json()['access_token']}"}
    )
    return client


# --------------------------------------------------------------------------- #
# 1. 🗑 Savat (soft delete + restore + purge)
# --------------------------------------------------------------------------- #
async def test_delete_moves_to_trash_not_gone(auth_client, storage, user) -> None:
    """DELETE → qator qoladi (deleted_at), ro'yxatda ko'rinmaydi."""
    record = await make_file(storage, user.telegram_id, name="hujjat.pdf", size=2048)
    record_id = str(record.id)

    deleted = auth_client.delete(f"/api/v1/files/{record_id}")
    assert deleted.status_code == 200, deleted.text

    listing = auth_client.get("/api/v1/files").json()
    assert all(item["id"] != record_id for item in listing["items"])

    # Qator hali bazada (soft): trash ro'yxatida ko'rinadi.
    trash = auth_client.get("/api/v1/files/trash").json()
    assert any(item["id"] == record_id for item in trash["items"])
    assert trash["items"][0]["deleted_at"], "savat yozuvida deleted_at bo'lishi shart"


async def test_restore_brings_file_back(auth_client, storage, user) -> None:
    """POST /restore → fayl ro'yxatga qaytadi, savatdan chiqadi."""
    record = await make_file(storage, user.telegram_id, name="qaytadi.mp3", size=1024)
    record_id = str(record.id)
    auth_client.delete(f"/api/v1/files/{record_id}")

    restored = auth_client.post(f"/api/v1/files/{record_id}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["deleted_at"] is None

    listing = auth_client.get("/api/v1/files").json()
    assert any(item["id"] == record_id for item in listing["items"])
    trash = auth_client.get("/api/v1/files/trash").json()
    assert all(item["id"] != record_id for item in trash["items"])


async def test_purge_removes_row_forever(auth_client, storage, user, session) -> None:
    """DELETE /purge → qator butunlay yo'qoladi."""
    record = await make_file(storage, user.telegram_id, name="ketadi.zip", size=512)
    record_id = record.id
    auth_client.delete(f"/api/v1/files/{record_id}")

    purged = auth_client.delete(f"/api/v1/files/{record_id}/purge")
    assert purged.status_code == 200, purged.text

    session.expire_all()
    row = (
        await session.execute(select(File.id).where(File.id == record_id))
    ).scalar_one_or_none()
    assert row is None, "purge'dan keyin qator qolmasligi kerak"


async def test_trash_frees_and_restore_recharges_quota(auth_client, storage, user) -> None:
    """Kvota: savat = bo'sh, tiklash = qayta band."""
    record = await make_file(storage, user.telegram_id, name="katta.bin", size=9000)
    record_id = str(record.id)
    await storage.recalculate_storage(user.telegram_id)

    me = auth_client.get("/api/v1/auth/me").json()
    used_before = me["storage_used"]
    assert used_before >= 9000

    auth_client.delete(f"/api/v1/files/{record_id}")
    used_trashed = auth_client.get("/api/v1/auth/me").json()["storage_used"]
    assert used_trashed == used_before - 9000

    auth_client.post(f"/api/v1/files/{record_id}/restore")
    used_restored = auth_client.get("/api/v1/auth/me").json()["storage_used"]
    assert used_restored == used_before


async def test_trashed_file_stream_is_404(stream_client, share, storage, user) -> None:
    """Savatdagi faylning eski ticket'i ishlamaydi (404)."""
    record = await make_file(storage, user.telegram_id, name="yashirin.mp4")
    ticket = share.issue_stream_ticket(record.id, user.telegram_id)
    await storage.delete_file(record.id, user.telegram_id)

    client = stream_client()
    response = client.get(f"/api/v1/stream/{ticket.token}")
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "file_not_found"


async def test_reupload_of_trashed_duplicate_restores(storage, user) -> None:
    """Savatdagi fayl qayta yuklansa — tiklash (UniqueConstraint buzilmaydi)."""
    record = await make_file(storage, user.telegram_id, name="dup.pdf", unique_id="U-dup")
    await storage.delete_file(record.id, user.telegram_id)

    stored = await storage.add_file(
        telegram_id=user.telegram_id,
        file_id="AgAD-yangi",
        file_unique_id="U-dup",
        file_name="dup.pdf",
        file_size=1024,
    )
    assert stored.is_new is False and stored.deduplicated is True
    assert stored.record.deleted_at is None, "qayta yuklash tiklashga aylanishi kerak"
    assert stored.record.file_id == "AgAD-yangi"


async def test_bulk_delete_is_soft(auth_client, storage, user, session) -> None:
    """bulk-delete ham savatga tashlaydi (qatorlar qoladi)."""
    a = await make_file(storage, user.telegram_id, name="a.txt")
    b = await make_file(storage, user.telegram_id, name="b.txt")
    ids = [str(a.id), str(b.id)]

    response = auth_client.post("/api/v1/files/bulk-delete", json={"ids": ids})
    assert response.status_code == 200, response.text

    session.expire_all()
    rows = (
        await session.execute(
            select(File.deleted_at).where(File.id.in_([uuid.UUID(i) for i in ids]))
        )
    ).scalars().all()
    assert len(rows) == 2 and all(value is not None for value in rows)


# --------------------------------------------------------------------------- #
# 2. ⏱ /stream throttle
# --------------------------------------------------------------------------- #
async def test_stream_scope_has_own_limits(settings) -> None:
    """`stream` scope o'z sozlamalariga ega (api'nikidan mustaqil)."""
    from api.dependencies import _limits_for

    limit, window = _limits_for("stream", settings)
    assert (limit, window) == (
        settings.security.stream_rate_limit,
        settings.security.stream_rate_window,
    )
    assert limit >= 60, "pleer seek'lari uchun chegara keng bo'lishi kerak"


async def test_stream_returns_429_past_the_limit(
    stream_client, share, storage, user, settings
) -> None:
    """Chegara oshsa — 429 + Retry-After (bandwidth-DoS to'xtaydi)."""
    record = await make_file(storage, user.telegram_id, name="kino.mp4", category="video")
    ticket = share.issue_stream_ticket(record.id, user.telegram_id)

    original = (
        settings.security.rate_limit_enabled,
        settings.security.stream_rate_limit,
        settings.security.stream_rate_window,
    )
    # conftest global RATE_LIMIT_ENABLED=false qo'yadi (boshqa testlar 429
    # ko'rmasligi uchun) — bu testda ataylab yoqamiz.
    settings.security.rate_limit_enabled = True
    settings.security.stream_rate_limit = 3
    settings.security.stream_rate_window = 60
    try:
        client = stream_client()
        codes = [
            client.get(f"/api/v1/stream/{ticket.token}").status_code for _ in range(5)
        ]
        assert codes.count(200) <= 3, f"chegara ishlamadi: {codes}"
        assert 429 in codes, f"429 kutildi: {codes}"
    finally:
        (
            settings.security.rate_limit_enabled,
            settings.security.stream_rate_limit,
            settings.security.stream_rate_window,
        ) = original


# --------------------------------------------------------------------------- #
# 3. 🎫 Ticket owner/purpose qat'iyligi
# --------------------------------------------------------------------------- #
async def test_foreign_owner_ticket_is_403(
    stream_client, share, storage, user
) -> None:
    """Boshqa foydalanuvchi nomidan imzolangan ticket rad etiladi."""
    record = await make_file(storage, user.telegram_id, name="meniki.mp4")
    foreign = share.issue_stream_ticket(record.id, 999_999)  # egasi emas, admin emas

    client = stream_client()
    response = client.get(f"/api/v1/stream/{foreign.token}")
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "invalid_ticket"


async def test_unknown_purpose_ticket_is_403(
    stream_client, share, storage, user
) -> None:
    """`purpose` ro'yxatdan tashqarida bo'lsa — 403 (masalan, "upload")."""
    record = await make_file(storage, user.telegram_id, name="fayl.mp4")
    weird = share.issue_stream_ticket(record.id, user.telegram_id, purpose="upload")

    client = stream_client()
    response = client.get(f"/api/v1/stream/{weird.token}")
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "invalid_ticket"


async def test_admin_ticket_for_foreign_file_is_allowed(
    stream_client, share, storage, user, settings
) -> None:
    """Admin chiptasi begona faylga ham o'tadi (moderatsiya oqimi)."""
    record = await make_file(storage, user.telegram_id, name="tekshir.mp4")
    admin_id = next(iter(settings.bot.admin_ids), None)
    if admin_id is None:
        pytest.skip("ADMIN_ID sozlanmagan")
    ticket = share.issue_stream_ticket(record.id, admin_id)

    client = stream_client()
    assert client.get(f"/api/v1/stream/{ticket.token}").status_code == 200


# --------------------------------------------------------------------------- #
# 4. 🚪 API darvozalari (is_blocked / is_authenticated)
# --------------------------------------------------------------------------- #
async def test_blocked_user_gets_403_everywhere(stream_client, storage, user, init_data) -> None:
    """Bloklangan hisob: hatto /auth/me ham 403 `blocked`."""
    user.is_blocked = True
    storage.session.add(user)
    await storage.session.commit()

    client = stream_client()
    headers = {"X-Telegram-Init-Data": init_data(user.telegram_id)}
    for path in ("/api/v1/auth/me", "/api/v1/files", "/api/v1/folders"):
        response = client.get(path, headers=headers)
        assert response.status_code == 403, f"{path}: {response.status_code}"
        assert response.json()["detail"]["code"] == "blocked"


async def test_unauthenticated_user_blocked_from_files_but_not_auth(
    stream_client, storage, user, init_data
) -> None:
    """Tasdiqlanmagan hisob: /files → 403, /auth/me → 200 (login oqimi uchun)."""
    user.is_authenticated = False  # fixture'ni "yangi hisob" holatiga qaytaramiz
    storage.session.add(user)
    await storage.session.commit()

    client = stream_client()
    headers = {"X-Telegram-Init-Data": init_data(user.telegram_id)}

    me = client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, "login oqimi /auth/me siz ishlamaydi"
    assert me.json()["is_authenticated"] is False

    for path in ("/api/v1/files", "/api/v1/folders", "/api/v1/shares"):
        response = client.get(path, headers=headers)
        assert response.status_code == 403, f"{path}: {response.status_code}"
        assert response.json()["detail"]["code"] == "not_authenticated"


async def test_authenticated_user_passes_the_gate(auth_client, storage, user) -> None:
    """Login qilingan hisob to'siqsiz ishlaydi (regressiya himoyasi)."""
    assert user.is_authenticated is True  # fixture default

    assert auth_client.get("/api/v1/files").status_code == 200
    assert auth_client.get("/api/v1/folders").status_code == 200


# --------------------------------------------------------------------------- #
# 5. Frontend shartnomasi
# --------------------------------------------------------------------------- #
def test_frontend_has_trash_ui() -> None:
    """Savat sheet'i mavjud va to'g'ri endpointlarga ulangan."""
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "async function openTrashSheet" in html
    assert '"/files/trash?page=' in html
    assert '/restore"' in html and '/purge"' in html
    assert "not_authenticated:" in html, "yangi xato kodi lug'atda bo'lishi kerak"
    assert "savatga tushadi" in html, "o'chirish dialogi savat haqida ogohlantirsin"
