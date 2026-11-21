"""🌐 HTTP API: initData imzosi, JWT, auth router'lari, fayl ro'yxati, system."""

from __future__ import annotations

import time

import pytest

from api.security import (
    InitDataError,
    build_init_data,
    create_session_token,
    decode_session_token,
    validate_init_data,
)
from config import Settings


# --------------------------------------------------------------------------- #
# initData validatsiyasi (Mini App kirishi)
# --------------------------------------------------------------------------- #
def test_valid_init_data_is_accepted(settings: Settings, init_data) -> None:
    payload = validate_init_data(init_data(777, username="sobir"), settings=settings)
    assert payload.user is not None
    assert payload.user.id == 777
    assert payload.user.username == "sobir"


def test_tampered_init_data_is_rejected(settings: Settings, init_data) -> None:
    """Imzo buzilsa (boshqa foydalanuvchi id) — rad etiladi."""
    raw = init_data(777)
    tampered = raw.replace("%22id%22%3A777", "%22id%22%3A999").replace("id%22%3A777", "id%22%3A999")
    with pytest.raises(InitDataError):
        validate_init_data(tampered + "&extra=1", settings=settings)


def test_wrong_bot_token_is_rejected(settings: Settings) -> None:
    """Boshqa bot tokeni bilan imzolangan initData o'tmaydi."""
    foreign = build_init_data(telegram_id=777, bot_token="999:OTHER-TOKEN")
    with pytest.raises(InitDataError):
        validate_init_data(foreign, settings=settings)


def test_expired_init_data_is_rejected(settings: Settings) -> None:
    """``auth_date`` eskirgan bo'lsa — rad etiladi (replay himoyasi)."""
    stale = build_init_data(
        telegram_id=777, bot_token=settings.bot_token, auth_date=int(time.time()) - 86400 * 3
    )
    with pytest.raises(InitDataError):
        validate_init_data(stale, settings=settings, max_age=3600)


def test_garbage_init_data_is_rejected(settings: Settings) -> None:
    for broken in ("", "hash=abc", "user=%7B%7D", "%%%not-a-query%%%"):
        with pytest.raises(InitDataError):
            validate_init_data(broken, settings=settings)


# --------------------------------------------------------------------------- #
# JWT sessiya tokenlari
# --------------------------------------------------------------------------- #
def test_session_token_roundtrip(settings: Settings) -> None:
    token = create_session_token(777, settings=settings, session_id="sess-1")
    claims = decode_session_token(token, settings=settings)
    assert claims["tg_id"] == 777
    assert claims["sub"] == "777"
    assert claims["sid"]                                # sessiya identifikatori
    assert claims["exp"] > claims["iat"]


def test_jwt_secret_is_derived_not_constant(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``JWT_SECRET`` berilmasa ``BOT_TOKEN`` dan hosil qilinadi (ma'lum konstanta emas)."""
    secret = settings.security.jwt_secret.get_secret_value()
    assert secret and secret != "change-me-in-production-please-32ch"
    assert len(secret) >= 32

    monkeypatch.setenv("BOT_TOKEN", "999:butunlay-boshqa-token")
    monkeypatch.delenv("JWT_SECRET", raising=False)
    other = Settings()
    assert other.security.jwt_secret.get_secret_value() != secret


def test_session_token_from_other_secret_rejected(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boshqa kalit bilan imzolangan token qabul qilinmaydi."""
    token = create_session_token(777, settings=settings)
    monkeypatch.setenv("JWT_SECRET", "boshqa-kalit-" + "x" * 40)
    other = Settings()
    with pytest.raises(Exception):  # noqa: B017
        decode_session_token(token, settings=other)


# --------------------------------------------------------------------------- #
# System router'lari (health-check / versiya)
# --------------------------------------------------------------------------- #
def test_healthz_returns_ok(session, client) -> None:
    """PaaS health-check: ``GET /healthz`` -> har doim oddiy ``200 OK``."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "OK"


def test_root_serves_mini_app_for_browser(session, client) -> None:
    """Brauzer/Telegram ``GET /`` da Mini App sahifasini oladi."""
    response = client.get("/", headers={"Accept": "text/html,application/xhtml+xml"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "telegram-web-app.js" in response.text


def test_root_returns_plain_ok_for_health_probe(session, client) -> None:
    """Legacy health-check (``curl /``) ``"OK"`` matnini oladi — 200."""
    response = client.get("/")
    assert response.status_code == 200
    assert response.text == "OK"


def test_root_returns_json_when_requested(session, client) -> None:
    """``Accept: application/json`` yoki ``?format=json`` -> batafsil holat."""
    body = client.get("/", headers={"Accept": "application/json"}).json()
    assert body["status"] == "ok"
    assert client.get("/?format=json").json()["status"] == "ok"


def test_index_html_is_reachable(session, client) -> None:
    """Mini App'ni aniq manzildan ham ochish mumkin."""
    response = client.get("/index.html")
    assert response.status_code == 200
    assert "<html" in response.text.lower()


def test_system_health_json(session, client) -> None:
    response = client.get("/api/v1/system/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "up"
    assert "version" in body


def test_system_version_reports_features(session, client) -> None:
    body = client.get("/api/v1/system/version").json()
    assert body["version"] == "2.0.0"
    features = body["features"]
    assert features["quota"] == 0
    assert features["quota_label"] == "Cheksiz (Unlimited)"
    assert features["page_size"] == 5
    assert features["archive_enabled"] is True


# --------------------------------------------------------------------------- #
# Auth router'lari
# --------------------------------------------------------------------------- #
def _auth_headers(init_data) -> dict[str, str]:
    return {"X-Telegram-Init-Data": init_data(777, username="sobir")}


def test_auth_telegram_returns_token_and_user(session, client, init_data) -> None:
    """Mini App birinchi qadami: ``POST /auth/telegram`` -> JWT + profil."""
    response = client.post("/api/v1/auth/telegram", json={"init_data": init_data(777)})
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["token"] == body["access_token"]      # Mini App ikkalasini ham o'qiydi
    assert body["user"]["telegram_id"] == 777
    assert body["user"]["unlimited"] is True
    assert body["user"]["quota_label"] == "Cheksiz (Unlimited)"


def test_auth_telegram_rejects_bad_signature(session, client, settings) -> None:
    foreign = build_init_data(telegram_id=777, bot_token="999:OTHER")
    response = client.post("/api/v1/auth/telegram", json={"init_data": foreign})
    assert response.status_code in (401, 403)


def test_auth_me_requires_identity(session, client) -> None:
    assert client.get("/api/v1/auth/me").status_code in (401, 403)
    assert client.get("/api/v1/auth/me/stats").status_code in (401, 403)


def test_register_login_flow_over_http(session, client, init_data) -> None:
    """``POST /auth/register`` -> ``POST /auth/login`` -> ``GET /auth/me``."""
    headers = _auth_headers(init_data)

    created = client.post(
        "/api/v1/auth/register",
        json={"login": "alisher", "password": "parol123"},
        headers=headers,
    )
    assert created.status_code in (200, 201), created.text
    token = created.json().get("access_token") or created.json().get("token")
    assert token

    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["login"] == "alisher"
    assert me.json()["is_authenticated"] is True

    stats = client.get("/api/v1/auth/me/stats", headers={"Authorization": f"Bearer {token}"})
    assert stats.status_code == 200


def test_login_with_wrong_password_returns_401(session, client, init_data) -> None:
    headers = _auth_headers(init_data)
    client.post("/api/v1/auth/register", json={"login": "alisher", "password": "parol123"}, headers=headers)

    wrong = client.post("/api/v1/auth/login", json={"login": "alisher", "password": "xato123"})
    assert wrong.status_code == 401
    detail = wrong.json()["detail"]
    assert detail["code"] == "wrong_password"
    # Qolgan urinishlar soni mijozga qaytariladi (Mini App ko'rsatadi).
    assert detail["details"]["attempts_left"] >= 0
    assert "urinish" in detail["message"]


def test_login_unknown_account_returns_404(session, client) -> None:
    response = client.post("/api/v1/auth/login", json={"login": "yoq", "password": "parol123"})
    assert response.status_code == 404


def test_register_without_identity_returns_401(session, client) -> None:
    """Telegram identifikatorisiz ro'yxatdan o'tish rad etiladi."""
    response = client.post("/api/v1/auth/register", json={"login": "alisher", "password": "parol123"})
    assert response.status_code in (401, 422)


# --------------------------------------------------------------------------- #
# Files router
# --------------------------------------------------------------------------- #
def test_files_list_requires_auth(session, client) -> None:
    assert client.get("/api/v1/files").status_code in (401, 403)


def test_files_list_returns_empty_page(session, client, init_data, user) -> None:
    response = client.post("/api/v1/auth/telegram", json={"init_data": init_data(777)})
    token = response.json()["access_token"]
    listing = client.get("/api/v1/files", headers={"Authorization": f"Bearer {token}"})
    assert listing.status_code == 200
    body = listing.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["page"] == 1


async def test_files_list_serializes_cloud_rows(session, client, storage, init_data, user) -> None:
    """Bazadagi fayllar API orqali to'g'ri seriallanadi (kvota/arxiv maydonlari)."""
    from tests.conftest import make_file

    user = (await storage.get_or_create_user(telegram_id=777, username="sobir"))[0]
    record = await make_file(storage, user.telegram_id, name="film.mp4", size=4096)
    record.mark_archived(chat_id="-1001234567890", message_id=4242)
    storage.session.add(record)
    await storage.session.commit()

    token = client.post("/api/v1/auth/telegram", json={"init_data": init_data(777)}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    listing = client.get("/api/v1/files", headers=headers)
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["name"] == "film.mp4"
    assert item["size"] == 4096
    assert item["archived"] is True
    assert item["archive_message_id"] == 4242
    assert item["is_pinned"] is False

    # Guruh filtri: 🎬 Medialar
    media = client.get("/api/v1/files?group=media", headers=headers).json()
    assert media["total"] == 1
    audio = client.get("/api/v1/files?group=audio", headers=headers).json()
    assert audio["total"] == 0


def test_files_group_param_is_validated(session, client, init_data, user) -> None:
    """Noma'lum guruh -> 422 (Literal cheklovi)."""
    token = client.post("/api/v1/auth/telegram", json={"init_data": init_data(777)}).json()["access_token"]
    response = client.get(
        "/api/v1/files?group=bunday_guruh_yoq", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 422


async def test_extensionless_file_returns_mime_and_category(
    session, client, storage, init_data, user
) -> None:
    """Kengaytmasiz ``tmp_3295``: API har doim ``mime_type``+``category`` beradi.

    Yozuvda kategoriya eski holatda "other" qolib ketgan bo'lsa ham, mime
    mavjud bo'lsa hisoblab ``video`` qaytariladi — Mini App pleeri shunga
    qarab ochiladi.
    """
    from db.models import FileCategory

    user = (await storage.get_or_create_user(telegram_id=777, username="sobir"))[0]
    stored = await storage.add_file(
        telegram_id=user.telegram_id,
        file_id="AgAD-tmp3295",
        file_unique_id="uniq-tmp3295",
        file_name="tmp_3295",
        file_size=190_246_808,
        mime_type="video/mp4",
    )
    record = stored.record
    record.category = FileCategory.OTHER          # eski/buzilgan holat
    storage.session.add(record)
    await storage.session.commit()

    token = client.post("/api/v1/auth/telegram", json={"init_data": init_data(777)}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    listing = client.get("/api/v1/files", headers=headers)
    assert listing.status_code == 200
    item = listing.json()["items"][0]
    assert item["mime_type"] == "video/mp4"
    assert item["category"] == "video"
    assert item["is_streamable"] is True

    detail = client.get(f"/api/v1/files/{record.id}", headers=headers)
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["category"] == "video"
    assert payload["mime_type"] == "video/mp4"
    assert payload["stream_url"], "video uchun ijro havolasi berilishi kerak"


async def test_extensionless_unknown_file_stays_other(
    session, client, storage, init_data, user
) -> None:
    """Kontent ham, mime ham noma'lum bo'lsa — ``other`` (pleer ochilmaydi)."""
    user = (await storage.get_or_create_user(telegram_id=777, username="sobir"))[0]
    await storage.add_file(
        telegram_id=user.telegram_id,
        file_id="AgAD-mystery",
        file_unique_id="uniq-mystery",
        file_name="tmp_0001",
        file_size=10,
        mime_type="application/octet-stream",
    )
    token = client.post("/api/v1/auth/telegram", json={"init_data": init_data(777)}).json()["access_token"]
    item = client.get(
        "/api/v1/files", headers={"Authorization": f"Bearer {token}"}
    ).json()["items"][0]
    assert item["category"] == "other"
    assert item["is_streamable"] is False



def test_openapi_schema_is_generated(session, client) -> None:
    """/docs ishlashi uchun OpenAPI sxemasi xatosiz yig'iladi."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/auth/telegram" in paths
    assert "/api/v1/files" in paths
    assert "/api/v1/system/health" in paths


def test_login_is_throttled_per_ip(session, client, init_data, settings) -> None:
    """Anonim endpoint ham limitsiz emas: ortiqcha urinishlar -> ``429``."""
    settings.security.rate_limit_enabled = True
    try:
        client.post(
            "/api/v1/auth/register",
            json={"login": "alisher", "password": "parol123"},
            headers=_auth_headers(init_data),
        )
        codes: list[int] = []
        for _ in range(15):
            response = client.post(
                "/api/v1/auth/login", json={"login": "alisher", "password": "xato123"}
            )
            codes.append(response.status_code)
        assert 429 in codes, codes
        denied = client.post("/api/v1/auth/login", json={"login": "alisher", "password": "xato123"})
        assert denied.status_code == 429
        assert "Retry-After" in denied.headers or "retry-after" in {
            key.lower() for key in denied.headers
        }
    finally:
        settings.security.rate_limit_enabled = False


async def test_files_group_pinned_returns_only_pinned(session, client, storage, init_data, user) -> None:
    """``group=pinned`` faqat muqimlangan fayllarni qaytaradi.

    Regressiya: ``list_files`` bu guruhni qayta ishlamasdi — ``FILE_GROUPS`` da
    "pinned" yo'qligi sababli filtr qo'yilmay, **barcha** fayllar qaytarilar edi
    (qidiruv yo'li esa to'g'ri ishlar, ya'ni ikki yo'l bir-biriga zid edi).
    """
    from tests.conftest import make_file

    user = (await storage.get_or_create_user(telegram_id=777, username="pinner"))[0]
    await make_file(storage, user.telegram_id, name="muhim1.mp4", pinned=True)
    await make_file(storage, user.telegram_id, name="muhim2.pdf", pinned=True)
    await make_file(storage, user.telegram_id, name="oddiy.mp3")

    token = client.post(
        "/api/v1/auth/telegram", json={"init_data": init_data(user.telegram_id)}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get("/api/v1/files?group=pinned", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2, "faqat ikkita muqimlangan fayl kutiladi"
    assert {item["name"] for item in body["items"]} == {"muhim1.mp4", "muhim2.pdf"}
    assert all(item["is_pinned"] for item in body["items"])

    # Legacy ``pinned=true`` bayrog'i bir xil natija beradi
    assert client.get("/api/v1/files?pinned=true", headers=headers).json()["total"] == 2
    # Boshqa guruhlar buzilmadi
    assert client.get("/api/v1/files?group=all", headers=headers).json()["total"] == 3


async def test_search_with_group_pinned_matches_listing(session, client, storage, init_data) -> None:
    """Qidiruv va ro'yxat yo'llari ``group=pinned`` uchun bir xil natija beradi."""
    from tests.conftest import make_file

    user = (await storage.get_or_create_user(telegram_id=777, username="pinner2"))[0]
    await make_file(storage, user.telegram_id, name="muhim_hisobot.pdf", pinned=True)
    await make_file(storage, user.telegram_id, name="oddiy_hisobot.pdf")

    listed, _ = await storage.list_files(user.telegram_id, group="pinned")
    searched = await storage.search_files(user.telegram_id, "hisobot", group="pinned")
    assert [f.file_name for f in listed] == ["muhim_hisobot.pdf"]
    assert [f.file_name for f in searched] == ["muhim_hisobot.pdf"]


async def test_empty_folder_id_is_treated_as_unset(session, client, storage, init_data, user) -> None:
    """``folder_id=`` (bo'sh qiymat) filtrsiz ro'yxat qaytaradi — 500 emas."""
    from tests.conftest import make_file

    user = (await storage.get_or_create_user(telegram_id=777, username="fold"))[0]
    await make_file(storage, user.telegram_id, name="a.mp4")
    await make_file(storage, user.telegram_id, name="b.mp4")

    token = client.post(
        "/api/v1/auth/telegram", json={"init_data": init_data(user.telegram_id)}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    for query in ("folder_id=", "folder_id=%20", "folder_id=root", "folder_id=-"):
        response = client.get(f"/api/v1/files?{query}", headers=headers)
        assert response.status_code == 200, f"{query} -> {response.status_code}"
    assert client.get("/api/v1/files?folder_id=", headers=headers).json()["total"] == 2
    # ``root`` faqat ildiz darajasidagi fayllarni ko'rsatadi (bu yerda ikkalasi ham)
    assert client.get("/api/v1/files?folder_id=root", headers=headers).json()["total"] == 2


async def test_invalid_folder_id_returns_422_not_500(session, client, storage, init_data, user) -> None:
    """Buzilgan UUID → loyiya xato konvertida 422 (avval ``ValueError`` → 500 edi)."""
    # ``user`` fixture 777 ni allaqachon (tasdiqlangan holda) yaratadi; shu yerda
    # qayta get_or_create chaqirish ikki sessiya orasida SQLite yozuv qulfiga
    # olib kelardi ("database is locked").
    token = client.post(
        "/api/v1/auth/telegram", json={"init_data": init_data(user.telegram_id)}
    ).json()["access_token"]

    response = client.get(
        "/api/v1/files?folder_id=buzilgan-uuid", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "validation_error"
    assert detail["details"]["folder_id"] == "buzilgan-uuid"


# --------------------------------------------------------------------------- #
# 🔗 Ommaviy havola payload'i (anonim foydalanuvchi tomoni)
# --------------------------------------------------------------------------- #
async def _shared_file(
    client,
    storage,
    init_data,
    *,
    name: str,
    category: str | None = None,
    pass_code: str | None = None,
    make_public: bool = False,
):
    """Fayl yaratib, HTTP orqali ulashish havolasini chiqaradi."""
    from tests.conftest import make_file

    user = (await storage.get_or_create_user(telegram_id=777, username="sharer"))[0]
    if not user.is_authenticated:  # 6-bosqich darvozasidan o'tish uchun
        user.is_authenticated = True
        storage.session.add(user)
        await storage.session.commit()
    record = await make_file(storage, user.telegram_id, name=name, category=category)
    token = client.post(
        "/api/v1/auth/telegram", json={"init_data": init_data(user.telegram_id)}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    body: dict[str, object] = {}
    if pass_code is not None:
        body["pass_code"] = pass_code
    if make_public:
        body["make_public"] = True

    response = client.post(f"/api/v1/files/{record.id}/share", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return user, record, headers, response.json()


async def test_public_share_payload_offers_download_for_document(
    session, client, storage, init_data
) -> None:
    """Hujjat ijro qilinmaydi, lekin havola orqali **yuklab olinishi** shart.

    Regressiya: ``_public_payload`` manzillarni faqat ``is_streamable`` fayllar
    uchun berardi — natijada ulashilgan PDF/ZIP/APK qabul qiluvchida faqat
    metadata ko'rsatilar, yuklab olish tugmasi esa yo'q edi.
    """
    _, _, _, link = await _shared_file(
        client, storage, init_data, name="shartnoma.pdf", category="document"
    )

    response = client.get(f"/api/v1/shares/public/{link['token']}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["is_streamable"] is False, "hujjat onlayn ijro qilinmaydi"
    assert payload["download_url"], "yuklab olish manzili berilishi kerak"
    assert payload["stream_url"], "egasi tomonidagi /stream shartnomasi bilan bir xil"
    assert payload["name"] == "shartnoma.pdf"


async def test_public_share_payload_for_video_is_streamable(
    session, client, storage, init_data
) -> None:
    """Video uchun ikkala manzil ham beriladi va ``is_streamable=True``."""
    _, _, _, link = await _shared_file(
        client, storage, init_data, name="film.mp4", category="video", make_public=True
    )

    payload = client.get(f"/api/v1/shares/public/{link['token']}").json()
    assert payload["is_streamable"] is True
    assert payload["stream_url"] and payload["download_url"]
    assert "?download=1" in payload["download_url"], "yuklab olish rejimi ? bilan belgilanadi"
    assert "&download=1" not in payload["download_url"]


async def test_pass_code_link_hides_urls_until_unlocked(
    session, client, storage, init_data
) -> None:
    """Parolli havola: kod berilmaguncha fayl manzillari ochilmaydi."""
    _, _, _, link = await _shared_file(
        client, storage, init_data, name="maxfiy.pdf", category="document", pass_code="2026"
    )
    token = link["token"]

    check = client.get(f"/api/v1/shares/public/{token}/check").json()
    assert check["exists"] is True and check["active"] is True
    assert check["requires_pass_code"] is True

    # Route'lar DomainError'ni HTTPException'ga o'raydi: konvert {"detail": {...}}
    blocked = client.get(f"/api/v1/shares/public/{token}")
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "invalid_pass_code"

    wrong = client.post(f"/api/v1/shares/public/{token}/unlock", json={"pass_code": "0000"})
    assert wrong.status_code == 403
    assert wrong.json()["detail"]["code"] == "invalid_pass_code"

    unlocked = client.post(f"/api/v1/shares/public/{token}/unlock", json={"pass_code": "2026"})
    assert unlocked.status_code == 200
    payload = unlocked.json()
    assert payload["name"] == "maxfiy.pdf"
    assert payload["download_url"], "kod to'g'ri kiritilgach yuklab olish ochiladi"

    # GET ham to'g'ri kod bilan ishlaydi (query parametri orqali)
    via_query = client.get(f"/api/v1/shares/public/{token}?pass_code=2026")
    assert via_query.status_code == 200
    assert via_query.json()["download_url"]


async def test_public_view_counter_counts_payload_views_only(
    session, client, storage, init_data
) -> None:
    """Ko'rishlar hisoblagichi: payload ochilganda oshadi, ``unlock``/``count_view=false`` da emas."""
    _, _, _, link = await _shared_file(
        client, storage, init_data, name="hisob.mp4", category="video", make_public=True
    )
    token = link["token"]

    before = client.get(f"/api/v1/shares/public/{token}/check").json()["views"]
    client.get(f"/api/v1/shares/public/{token}")
    after = client.get(f"/api/v1/shares/public/{token}/check").json()["views"]
    assert after == before + 1

    client.get(f"/api/v1/shares/public/{token}?count_view=false")
    assert client.get(f"/api/v1/shares/public/{token}/check").json()["views"] == after


async def test_public_payload_has_no_urls_when_proxy_disabled(
    session, client, storage, init_data, settings, monkeypatch
) -> None:
    """``STREAM_PROXY_ENABLED=false`` bo'lsa manzillar berilmaydi (501 mantiqiga mos)."""
    monkeypatch.setattr(settings.storage, "stream_proxy_enabled", False)
    _, _, _, link = await _shared_file(
        client, storage, init_data, name="proxy_ochoff.mp4", category="video", make_public=True
    )

    payload = client.get(f"/api/v1/shares/public/{link['token']}").json()
    assert payload["stream_url"] is None
    assert payload["download_url"] is None
    assert payload["is_streamable"] is True, "fayl turi o'zgarmaydi, faqat manzil berilmaydi"


async def test_unknown_public_token_reports_missing(session, client) -> None:
    """Noma'lum token uchun ``check`` 404 emas, ``exists=False`` qaytaradi."""
    response = client.get("/api/v1/shares/public/bunday-token-yoq/check")
    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is False and body["active"] is False

    payload = client.get("/api/v1/shares/public/bunday-token-yoq")
    assert payload.status_code == 404
    assert payload.json()["detail"]["code"] == "link_not_found"
