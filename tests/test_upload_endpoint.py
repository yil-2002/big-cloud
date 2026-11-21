"""⬆️ Mini App HTTP upload (multipart → Telegram relay) testlari.

Mini App faylni brauzerdagidek ``multipart/form-data`` bilan yuboradi; server
baytlarni qabul qilib, Telegram'ga uzatadi va ``file_id`` ni bazaga yozadi.
Bu modul shu zanjirning shartnomasini qo'riqlaydi: 503 (bot yo'q), 413 (hajm),
201 (muvaffaqiyat + temp fayl tozalanishi) va noto'g'ri papka (422).
"""

from __future__ import annotations

import os

from db.models import FileCategory
from services.telegram import TelegramFileInfo


class _FakeBot:
    """``send_document`` chaqiruvini yozib boruvchi soxta bot."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_document(self, **kwargs):
        self.sent.append(kwargs)
        return object()


def _token(client, init_data) -> str:
    """``/auth/telegram`` orqali JWT oladi (telegram_id=777)."""
    response = client.post("/api/v1/auth/telegram", json={"init_data": init_data(777)})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_upload_unavailable_without_bot(session, client, init_data, user) -> None:
    """Bot ishlamayotgan bo'lsa — 503 va aniq kod."""
    client.app.state.bot = None
    token = _token(client, init_data)

    response = client.post(
        "/api/v1/files/upload",
        files={"file": ("a.txt", b"hello", "text/plain")},
        headers=_headers(token),
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "upload_unavailable"


def test_upload_rejects_oversized_file(session, client, init_data, user, monkeypatch) -> None:
    """50 MB (bu yerda kichraytirilgan) chegarasidan katta fayl — 413."""
    import api.routes.files as files_module

    monkeypatch.setattr(files_module, "MINIAPP_UPLOAD_LIMIT", 4)
    client.app.state.bot = _FakeBot()
    token = _token(client, init_data)

    response = client.post(
        "/api/v1/files/upload",
        files={"file": ("katta.txt", b"hello world", "text/plain")},
        headers=_headers(token),
    )

    assert response.status_code == 413
    detail = response.json()["detail"]
    assert detail["code"] == "file_too_large_for_upload"
    assert detail["limit"] == 4


def test_upload_rejects_bad_folder_id(session, client, init_data, user) -> None:
    """Noto'g'ri ``folder_id`` — 422 (server 500 bermasin)."""
    client.app.state.bot = _FakeBot()
    token = _token(client, init_data)

    response = client.post(
        "/api/v1/files/upload",
        files={"file": ("a.txt", b"hello", "text/plain")},
        data={"folder_id": "uuid-emas"},
        headers=_headers(token),
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "validation_error"


def test_upload_success_stores_file_and_cleans_temp(
    session, client, init_data, user, monkeypatch
) -> None:
    """Muvaffaqiyatli yuklash: 201, Telegram'ga uzatilgan, DB'da bor, temp o'chgan."""
    import api.routes.files as files_module

    fake = _FakeBot()
    client.app.state.bot = fake

    info = TelegramFileInfo(
        file_id="FAKE-FILE-ID",
        file_unique_id="FAKE-UNIQUE-ID",
        file_name="salom.txt",
        file_size=5,
        mime_type="text/plain",
        category=FileCategory.DOCUMENT,
        caption=None,
    )
    monkeypatch.setattr(files_module, "extract_file_info", lambda message: info)

    made: list[str] = []
    real_named_temp = files_module.tempfile.NamedTemporaryFile

    def spy(*args, **kwargs):
        handle = real_named_temp(*args, **kwargs)
        made.append(handle.name)
        return handle

    monkeypatch.setattr(files_module.tempfile, "NamedTemporaryFile", spy)

    token = _token(client, init_data)
    response = client.post(
        "/api/v1/files/upload",
        files={"file": ("salom.txt", b"hello", "text/plain")},
        headers=_headers(token),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "salom.txt"
    assert body["category"] == "document"
    assert body["size"] == 5

    # Telegram relay chaqirilgan va to'g'ri chatga
    assert fake.sent, "send_document chaqirilmadi"
    assert fake.sent[0]["chat_id"] == 777

    # Vaqtinchalik fayl o'chirilgan
    assert made, "temp fayl yaratilmadi"
    assert all(not os.path.exists(path) for path in made)

    # Bazada ko'rinadi
    listing = client.get("/api/v1/files?page_size=50", headers=_headers(token))
    assert listing.status_code == 200
    names = [item["name"] for item in listing.json()["items"]]
    assert "salom.txt" in names


def test_upload_256kb_integrity_and_headers(
    session, client, init_data, user, monkeypatch
) -> None:
    """256 KB tasodifiy bayt yuklash, X-File-SHA256 va temp fayl butunligini tekshirish."""
    import hashlib
    import os

    import api.routes.files as files_module

    fake = _FakeBot()
    client.app.state.bot = fake

    info = TelegramFileInfo(
        file_id="FAKE-FILE-ID",
        file_unique_id="FAKE-UNIQUE-ID",
        file_name="rand.bin",
        file_size=256 * 1024,
        mime_type="application/octet-stream",
        category=FileCategory.DOCUMENT,
        caption=None,
    )
    monkeypatch.setattr(files_module, "extract_file_info", lambda message: info)

    saved_tmp_content = None

    async def fake_send_document(chat_id, document):
        nonlocal saved_tmp_content
        with open(document.path, "rb") as f:  # noqa: ASYNC230 - test dubuli, kichik fayl
            saved_tmp_content = f.read()
        return object()

    fake.send_document = fake_send_document

    token = _token(client, init_data)
    data = os.urandom(256 * 1024)
    expected_sha256 = hashlib.sha256(data).hexdigest()

    response = client.post(
        "/api/v1/files/upload",
        files={"file": ("rand.bin", data, "application/octet-stream")},
        headers=_headers(token),
    )

    assert response.status_code == 201, response.text
    assert response.headers.get("X-File-SHA256") == expected_sha256

    body = response.json()
    assert body["size"] == len(data)
    assert saved_tmp_content == data


def test_upload_size_mismatch_rejected(session, client, init_data, user, monkeypatch) -> None:
    """E'lon qilingan hajm va qabul qilingan hajm mos kelmasa 400."""
    import api.routes.files as files_module
    from db.models import FileCategory
    from services.telegram import TelegramFileInfo

    client.app.state.bot = _FakeBot()
    token = _token(client, init_data)

    info = TelegramFileInfo(
        file_id="FAKE-FILE-ID",
        file_unique_id="FAKE-UNIQUE-ID",
        file_name="a.txt",
        file_size=5,
        mime_type="text/plain",
        category=FileCategory.DOCUMENT,
        caption=None,
    )
    monkeypatch.setattr(files_module, "extract_file_info", lambda message: info)

    # MUHIM: FastAPI so'rovda **starlette** ``UploadFile`` nusxasini yaratadi,
    # shuning uchun patch aynan shu klassga qo'llanadi (``fastapi.UploadFile``
    # faqat annotatsiya uchun — unga patch qilish ta'sir qilmaydi).
    from starlette.datastructures import UploadFile as StarletteUploadFile

    async def mock_read(self, size=-1):
        # Mijoz "12345" jo'natdi, lekin o'qishda hech narsa qaytmaydi (uzilgan yuklash).
        return b""

    monkeypatch.setattr(StarletteUploadFile, "read", mock_read)
    # E'lon qilingan hajm 5 bo'lib qoladi — server buni qabul qilingan 0 bilan
    # solishtirib, 400 qaytarishi kerak.
    monkeypatch.setattr(StarletteUploadFile, "size", 5, raising=False)

    response = client.post(
        "/api/v1/files/upload",
        files={"file": ("a.txt", b"12345", "text/plain")},
        headers=_headers(token),
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "upload_size_mismatch"


class _FakeMessage:
    def __init__(self, message_id=1):
        self.message_id = message_id

class _FakeSentDocument(_FakeMessage):
    def __init__(self, message_id=1):
        super().__init__(message_id)

def test_upload_success_archives_and_cleans_message(
    session, client, init_data, user, monkeypatch
) -> None:
    """Vault yoqilgan va ok=True bo'lsa xabar o'chiriladi."""
    import api.routes.files as files_module
    from services.vault import ArchiveResult
    fake_bot = _FakeBot()
    
    deleted_messages = []
    async def mock_delete(chat_id, message_id):
        deleted_messages.append((chat_id, message_id))
    fake_bot.delete_message = mock_delete
    
    client.app.state.bot = fake_bot

    info = TelegramFileInfo(
        file_id="FAKE-FILE-ID",
        file_unique_id="FAKE-UNIQUE-ID",
        file_name="salom.txt",
        file_size=5,
        mime_type="text/plain",
        category=FileCategory.DOCUMENT,
        caption=None,
    )
    monkeypatch.setattr(files_module, "extract_file_info", lambda message: info)
    
    async def mock_send_doc(**kwargs):
        return _FakeSentDocument(message_id=999)
    fake_bot.send_document = mock_send_doc

    class FakeVault:
        enabled = True
        async def archive_message(self, *args, **kwargs):
            return ArchiveResult(ok=True, message_id=111)
            
    monkeypatch.setattr(files_module, "get_vault", lambda settings: FakeVault())

    token = _token(client, init_data)
    response = client.post(
        "/api/v1/files/upload",
        files={"file": ("salom.txt", b"hello", "text/plain")},
        headers=_headers(token),
    )

    assert response.status_code == 201
    assert deleted_messages == [(777, 999)]

def test_upload_vault_disabled_no_delete(
    session, client, init_data, user, monkeypatch
) -> None:
    """Vault o'chiq bo'lsa xabar o'chirilmaydi."""
    import api.routes.files as files_module
    fake_bot = _FakeBot()
    deleted_messages = []
    async def mock_delete(chat_id, message_id):
        deleted_messages.append((chat_id, message_id))
    fake_bot.delete_message = mock_delete
    client.app.state.bot = fake_bot

    info = TelegramFileInfo(
        file_id="FAKE-FILE-ID",
        file_unique_id="FAKE-UNIQUE-ID",
        file_name="salom.txt",
        file_size=5,
        mime_type="text/plain",
        category=FileCategory.DOCUMENT,
        caption=None,
    )
    monkeypatch.setattr(files_module, "extract_file_info", lambda message: info)
    
    async def mock_send_doc(**kwargs):
        return _FakeSentDocument(message_id=999)
    fake_bot.send_document = mock_send_doc

    class FakeVault:
        enabled = False
        async def archive_message(self, *args, **kwargs):
            return None
            
    monkeypatch.setattr(files_module, "get_vault", lambda settings: FakeVault())

    token = _token(client, init_data)
    client.post(
        "/api/v1/files/upload",
        files={"file": ("salom.txt", b"hello", "text/plain")},
        headers=_headers(token),
    )
    assert not deleted_messages

def test_upload_vault_fails_no_delete(
    session, client, init_data, user, monkeypatch
) -> None:
    """Vault xato qaytarsa xabar o'chirilmaydi."""
    import api.routes.files as files_module
    from services.vault import ArchiveResult
    fake_bot = _FakeBot()
    deleted_messages = []
    async def mock_delete(chat_id, message_id):
        deleted_messages.append((chat_id, message_id))
    fake_bot.delete_message = mock_delete
    client.app.state.bot = fake_bot

    info = TelegramFileInfo(
        file_id="FAKE-FILE-ID",
        file_unique_id="FAKE-UNIQUE-ID",
        file_name="salom.txt",
        file_size=5,
        mime_type="text/plain",
        category=FileCategory.DOCUMENT,
        caption=None,
    )
    monkeypatch.setattr(files_module, "extract_file_info", lambda message: info)
    
    async def mock_send_doc(**kwargs):
        return _FakeSentDocument(message_id=999)
    fake_bot.send_document = mock_send_doc

    class FakeVault:
        enabled = True
        async def archive_message(self, *args, **kwargs):
            return ArchiveResult(ok=False, error="archive failed")
            
    monkeypatch.setattr(files_module, "get_vault", lambda settings: FakeVault())

    token = _token(client, init_data)
    client.post(
        "/api/v1/files/upload",
        files={"file": ("salom.txt", b"hello", "text/plain")},
        headers=_headers(token),
    )
    assert not deleted_messages
