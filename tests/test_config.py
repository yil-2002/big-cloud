"""⚙️ Sozlamalar: .env.example fayli haqiqatan ham yuklanadigan bo'lishi kerak."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from config import Settings, get_settings

ROOT = Path(__file__).resolve().parent.parent


def _load_example_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """``.env.example`` ni vaqtinchalik papkaga nusxalab, ``.env`` sifatida yuklaydi."""
    workdir = tmp_path / "proj"
    shutil.copytree(
        ROOT,
        workdir,
        ignore=shutil.ignore_patterns(
            "__pycache__", ".pytest_cache", "venv", ".venv", "*.db", "*.tar.gz"
        ),
    )
    shutil.copy(workdir / ".env.example", workdir / ".env")
    monkeypatch.chdir(workdir)
    # Env o'zgaruvchilari .env'dan ustun — toza yuklash uchun ularni olib tashlaymiz.
    for key in list(os.environ):
        if key.isupper() and key not in {"PATH", "HOME", "LANG", "PWD", "PYTHONPATH"}:
            monkeypatch.delenv(key, raising=False)
    # ``conftest`` production ``.env`` ni sukut bo'yicha o'chiradi; bu test esa
    # ataylab vaqtinchalik ``.env`` ni yuklaydi.
    monkeypatch.setenv("LOAD_DOTENV", "1")
    get_settings.cache_clear()
    try:
        return Settings()
    finally:
        get_settings.cache_clear()


import os


def test_env_example_loads_without_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Eng muhim tekshiruv: namunaviy .env ilovani yiqitmasligi kerak."""
    settings = _load_example_env(tmp_path, monkeypatch)
    assert settings.bot.username == "Huge_cloud_bot"
    assert settings.app.language == "uz"


def test_empty_admin_ids_does_not_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``ADMIN_IDS=`` (bo'sh) JSON xatosiga olib kelmasligi kerak."""
    settings = _load_example_env(tmp_path, monkeypatch)
    # ``ADMIN_ID`` validator orqali ``admin_ids`` ga birlashtiriladi,
    # shuning uchun bo'sh ``ADMIN_IDS=`` dan keyin ham bitta id qoladi.
    assert settings.bot.admin_ids == (111222333,)
    assert settings.bot.all_admin_ids == (111222333,)
    assert settings.bot.is_admin(111222333) is True
    assert settings.bot.is_admin(1) is False


def test_cors_wildcard_parses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``CORS_ORIGINS=*`` — JSON emas, lekin to'g'ri o'qiladi."""
    settings = _load_example_env(tmp_path, monkeypatch)
    assert settings.api.cors_origins == ("*",)


def test_unlimited_quota_is_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sukut kvota 0 = cheksiz va UI matni o'zbekcha."""
    settings = _load_example_env(tmp_path, monkeypatch)
    assert settings.storage.default_quota == 0
    assert settings.storage.unlimited_label == "Cheksiz (Unlimited)"


def test_legacy_mechanics_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy xatti-harakatlar: sahifa hajmi 5, media ko'rish, stream proxy."""
    settings = _load_example_env(tmp_path, monkeypatch)
    assert settings.storage.page_size == 5
    assert settings.storage.browse_send_media is True
    assert settings.storage.stream_proxy_enabled is True
    assert settings.storage.dedupe_enabled is True


def test_jwt_secret_derived_when_blank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``JWT_SECRET=`` bo'sh bo'lsa — ``BOT_TOKEN`` dan hosil qilinadi."""
    settings = _load_example_env(tmp_path, monkeypatch)
    secret = settings.security.jwt_secret.get_secret_value()
    assert secret and len(secret) >= 32
    assert secret != "change-me-in-production-please-32ch"


def test_webhook_path_gets_leading_slash() -> None:
    """``WEBHOOK_PATH=api/webhook`` -> ``/api/webhook`` (validator)."""
    os.environ.setdefault("BOT_TOKEN", "123:abc")
    settings = Settings(api={"webhook_path": "api/webhook"})
    assert settings.api.webhook_path == "/api/webhook"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Neon: sslmode + channel_binding (asyncpg ikkalasini ham tanimaydi).
        (
            "postgresql://u:p@ep-x.neon.tech/db?sslmode=require&channel_binding=require",
            "postgresql+asyncpg://u:p@ep-x.neon.tech/db?ssl=require",
        ),
        ("postgres://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgresql+asyncpg://u:p@h/db?ssl=require", "postgresql+asyncpg://u:p@h/db?ssl=require"),
        ("sqlite:///./data/x.sqlite3", "sqlite+aiosqlite:///./data/x.sqlite3"),
    ],
)
def test_cloud_postgres_dsn_is_asyncpg_compatible(raw: str, expected: str) -> None:
    """Bulutli Postgres DSN'i asyncpg uchun to'g'rilanadi (VPS'siz rejim)."""
    from config import DatabaseSettings

    assert DatabaseSettings(DATABASE_URL=raw).url == expected
