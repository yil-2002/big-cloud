"""🚀 Deploy fayllari: sintaksis va o'zaro moslik (README bilan ham).

Bu fayllar kod emas, shuning uchun ularni hech qanday test qamramaydi — lekin
xato (masalan, health-check yo'li yoki start buyrug'i) production'da darhol
ko'rinadi. Shu sababli bu yerda YAML/ini sintaksisi va asosiy kelishuvlar
tekshiriladi.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

yaml = pytest.importorskip("yaml", reason="PyYAML dev bog'liqligi kerak")


def test_render_yaml_is_valid_and_consistent() -> None:
    """``render.yaml``: web servis, health-check ``/healthz``, webhook yoqilgan."""
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    assert blueprint["databases"], "PostgreSQL bazasi e'lon qilinmagan"

    web = next(service for service in blueprint["services"] if service["type"] == "web")
    assert web["healthCheckPath"] == "/healthz"
    assert "main.py" in web["startCommand"]

    env = {item["key"]: item for item in web["envVars"]}
    # Webhook rejimi: polling bilan bir vaqtda ishlamasligi uchun.
    assert env["WEBHOOK_ENABLED"]["value"] == "true"
    assert env["WEBHOOK_PATH"]["value"] == "/api/webhook"
    # Cheksiz kvota va Mini App.
    assert env["STORAGE_QUOTA"]["value"] == "0"
    assert env["SERVE_WEB"]["value"] == "true"
    # Production'da Swagger yopiq.
    assert env["API_DOCS_ENABLED"]["value"] == "false"
    # Maxfiy qiymatlar qat'iy belgilanmagan (operator to'ldiradi).
    for secret in ("BOT_TOKEN", "ADMIN_ID", "ARCHIVE_CHANNEL_ID"):
        assert env[secret].get("sync") is False, secret
    # JWT/webhook sirlari Render tomonidan yaratiladi.
    assert env["JWT_SECRET"]["generateValue"] is True
    assert env["WEBHOOK_SECRET"]["generateValue"] is True


def test_docker_compose_is_valid() -> None:
    """``docker-compose.yml``: postgres + redis + app, health-check ``/healthz``."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert {"postgres", "redis", "app"} <= set(services)

    app = services["app"]
    assert app["healthcheck"]["test"][-1].endswith("/healthz")
    assert app["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert app["depends_on"]["redis"]["condition"] == "service_healthy"
    # Baza va kesh manzillari konteyner tarmog'idagi nomlar bilan.
    assert "postgres:5432" in app["environment"]["DATABASE_URL"]
    assert "redis:6379" in app["environment"]["REDIS_URL"]


def test_github_workflow_is_valid() -> None:
    """Bot workflow: soatlik zaxira cron, bitta concurrency guruhi, runner skripti."""
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "bot.yml").read_text(encoding="utf-8"))
    # ``on`` YAML'da ``True`` ga aylanib qoladi (1.1 boolean) — shuni hisobga olamiz.
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["schedule"][0]["cron"]
    assert "workflow_dispatch" in triggers
    # Ikki bot bir vaqtda polling qilmasin (409 Conflict).
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["permissions"]["actions"] == "write"

    job = workflow["jobs"]["run"]
    assert job["timeout-minutes"] <= 360
    assert job["env"]["WEBHOOK_ENABLED"] == "false"
    assert job["env"]["BOT_DROP_PENDING_UPDATES"] == "false"
    assert any(step.get("run") == "bash scripts/gha_run.sh" for step in job["steps"])
    assert (ROOT / "scripts" / "gha_run.sh").exists()


def test_worker_config_is_consistent() -> None:
    """Cloudflare Worker: Durable Object bog'lanishi va boshqaruv yo'li."""
    toml = (ROOT / "cloudflare" / "wrangler.toml").read_text(encoding="utf-8")
    worker = (ROOT / "cloudflare" / "worker.js").read_text(encoding="utf-8")
    assert 'class_name = "OriginStore"' in toml
    assert "new_sqlite_classes" in toml
    assert "export class OriginStore" in worker
    assert "/__hc/origin" in worker
    assert "/__hc/origin" in (ROOT / "scripts" / "gha_run.sh").read_text(encoding="utf-8")


def test_dockerfile_uses_healthz_and_non_root() -> None:
    """``Dockerfile``: `/healthz` healthcheck, root bo'lmagan foydalanuvchi."""
    content = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "/healthz" in content
    assert "USER hugecloud" in content
    assert "python main.py" in content
    assert "requirements.txt" in content


def test_systemd_unit_matches_readme() -> None:
    """systemd unit: ``hugecloud`` nomi, ``main.py --mode all``, ``Restart=always``."""
    unit = (ROOT / "deploy" / "hugecloud.service").read_text(encoding="utf-8")
    assert "Description=hugecloud" in unit
    assert "main.py --mode all" in unit
    assert "Restart=always" in unit
    assert "[Install]" in unit and "WantedBy=multi-user.target" in unit


def test_procfile_matches_render_start_command() -> None:
    """``Procfile`` va ``render.yaml`` bir xil start buyrug'ini ishlatadi."""
    procfile = (ROOT / "Procfile").read_text(encoding="utf-8")
    assert "python main.py --mode all" in procfile


def test_env_example_covers_required_variables() -> None:
    """``.env.example`` da README'da va'da qilingan barcha kalitlar bor."""
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    required = (
        "BOT_TOKEN", "BOT_USERNAME", "ADMIN_ID", "ADMIN_PASSWORD",
        "ARCHIVE_CHANNEL_ID", "ARCHIVE_ENABLED", "ARCHIVE_BLOCKING",
        "AUTH_REQUIRED", "AUTH_ALLOW_REGISTER", "AUTH_MAX_ATTEMPTS",
        "STORAGE_QUOTA", "QUOTA_UNLIMITED_LABEL", "STORAGE_DEDUPE", "PAGE_SIZE",
        "DATABASE_URL", "DB_AUTO_MIGRATE", "REDIS_URL", "JWT_SECRET",
        "WEBHOOK_ENABLED", "WEBHOOK_URL", "WEBHOOK_PATH", "WEBHOOK_SECRET",
        "RUN_MODE", "APP_LANGUAGE", "SERVE_WEB", "WEB_MOUNT_PATH", "PORT",
    )
    missing = [key for key in required if f"{key}=" not in example]
    assert not missing, f".env.example'da yetishmayapti: {missing}"


def test_dockerignore_keeps_secrets_out() -> None:
    """``.dockerignore``: ``.env`` obrazga kirmasligi shart."""
    content = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    lines = [line.strip() for line in content.splitlines() if line.strip() and not line.startswith("#")]
    assert ".env" in lines
    assert ".git" in lines
    # Testlar CI uchun obrazda qolishi mumkin.
    assert "tests/" not in lines


def test_requirements_are_installable_ranges() -> None:
    """``requirements.txt``: qat'iy pin emas, "minimum" diapazonlari."""
    content = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for package in ("aiogram", "fastapi", "SQLAlchemy", "uvicorn", "PyJWT"):
        line = next(item for item in content.splitlines() if item.startswith(package))
        assert ">=" in line, f"{package} versiyasi diapazon bilan berilishi kerak"
    # email-validator 2.4.0 PyPI'da yo'q edi — shu xato takrorlanmasin.
    email_line = next(item for item in content.splitlines() if item.startswith("email-validator"))
    assert "==2.4.0" not in email_line


# --------------------------------------------------------------------------- #
# pyproject.toml (ruff) — hujjatlash va izchillik
# --------------------------------------------------------------------------- #
def test_pyproject_exists_and_is_valid_toml() -> None:
    """``pyproject.toml`` mavjud va TOML sifatida o'qiladi."""
    import tomllib

    path = ROOT / "pyproject.toml"
    assert path.exists(), "pyproject.toml yo'q — lint sozlamalari hujjatlanmagan"
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    assert config["tool"]["ruff"]["line-length"] >= 100
    assert config["tool"]["ruff"]["lint"]["ignore"], "istisnolar ro'yxati bo'sh"


def test_every_ruff_ignore_is_documented() -> None:
    """Har bir o'chirilgan qoida **izohlangan** bo'lishi kerak.

    Qoida: fayldagi izoh satrlarida tilga olingan qoidalar to'plami ``ignore``
    ro'yxati bilan ustma-ush tushishi kerak. Shunda keyinchalik biror qoida
    izohsiz qo'shilsa (yoki izoh eskirsa) test darhol signal beradi.
    """
    import re
    import tomllib

    path = ROOT / "pyproject.toml"
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    ignored = set(config["tool"]["ruff"]["lint"]["ignore"])
    assert ignored, "istisnolar ro'yxati bo'sh"

    # Faqat `ignore = [...]` blokidagi izohlarni hisobga olamiz: fayl boshidagi
    # umumiy izoh ("F821/F841 kabi qoidalar ushlanadi") istisno ro'yxati emas.
    source = path.read_text(encoding="utf-8")
    block_match = re.search(r"ignore\s*=\s*\[(?P<body>.*?)\]", source, re.DOTALL)
    assert block_match, "`ignore = [...]` bloki topilmadi"
    block = block_match.group("body")

    commented: set[str] = set()
    for line in block.splitlines():
        if "#" not in line:
            continue
        comment = line[line.index("#") :]
        commented.update(re.findall(r"\b[A-Z]{1,4}\d{3,4}\b", comment))

    undocumented = sorted(ignored - commented)
    assert not undocumented, f"izohsiz o'chirilgan qoidalar: {undocumented}"

    stale = sorted(commented - ignored)
    assert not stale, f"izohda bor, lekin `ignore` da yo'q (eskirgan hujjat): {stale}"
