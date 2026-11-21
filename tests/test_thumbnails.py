"""🖼️ Thumbnail'lar — Telegram preview'lari Mini App kartalarida.

Zanjir: aiogram media → ``extract_file_info`` (``thumbnail_file_id``) →
``StorageService.add_file`` → ``files.thumbnail_file_id`` ustuni →
``serialize_file`` → ``FileOut.thumbnail_url`` (imzolangan,
``purpose="thumb"``) → ``GET /stream/{ticket}`` thumbnail baytlarini oqizadi →
Mini App ``card-thumb`` ichida ``<img>``; rasm bo'lmasa emoji fallback.

Nega ``purpose="thumb"``: alohida endpoint ochmasdan mavjud ticket
infratuzilmasi qayta ishlatiladi — HMAC imzo, muddat, egalik hammasi tayyor.
Thumbnail'lar doim kichik (~KB), shuning uchun 20 MB devori ularga tegmaydi:
100 MB video ham kartada rasmini ko'rsata oladi.
"""

# ``stream_client`` fixture'i ``tests.test_stream`` dan import qilinadi; pytest
# uni parametr nomi orqali topadi — ruff buni "qayta ta'rif" deb o'ylaydi.
# ruff: noqa: F811

from __future__ import annotations

import re
from pathlib import Path

import pytest
from aiogram.types import PhotoSize, Video

from services.share import ShareService
from services.telegram import extract_file_info
from tests.conftest import make_file
from tests.test_stream import stream_client  # noqa: F401 - fixture importi
from tests.test_telegram_metadata import _message

ROOT = Path(__file__).resolve().parents[1]
WEB_FILE = ROOT / "web" / "index.html"


@pytest.fixture()
def share(storage, settings) -> ShareService:
    return ShareService(storage.session, settings=settings)


def _web() -> str:
    return WEB_FILE.read_text(encoding="utf-8")


async def _thumbed_file(storage, telegram_id: int, *, thumb_id: str | None = "thumb-1"):
    """`thumbnail_file_id` to'ldirilgan test fayli."""
    record = await make_file(storage, telegram_id, name="kino.mp4", category="video")
    record.thumbnail_file_id = thumb_id
    storage.session.add(record)
    await storage.session.commit()
    await storage.session.refresh(record)
    return record


# --------------------------------------------------------------------------- #
# 1. Metadata qatlamı: aiogram → thumbnail_file_id
# --------------------------------------------------------------------------- #
def test_video_thumbnail_extracted() -> None:
    """Video xabaridan ``thumbnail.file_id`` olinadi."""
    thumb = PhotoSize(file_id="th-1", file_unique_id="thu-1", width=90, height=90, file_size=800)
    video = Video(
        file_id="v-1", file_unique_id="vu-1", width=640, height=360, duration=10,
        thumbnail=thumb,
    )
    info = extract_file_info(_message(video=video))
    assert info is not None
    assert info.thumbnail_file_id == "th-1"
    assert "thumbnail_file_id" in info.to_kwargs()


def test_photo_uses_smallest_size_as_thumbnail() -> None:
    """Rasm: saqlash uchun eng katta, preview uchun eng kichik o'lcham."""
    small = PhotoSize(file_id="p-s", file_unique_id="u-s", width=90, height=90, file_size=700)
    large = PhotoSize(file_id="p-l", file_unique_id="u-l", width=1280, height=960, file_size=90000)
    info = extract_file_info(_message(photo=[small, large]))
    assert info is not None
    assert info.file_id == "p-l"              # asosiy fayl — eng katta
    assert info.thumbnail_file_id == "p-s"    # preview — eng kichik


def test_single_size_photo_has_no_thumbnail() -> None:
    """Bitta o'lchamli rasm: preview asl bilan bir xil bo'lardi — kerak emas."""
    only = PhotoSize(file_id="p-1", file_unique_id="u-1", width=90, height=90, file_size=900)
    info = extract_file_info(_message(photo=[only]))
    assert info is not None
    assert info.thumbnail_file_id is None


# --------------------------------------------------------------------------- #
# 2. API qatlamı: FileOut.thumbnail_url
# --------------------------------------------------------------------------- #
async def test_serialized_file_gets_thumbnail_url(storage, user, share, settings) -> None:
    """`thumbnail_file_id` bor fayl → imzolangan ``thumbnail_url``."""
    from api.serializers import serialize_file

    record = await _thumbed_file(storage, user.telegram_id)
    out = serialize_file(record, settings=settings, share=share, telegram_id=user.telegram_id)
    assert out.thumbnail_url, "thumbnail_url berilmadi"
    assert "/stream/" in out.thumbnail_url

    # Ticket purpose="thumb" bilan imzolangan bo'lishi shart.
    token = out.thumbnail_url.rsplit("/", 1)[-1]
    verified = share.verify_stream_ticket(token)
    assert verified.purpose == "thumb"
    assert verified.file_id == record.id


async def test_file_without_thumbnail_has_null_url(storage, user, share, settings) -> None:
    """Thumbnail yo'q → ``thumbnail_url = None`` (frontend emoji ko'rsatadi)."""
    from api.serializers import serialize_file

    record = await make_file(storage, user.telegram_id, name="doc.pdf")
    out = serialize_file(record, settings=settings, share=share, telegram_id=user.telegram_id)
    assert out.thumbnail_url is None


async def test_register_endpoint_accepts_thumbnail(
    stream_client, storage, user, init_data
) -> None:
    """``POST /files/register`` ``thumbnail_file_id`` ni qabul qiladi va saqlaydi."""
    client = stream_client()
    token_response = client.post(
        "/api/v1/auth/telegram", json={"init_data": init_data(user.telegram_id)}
    )
    assert token_response.status_code == 200, token_response.text
    headers = {"Authorization": f"Bearer {token_response.json()['access_token']}"}

    created = client.post(
        "/api/v1/files",
        json={
            "file_id": "AgAD-reg-1",
            "file_unique_id": "uniq-reg-1",
            "file_name": "klip.mp4",
            "file_size": 5 * 1024 * 1024,
            "mime_type": "video/mp4",
            "thumbnail_file_id": "th-reg-1",
        },
        headers=headers,
    )
    assert created.status_code in (200, 201), created.text
    assert created.json()["thumbnail_url"], "ro'yxatdan o'tgan fayl thumbnail_url olmadi"


# --------------------------------------------------------------------------- #
# 3. Stream qatlamı: purpose="thumb" thumbnail baytlarini oqizadi
# --------------------------------------------------------------------------- #
async def test_thumb_ticket_streams_thumbnail_bytes(
    stream_client, share, storage, user, monkeypatch
) -> None:
    """Thumb ticket: ``getFile`` ga **thumbnail** file_id ketadi, asosiysi emas."""
    import api.routes.stream as stream_module

    record = await _thumbed_file(storage, user.telegram_id, thumb_id="thumb-xyz")
    resolved: list[str] = []

    async def spy_resolve(file_id: str, _settings, _client) -> str:
        resolved.append(file_id)
        return "thumbs/small.jpg"

    monkeypatch.setattr(stream_module, "get_telegram_file_path", spy_resolve)
    ticket = share.issue_stream_ticket(record.id, user.telegram_id, purpose="thumb")

    client = stream_client()
    response = client.get(f"/api/v1/stream/{ticket.token}")
    assert response.status_code == 200, response.text
    assert resolved == ["thumb-xyz"], "asosiy file_id emas, thumbnail so'ralishi kerak"


async def test_thumb_ticket_without_thumbnail_is_404(
    stream_client, share, storage, user
) -> None:
    """Thumbnail'siz faylga thumb ticket → 404 ``thumbnail_not_found``."""
    record = await make_file(storage, user.telegram_id, name="doc.pdf")
    ticket = share.issue_stream_ticket(record.id, user.telegram_id, purpose="thumb")

    client = stream_client()
    response = client.get(f"/api/v1/stream/{ticket.token}")
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "thumbnail_not_found"


async def test_thumb_bypasses_20mb_wall(stream_client, share, storage, user) -> None:
    """100 MB video ham kartada rasm ko'rsatadi: devor thumb'ga tegmaydi."""
    record = await make_file(
        storage, user.telegram_id, name="katta.mp4", size=100 * 1024 * 1024, category="video"
    )
    record.thumbnail_file_id = "thumb-big"
    storage.session.add(record)
    await storage.session.commit()

    ticket = share.issue_stream_ticket(record.id, user.telegram_id, purpose="thumb")
    client = stream_client()
    assert client.get(f"/api/v1/stream/{ticket.token}").status_code == 200

    # Nazorat: xuddi shu faylning ODDIY stream'i esa devorga uriladi.
    stream_ticket = share.issue_stream_ticket(record.id, user.telegram_id)
    assert client.get(f"/api/v1/stream/{stream_ticket.token}").status_code == 413


async def test_thumb_does_not_touch_view_counter(
    stream_client, share, storage, user, session
) -> None:
    """Ro'yxatni ochish "ko'rish" emas: thumb hisoblagichni oshirmaydi."""
    record = await _thumbed_file(storage, user.telegram_id)
    record_id = record.id  # expire'dan keyin atributga tegmaslik uchun
    before = int(record.views or 0)

    ticket = share.issue_stream_ticket(record_id, user.telegram_id, purpose="thumb")
    client = stream_client()
    assert client.get(f"/api/v1/stream/{ticket.token}").status_code == 200

    # Atribut orqali emas, to'g'ridan-to'g'ri ustun so'rovi bilan o'qiymiz:
    # expire qilingan obyektda atributga murojaat async sessiyada sync IO'ga
    # urinib MissingGreenlet beradi.
    from sqlalchemy import select

    from db.models import File

    session.expire_all()
    views_now = (
        await session.execute(select(File.views).where(File.id == record_id))
    ).scalar_one()
    assert int(views_now or 0) == before


# --------------------------------------------------------------------------- #
# 4. Migratsiya va frontend shartnomasi
# --------------------------------------------------------------------------- #
def test_migrate_plan_includes_thumbnail_column() -> None:
    """`scripts/migrate.py` ustunni ADD COLUMN bilan qo'shadi (ma'lumot o'chmaydi)."""
    text = (ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")
    assert '("thumbnail_file_id", "VARCHAR(255)")' in text
    assert "DROP" not in text.upper() or "DROP TABLE" not in text.upper(), (
        "migratsiya hech narsani o'chirmasligi kerak"
    )


def test_card_renders_img_with_emoji_fallback() -> None:
    """Kartada ``<img class=thumb-img>``; xato bo'lsa emoji qaytadi."""
    html = _web()
    card = html[html.index("function fileCard") : html.index("function miniButton")]
    assert 'class: "thumb-img"' in card
    assert "file.thumbnail_url" in card
    assert "onerror" in card, "rasm yuklanmasa emoji fallback bo'lishi kerak"
    assert "toMediaUrl(file.thumbnail_url" in card, (
        "thumbnail URL ham proksi orqasida nisbiy bo'lishi kerak (mixed content)"
    )
    # CSS: rasm kartani to'liq qoplaydi.
    assert re.search(r"\.card-thumb \.thumb-img\s*{[^}]*object-fit:\s*cover", html)


def test_frontend_localizes_thumbnail_error() -> None:
    """Yangi xato kodi lug'atda (shartnoma testi talabi)."""
    assert "thumbnail_not_found" in _web()
