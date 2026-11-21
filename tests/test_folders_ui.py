"""🗂 Papkalar interfeysi — Mini App'dagi papka navigatsiyasi (5-bosqich).

Backend (``api/routes/folders.py``) to'liq tayyor edi, lekin Mini App'da papka
UI yo'q edi. Bu testlar frontend-backend shartnomasini mustahkamlaydi:

1. **Backend regressiyasi** — folder CRUD + fayl ko'chirish HTTP orqali
   (UI ishlatadigan ayni chaqiruvlar bilan);
2. **Frontend statik shartnoma** — ``folderBar`` markup'da bor, chips /
   breadcrumb / yaratish sheet'i / o'chirish / ``moveFileDialog`` JS'da mavjud
   va to'g'ri API'larga ulangan.

Frontend'da build bosqichi yo'q (bitta HTML), shuning uchun statik tekshiruvlar
"ishlamaydigan tugma" regressiyasini ushlaydigan yagona arzon vosita.
"""

# ``stream_client`` fixture'i ``tests.test_stream`` dan import qilinadi; pytest
# uni parametr nomi orqali topadi — ruff buni "qayta ta'rif" deb o'ylaydi.
# ruff: noqa: F811

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import make_file
from tests.test_stream import stream_client  # noqa: F401 - fixture importi

ROOT = Path(__file__).resolve().parents[1]
WEB_FILE = ROOT / "web" / "index.html"


def _web() -> str:
    return WEB_FILE.read_text(encoding="utf-8")


def _script() -> str:
    html = _web()
    scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    return max(scripts, key=len)


@pytest.fixture()
def auth_client(stream_client, user, init_data):
    """UI ishlatadigan header'lar bilan tayyor TestClient."""
    client = stream_client()
    response = client.post(
        "/api/v1/auth/telegram", json={"init_data": init_data(user.telegram_id)}
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


# --------------------------------------------------------------------------- #
# 1. Backend: UI ishlatadigan folder oqimlari (HTTP)
# --------------------------------------------------------------------------- #
async def test_folder_crud_flow(auth_client) -> None:
    """Yaratish → ro'yxat → bo'sh papkani o'chirish (UI'ning asosiy sikli)."""
    created = auth_client.post("/api/v1/folders", json={"name": "Hujjatlar"})
    assert created.status_code == 201, created.text
    folder = created.json()
    assert folder["name"] == "Hujjatlar"
    assert folder["files_count"] == 0

    listed = auth_client.get("/api/v1/folders")
    assert listed.status_code == 200
    assert any(item["id"] == folder["id"] for item in listed.json())

    deleted = auth_client.delete(f"/api/v1/folders/{folder['id']}")
    assert deleted.status_code == 200, deleted.text

    remaining = auth_client.get("/api/v1/folders").json()
    assert all(item["id"] != folder["id"] for item in remaining)


async def test_nested_folder_listing_by_parent(auth_client) -> None:
    """Chips bir sathni ko'rsatadi: ``?parent_id=`` filtri ishlashi shart."""
    root = auth_client.post("/api/v1/folders", json={"name": "Media"}).json()
    child = auth_client.post(
        "/api/v1/folders", json={"name": "2026", "parent_id": root["id"]}
    ).json()

    top = auth_client.get("/api/v1/folders").json()
    assert any(item["id"] == root["id"] for item in top)
    assert all(item["id"] != child["id"] for item in top), "bola papka ildiz sathida chiqmasin"

    inner = auth_client.get(f"/api/v1/folders?parent_id={root['id']}").json()
    assert [item["id"] for item in inner] == [child["id"]]

    # moveFileDialog uchun: deep=true butun daraxtni breadcrumb path bilan beradi.
    deep = auth_client.get("/api/v1/folders?deep=true").json()
    child_row = next(item for item in deep if item["id"] == child["id"])
    assert "Media" in child_row["path"] and "2026" in child_row["path"]


async def test_non_empty_folder_delete_conflicts(auth_client, storage, user) -> None:
    """Ichida fayl bor papka o'chmaydi: 409 ``folder_not_empty`` (UI matni bor)."""
    import uuid as uuid_module

    folder = auth_client.post("/api/v1/folders", json={"name": "Band"}).json()
    record = await make_file(storage, user.telegram_id, name="ichida.pdf")
    record_id = str(record.id)  # TestClient so'rovi sessiyani expire qiladi
    moved = auth_client.patch(
        f"/api/v1/files/{record_id}", json={"folder_id": folder["id"]}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["folder_id"] == str(uuid_module.UUID(folder["id"]))

    conflict = auth_client.delete(f"/api/v1/folders/{folder['id']}")
    assert conflict.status_code == 409, conflict.text
    body = conflict.json()
    code = (body.get("detail") or body.get("error") or {}).get("code")
    assert code == "folder_not_empty"


async def test_move_file_to_folder_and_back(auth_client, storage, user) -> None:
    """`moveFileDialog` oqimi: PATCH folder_id → papka filtri → move_to_root."""
    folder = auth_client.post("/api/v1/folders", json={"name": "Kino"}).json()
    record = await make_file(storage, user.telegram_id, name="film.mp4", category="video")
    record_id = str(record.id)  # TestClient so'rovi sessiyani expire qiladi

    moved = auth_client.patch(
        f"/api/v1/files/{record_id}", json={"folder_id": folder["id"]}
    )
    assert moved.status_code == 200, moved.text

    inside = auth_client.get(f"/api/v1/files?folder_id={folder['id']}").json()
    assert any(item["id"] == record_id for item in inside["items"])

    root_only = auth_client.get("/api/v1/files?folder_id=root").json()
    assert all(item["id"] != record_id for item in root_only["items"])

    back = auth_client.patch(
        f"/api/v1/files/{record_id}", json={"move_to_root": True}
    )
    assert back.status_code == 200, back.text
    assert back.json()["folder_id"] is None


# --------------------------------------------------------------------------- #
# 2. Frontend statik shartnoma
# --------------------------------------------------------------------------- #
def test_folder_bar_exists_in_markup() -> None:
    """`folderBar` paneli markup'da (groupTabs'dan keyin)."""
    html = _web()
    assert 'id="folderBar"' in html
    assert html.index('id="groupTabs"') < html.index('id="folderBar"')


def test_folder_ui_functions_present() -> None:
    """Barcha papka funksiyalari JS'da mavjud."""
    script = _script()
    for name in (
        "function renderFolderBar",
        "function enterFolder",
        "function openFolder",
        "async function createFolderSheet",
        "async function deleteCurrentFolder",
        "async function moveFileDialog",
        "async function loadFolders",
    ):
        assert name in script, f"yo'q: {name}"


def test_folder_state_wired_into_load_files() -> None:
    """Papka tanlovi fayllar so'roviga ulangan (``folder_id`` filtri)."""
    script = _script()
    load = script[script.index("async function loadFiles") : script.index("async function loadFolders")]
    assert "state.folderId" in load
    assert "folder_id=" in load
    assert "loadFolders()" in load, "papkalar ro'yxat bilan birga yangilanishi kerak"


def test_move_dialog_uses_folders_endpoint_and_patch() -> None:
    """`moveFileDialog`: GET /folders?deep=true → PATCH /files/{id}."""
    script = _script()
    move = script[script.index("async function moveFileDialog") : script.index("async function groupCounts")]
    assert '"/folders?deep=true"' in move
    assert '"PATCH"' in move
    assert "move_to_root" in move and "folder_id" in move
    assert "folder.path" in move, "sheet'da breadcrumb path ko'rsatilishi kerak"


def test_create_sheet_sends_parent_id() -> None:
    """Yangi papka joriy papka ichida yaratiladi (parent_id yuboriladi)."""
    script = _script()
    create = script[
        script.index("async function createFolderSheet")
        : script.index("async function deleteCurrentFolder")
    ]
    assert '"/folders"' in create and '"POST"' in create
    assert "parent_id: state.folderId" in create


def test_file_menu_offers_move_action() -> None:
    """Fayl menyusida "Ko'chirish" bandi bor va moveFileDialog'ga ulangan."""
    script = _script()
    menu = script[script.index("function fileMenu") : script.index("async function removeFile")]
    assert 'value: "move"' in menu
    assert "moveFileDialog(file)" in menu


def test_folder_bar_css_uses_design_tokens() -> None:
    """Panel mavjud dizayn tokenlaridan foydalanadi (ranglar o'zgarmagan)."""
    html = _web()
    css = html[html.index(".folder-bar") : html.index(".folder-chip.folder-new")]
    assert "var(--surface)" in css and "var(--border" in css
    assert "#0066FF" not in css and "#0F172A" not in css, (
        "yangi CSS'da xom ranglar bo'lmasin — faqat tokenlar"
    )
