"""🎨 Mini App frontend (``web/index.html``) testlari.

Bu modul UI/UX qayta tuzilishining **frontend** qatlamini tekshiradi. Testlar
uch guruhga bo'lingan:

1. **Statik dizayn-kontrakt** — dizayn token'lari (yorug' ``#FFFFFF``/``#0066FF``,
   qorong'i ``#0F172A``/``#1E293B``), mavzu almashtirgichi, suzuvchi amallar
   paneli (pastki o'ng burchak, pill uslub, ko'tarilgan soya), pleer
   (bufer zonasi, maxsus boshqaruv, Media Session, klaviatura).
2. **Butunlik** — JS'dagi har bir ``$("id")`` markup'da mavjud; JS'dagi har bir
   API manzili FastAPI router'ida ro'yxatdan o'tgan; ``<script>`` bloklari
   sintaktik jihatdan yaroqli (Node bo'lsa).
3. **Xatti-harakat** — sof funksiyalar (``humanSize``, ``humanTime``,
   ``escapeHtml``, ``extractShareToken``) Node'da haqiqatan ishga tushiriladi va
   botdagi ``extract_share_token`` bilan **bir xil** natija berishi tekshiriladi.

Nega shunday: frontend'da build bosqichi yo'q (bitta HTML fayl), shuning uchun
"ishlamaydigan tugma" xatosini faqat statik + izolyatsiyalangan dinamik
testlar ushlay oladi.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bot.handlers.miniapp import extract_share_token

ROOT = Path(__file__).resolve().parents[1]
WEB_FILE = ROOT / "web" / "index.html"

pytestmark = pytest.mark.asyncio

#: Node bo'lmasa (masalan, minimal konteyner) dinamik testlar o'tkazib yuboriladi.
NODE = shutil.which("node")


def _html() -> str:
    return WEB_FILE.read_text(encoding="utf-8")


def _scripts() -> list[str]:
    """``<script src=...>`` bo'lmagan ichki skript bloklari."""
    return re.findall(r"<script>(.*?)</script>", _html(), re.DOTALL)


def _main_script() -> str:
    scripts = _scripts()
    assert len(scripts) >= 2, "kritik mavzu skripti + asosiy ilova skripti kutiladi"
    return max(scripts, key=len)



#: Yuqori darajali (2 bo'shliq) JS deklaratsiyalari — funksiya bloki chegarasi.
_JS_BOUNDARY = re.compile(
    r"^  (?:async function|function|var|const|let) ",
    re.MULTILINE,
)


def _code_block(anchor: str) -> str:
    """``anchor``dan keyingi funksiyalar/blok chegarasigacha bo'lgan JS parchasi.

    Barcha komponent funksiyalari asosiy ``<script>`` ichida 2 bo'shliq
    chekinish bilan e'lon qilinadi; shuning uchun keyingi shunday deklaratsiya
    blok oxiri hisoblanadi.
    """
    src = _main_script()
    start = src.index(anchor)
    match = _JS_BOUNDARY.search(src, start + len(anchor))
    return src[start : match.start() if match else len(src)]

def _ids_in_markup() -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', _html()))


def _css() -> str:
    match = re.search(r"<style>(.*?)</style>", _html(), re.DOTALL)
    assert match, "ichki <style> bloki topilmadi"
    return match.group(1)


# --------------------------------------------------------------------------- #
# 1. Dizayn kontrakti
# --------------------------------------------------------------------------- #
def test_web_file_exists_and_is_self_contained() -> None:
    """Bitta fayl: build yo'q, tashqi CSS/JS yo'q (faqat Telegram SDK)."""
    assert WEB_FILE.exists()
    html = _html()
    assert html.lstrip().lower().startswith("<!doctype html>")
    # Tashqi resurslar: faqat Telegram WebApp SDK ruxsat etilgan
    externals = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    assert externals == ["https://telegram.org/js/telegram-web-app.js"]


def test_design_tokens_light_theme() -> None:
    """Yorug' mavzu: toza oq fon + korporativ ko'k ``#0066FF``."""
    css = _css()
    light = re.search(r":root\[data-theme=\"light\"\]\s*\{(.*?)\}", css, re.DOTALL)
    assert light, "yorug' mavzu token bloki yo'q"
    block = light.group(1)
    assert "--bg: #FFFFFF" in block
    assert "--brand: #0066FF" in css


def test_design_tokens_dark_theme() -> None:
    """Qorong'i mavzu: ``#0F172A`` fon, ``#1E293B`` sirtlar."""
    css = _css()
    dark = re.search(r":root\[data-theme=\"dark\"\]\s*\{(.*?)\}", css, re.DOTALL)
    assert dark, "qorong'i mavzu token bloki yo'q"
    block = dark.group(1)
    assert "--bg: #0F172A" in block
    assert "--surface: #1E293B" in block


def test_no_legacy_heavy_gradients() -> None:
    """SaaS estetika: dekorativ ko'p rangli gradientlar yo'q.

    Faqat ikkita texnik istisno: skeleton "shimmer" effekti va select
    o'q belgisi (ikkalasi ham bir rangli/neytral).
    """
    css = _css()
    offenders: list[str] = []
    for match in re.finditer(r"background[^;]*linear-gradient\(([^;]*)\)", css):
        colors = re.findall(r"#[0-9a-fA-F]{3,8}", match.group(1))
        if len(set(colors)) >= 3:
            offenders.append(match.group(0)[:80])
    assert not offenders, f"og'ir gradientlar: {offenders}"


def test_meta_theme_color_matches_palette() -> None:
    """``meta[name=theme-color]`` palitra bilan bir xil (brauzer chrome rangi)."""
    html = _html()
    assert '<meta name="theme-color" content="#0F172A"' in html


def test_theme_switcher_wiring() -> None:
    """Mavzu almashtirgichi: tugma, switch, localStorage, Telegram, tizim."""
    html = _html()
    script = _main_script()
    assert 'id="btnTheme"' in html and 'id="themeSwitch"' in html
    # localStorage'da saqlash + Telegram colorScheme + tizim preferensi
    assert "hugecloud:theme" in script or "STORE.theme" in script
    assert "colorScheme" in script
    assert "prefers-color-scheme" in html
    # Telegram oynasi ranglari sinxronlanadi
    assert "setHeaderColor" in script and "setBackgroundColor" in script
    # Server sozlamalariga ham yoziladi (qurilmalararo izchillik)
    assert '"theme"' in script or "theme:" in script


def test_theme_is_applied_before_first_paint() -> None:
    """FOUC'ning oldini olish: mavzu skripti ``<body>`` dan **oldin**."""
    html = _html()
    inline_theme = html.index('data-theme')
    body_index = html.index("<body>")
    assert inline_theme < body_index
    assert "setAttribute(\"data-theme\"" in _scripts()[0]


def test_floating_toolbar_is_fixed_bottom_right() -> None:
    """Suzuvchi panel: qo'zg'almas, pastki o'ng burchak."""
    css = _css()
    dock = re.search(r"\.fab-dock\s*\{(.*?)\}", css, re.DOTALL)
    assert dock, "``.fab-dock`` uslubi yo'q"
    block = dock.group(1)
    assert "position: fixed" in block
    assert "right:" in block and "bottom:" in block
    assert "z-index" in block


def test_floating_toolbar_has_upload_and_import_pills() -> None:
    """Ikki aniq amal: ⬆️ Yuklash va 📥 Import (pill shakl, ko'tarilgan soya)."""
    html = _html()
    css = _css()
    assert 'id="fabUpload"' in html and 'id="fabImport"' in html
    assert ">Yuklash<" in html.replace("\n", "").replace("  ", "")
    assert "Import" in html

    action = re.search(r"\.fab-action\s*\{(.*?)\}", css, re.DOTALL)
    assert action, "``.fab-action`` uslubi yo'q"
    block = action.group(1)
    # Pill uslubi va ko'tarilgan soya
    assert "--r-pill" in block or "999px" in block
    assert "box-shadow" in block
    assert "--sh-3" in block or "0 10px" in block


def test_fab_has_speed_dial_and_backdrop() -> None:
    """FAB bosilganda pill'lar chiqadi, fon bosilsa yopiladi (accessibility bilan)."""
    html = _html()
    script = _main_script()
    assert 'id="fabMain"' in html and 'id="fabBackdrop"' in html
    assert "is-open" in script
    assert 'aria-expanded' in html
    # Klaviatura: Escape bilan yopiladi
    assert "closeFab" in script


def test_photos_have_a_separate_group() -> None:
    """Rasmlar — ``media`` dan alohida bo'lim (server ``category=photo``)."""
    script = _main_script()
    groups = _code_block("var GROUPS = [")
    # Emoji emas, monoxrom SVG identifikatori (``icon``).
    assert '"photo"' in groups and 'icon: "image"' in groups
    # ``photo`` guruhi ``group=`` emas, ``category=photo`` so'rovini yuboradi.
    assert 'state.group === "photo"' in script
    assert 'query.push("category=photo")' in script


def test_bottom_nav_spreads_all_items_evenly() -> None:
    """Pastki navigatsiya butun kenglikka teng taqsimlanadi (FAB uchun bo'shliq yo'q)."""
    html = _html()
    assert 'class="nav-spacer"' not in html, "FAB bo'shligi tugmalarni chapga siqib qo'yardi"
    rule = re.search(r"\.bottom-nav button\s*\{(.*?)\}", _css(), re.DOTALL)
    assert rule, "``.bottom-nav button`` uslubi yo'q"
    assert "flex: 1 1 0" in rule.group(1)


def test_wide_screens_use_a_mobile_preview_shell() -> None:
    """Keng ekranda ilova telefon kengligida markazda ko'rsatiladi."""
    css = _css()
    assert "--shell-w:" in css
    shell = re.search(r"@media \(min-width: 700px\)\s*\{(.*?)\n\}", css, re.DOTALL)
    assert shell, "mobil preview media query topilmadi"
    block = shell.group(1)
    assert "max-width: var(--shell-w)" in block
    assert ".player-backdrop," not in block, "pleer landscape'da butun ekranni egallashi kerak"
    # Suzuvchi navbar o'z-o'zidan telefon kengligi bilan cheklanadi va markazlashadi
    # (shuning uchun media query ichida takror shart emas).
    nav = re.search(r"\.bottom-nav\s*\{(.*?)\}", css, re.DOTALL)
    assert nav, "``.bottom-nav`` uslubi yo'q"
    nav_block = nav.group(1)
    assert "var(--shell-w)" in nav_block, "navbar telefon kengligi bilan cheklanmagan"
    assert "translateX(-50%)" in nav_block, "navbar markazlashmagan"


def test_fullscreen_uses_telegram_and_native_fallbacks() -> None:
    """To'liq ekran: Telegram API + DOM + iOS ``webkitEnterFullscreen`` zanjiri."""
    script = _main_script()
    assert "tg.requestFullscreen" in script
    assert "tg.exitFullscreen" in script
    assert "webkitEnterFullscreen" in script
    assert "function enterFullscreen" in script and "function exitFullscreen" in script
    assert "exitFullscreen()" in _code_block("function closePlayer")


def test_player_has_custom_controls() -> None:
    """Pleer: brauzer ``controls`` ishlatilmaydi — barchasi maxsus UI."""
    html = _html()
    assert 'id="mediaEl"' in html
    assert "<video" in html and "controlslist" in html
    assert not re.search(r"<video[^>]*\scontrols[\s>]", html), "standart controls qolmasligi kerak"
    for control_id in [
        "btnPlay",
        "progress",
        "progressPlayed",
        "progressBuffered",
        "progressThumb",
        "timeCurrent",
        "timeTotal",
        "volumeRange",
        "btnMute",
        "btnSpeed",
        "btnFwd10",
        "btnBack10",
        "btnFullscreen",
        "btnDownload",
        "buffering",
    ]:
        assert f'id="{control_id}"' in html, control_id


def test_player_buffering_and_performance_hooks() -> None:
    """Silliq buferlash: spinner, bufer zonasi, ticket'ni oldindan yangilash."""
    script = _main_script()
    # Bufer holatlari kuzatiladi
    for event in ["waiting", "seeking", "seeked", "progress", "stalled", "canplay"]:
        assert f'addEventListener("{event}"' in script, event
    assert "media.buffered" in script
    assert "is-buffering" in script
    # Ticket muddati tugashidan oldin fon yangilanishi + xatodan tiklanish
    assert "ensureStream" in script
    assert "recoverPlayback" in script
    assert "maxRetry" in script
    # Ijro samaradorligi: metadata oldindan yuklanadi
    assert 'preload="metadata"' in _html()


def test_player_media_session_and_shortcuts() -> None:
    """Qulf ekrani boshqaruvi (Media Session) va klaviatura qisqartmalari."""
    script = _main_script()
    assert "mediaSession" in script and "MediaMetadata" in script
    assert "setActionHandler" in script
    # Qisqartmalar: space/k, j/l, f, p, m, strelkalar, 0-9
    for key in ['" "', '"k"', '"j"', '"l"', '"f"', '"m"', '"ArrowRight"', '"ArrowLeft"']:
        assert key in script, key


def test_legacy_features_are_preserved() -> None:
    """Eski bot mexanikasi yo'qolmagan: guruhlar, saralash, pin, arxiv belgisi."""
    html = _html()
    # Guruh identifikatsiyasi: emoji o'rniga monoxrom SVG ikonka nomlari.
    for icon in ['icon: "film"', 'icon: "music"', 'icon: "file"', 'icon: "archive"', 'icon: "robot"', 'icon: "pin"']:
        assert icon in html, icon
    for sort_value in ['value="new"', 'value="old"', 'value="big"', 'value="small"', 'value="az"', 'value="za"']:
        assert sort_value in html, sort_value
    # Pin va arxiv belgilari
    assert "/pin" in html
    assert "archived" in html
    # Uzbek interfeys
    assert '<html lang="uz"' in html


def test_uzbek_ui_language() -> None:
    """Yangi UI o'zbekcha (loyiha talabi)."""
    html = _html()
    for word in ["Yuklash", "Import", "Fayllar", "Statistika", "Hisob", "Mavzu"]:
        assert word in html, word


# --------------------------------------------------------------------------- #
# 2. Butunlik
# --------------------------------------------------------------------------- #
def test_every_element_id_referenced_by_js_exists() -> None:
    """JS'dagi har bir ``$("id")`` markup'da bor (aks holda ``null`` xatosi)."""
    script = _main_script()
    referenced = set(re.findall(r'\$\("([A-Za-z0-9_]+)"\)', script))
    markup = _ids_in_markup()
    missing = sorted(referenced - markup)
    assert not missing, f"markup'da yo'q ID'lar: {missing}"


def test_js_has_no_undefined_element_lookups() -> None:
    """``getElementById`` to'g'ridan-to'g'ri ham xavfsiz chaqiriladi."""
    script = _main_script()
    direct = set(re.findall(r'getElementById\("([A-Za-z0-9_]+)"\)', script))
    markup = _ids_in_markup()
    assert not sorted(direct - markup)


def test_script_blocks_are_syntactically_valid() -> None:
    """Har bir ``<script>`` bloki sintaktik jihatdan yaroqli (Node ``--check``)."""
    if NODE is None:
        pytest.skip("Node.js o'rnatilmagan")
    for index, script in enumerate(_scripts()):
        tmp = Path(f"/tmp/hugecloud_web_script_{index}.js")
        tmp.write_text(script, encoding="utf-8")
        result = subprocess.run(
            [NODE, "--check", str(tmp)], capture_output=True, text=True, timeout=120, check=False
        )
        assert result.returncode == 0, f"skript #{index}: {result.stderr[:600]}"


def test_api_paths_used_by_frontend_exist(client: Any) -> None:
    """Frontend chaqiradigan **har bir** manzil FastAPI'da ro'yxatdan o'tgan."""
    script = _main_script()
    # FastAPI 0.141+ ``include_router`` natijalarini ``_IncludedRouter`` ichida
    # saqlaydi, shuning uchun marshrutlarni OpenAPI spetsifikatsiyasidan olamiz:
    # u barcha prefix'larni hisobga olgan holda to'liq ro'yxat beradi.
    registered: set[tuple[str, str]] = set()
    for path, operations in client.app.openapi()["paths"].items():
        for method in operations:
            registered.add((method.upper(), path))

    def to_pattern(route_path: str) -> re.Pattern[str]:
        """Yo'l shablonini regex'ga aylantirish: ``{param}`` → ``[^/]+``."""
        pieces: list[str] = []
        for part in re.split(r"(\{[^}]+\})", route_path):
            if part.startswith("{") and part.endswith("}"):
                pieces.append("[^/]+")
            else:
                pieces.append(re.escape(part))
        return re.compile("^" + "".join(pieces) + "$")

    def exists(method: str, path: str) -> bool:
        return any(
            method == registered_method and to_pattern(registered_path).match(path)
            for registered_method, registered_path in registered
        )

    # Frontend ishlatadigan manzillar (namuna ID/token bilan)
    expected: list[tuple[str, str]] = [
        ("POST", "/api/v1/auth/telegram"),
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/register"),
        ("POST", "/api/v1/auth/logout"),
        ("GET", "/api/v1/auth/me"),
        ("PATCH", "/api/v1/auth/me"),
        ("GET", "/api/v1/auth/me/stats"),
        ("GET", "/api/v1/files"),
        ("GET", "/api/v1/files/some-id/stream"),
        ("POST", "/api/v1/files/some-id/pin"),
        ("POST", "/api/v1/files/some-id/share"),
        ("PATCH", "/api/v1/files/some-id"),
        ("DELETE", "/api/v1/files/some-id"),
        # Phase D: nom o'zgartirish / ko'chirish / ommaviy o'chirish,
        # hamda papkalar ro'yxati (ko'chirish oynasi uchun).
        ("POST", "/api/v1/files/bulk-delete"),
        # 🗑 Savat (6-bosqich): ro'yxat + tiklash + butunlay o'chirish
        ("GET", "/api/v1/files/trash"),
        ("POST", "/api/v1/files/some-id/restore"),
        ("DELETE", "/api/v1/files/some-id/purge"),
        # Papkalar interfeysi (5-bosqich): chips + breadcrumb + ko'chirish
        ("GET", "/api/v1/folders"),
        ("POST", "/api/v1/folders"),
        ("DELETE", "/api/v1/folders/some-id"),
        ("GET", "/api/v1/shares"),
        ("DELETE", "/api/v1/shares/some-token"),
        ("GET", "/api/v1/shares/public/some-token"),
        ("GET", "/api/v1/shares/public/some-token/check"),
        ("POST", "/api/v1/shares/public/some-token/unlock"),
    ]
    missing = [f"{method} {path}" for method, path in expected if not exists(method, path)]
    assert not missing, f"router'da yo'q: {missing}"

    # JS'da chaqirilayotgan barcha manzil prefikslari ro'yxatdan o'tgan
    # yo'llarning boshlanishi bo'lishi kerak (dinamik qismlar: ``+ file.id``).
    called = set(re.findall(r'api\("(/[a-z0-9/_\-]+)', script))
    known = {path.replace("/api/v1", "") for _, path in expected}
    unknown = sorted(
        prefix for prefix in called if not any(item.startswith(prefix) for item in known)
    )
    assert not unknown, unknown


def test_frontend_uses_relative_api_paths() -> None:
    """Mini App proxy orqali ochiladi: manzillar **nisbiy** bo'lishi shart."""
    script = _main_script()
    assert 'apiPrefix: "/api/v1"' in script
    assert not re.search(r'fetch\(\s*"https?://', script), "mutlaq URL ishlatilmasin"


def test_upload_uses_http_relay_with_chat_fallback() -> None:
    """Yuklash: ≤50 MB — HTTP relay (progress bilan), kattasi — bot chatiga.

    Mini App Telegram'ga fayl baytlarini to'g'ridan-to'g'ri yubora olmaydi
    (``sendData`` faqat matn), shuning uchun baytlar ``/files/upload`` orqali
    serverga boradi, server ularni ``sendDocument`` bilan Telegram'ga uzatadi.
    ``sendDocument`` chegarasidan (50 MB) katta fayl eski chat yo'liga o'tadi.
    """
    script = _main_script()
    # HTTP relay: multipart + yuklash progressi
    assert "XMLHttpRequest" in script
    assert "new FormData()" in script
    assert "/files/upload" in script
    assert "upload.onprogress" in script
    # Katta fayl uchun bot chatiga yo'naltirish saqlanadi
    assert "openInTelegram" in script


def test_import_sends_token_payload_to_bot() -> None:
    """Import: ``hugecloud.import`` + token (bot ImportState dialogiga o'tadi)."""
    script = _main_script()
    assert "hugecloud.import" in script
    assert "type: \"hugecloud.import\", token: token" in script


# --------------------------------------------------------------------------- #
# 3. Dinamik (Node) — sof funksiyalar
# --------------------------------------------------------------------------- #
_PURE_FUNCTIONS = (
    "humanSize",
    "humanTime",
    "humanDate",
    "escapeHtml",
    "clamp",
    "toMediaUrl",
    "extractShareToken",
)


def _extract_pure_functions() -> str:
    """IIFE ichidagi sof funksiyalarni ajratib olish (DOM'ga bog'liq emas)."""
    script = _main_script()
    chunks: list[str] = []
    for name in _PURE_FUNCTIONS:
        start = script.index("function " + name + "(")
        # Funksiya tanasi: birinchi "{" dan mos yopilishgacha
        brace_open = script.index("{", start)
        depth = 0
        for index in range(brace_open, len(script)):
            if script[index] == "{":
                depth += 1
            elif script[index] == "}":
                depth -= 1
                if depth == 0:
                    chunks.append(script[start : index + 1])
                    break
    assert len(chunks) == len(_PURE_FUNCTIONS), "barcha funksiyalar ajratilmadi"
    return "\n\n".join(chunks)


def _run_node(code: str) -> Any:
    """Node'da kodni ishga tushirish va ``console.log`` JSON natijasini olish."""
    if NODE is None:
        pytest.skip("Node.js o'rnatilmagan")
    harness = _extract_pure_functions() + "\n\n" + code
    tmp = Path("/tmp/hugecloud_web_probe.js")
    tmp.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(tmp)], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, f"Node xatosi: {result.stderr[:800]}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_human_size_formatting() -> None:
    """Hajm formati o'zbek UI uchun ixcham (B/KB/MB/GB)."""
    output = _run_node(
        "console.log(JSON.stringify([humanSize(0), humanSize(512), humanSize(2048),"
        " humanSize(5 * 1024 * 1024), humanSize(3 * 1024 * 1024 * 1024)]))"
    )
    assert output == ["0 B", "512 B", "2.0 KB", "5.0 MB", "3.0 GB"]


def test_human_time_formatting() -> None:
    """Pleer vaqti: ``m:ss`` va ``h:mm:ss``."""
    output = _run_node(
        "console.log(JSON.stringify([humanTime(0), humanTime(9), humanTime(75),"
        " humanTime(3725), humanTime(-4), humanTime(null)]))"
    )
    assert output == ["0:00", "0:09", "1:15", "1:02:05", "0:00", "0:00"]


def test_escape_html_prevents_injection() -> None:
    """Fayl nomlari HTML'ga xavfsiz chiqadi (XSS'ning oldini oladi)."""
    payload = "<img src=x onerror=alert(1)>\"'&"
    code = (
        "var raw = " + json.dumps(payload) + ";\n"
        "var escaped = escapeHtml(raw);\n"
        "console.log(JSON.stringify({ raw: raw, escaped: escaped }));"
    )
    output = _run_node(code)
    assert output["raw"] == payload
    escaped = output["escaped"]
    assert "<" not in escaped and ">" not in escaped
    assert "&lt;img" in escaped and "&quot;" in escaped and "&#39;" in escaped and "&amp;" in escaped


def test_extract_share_token_matches_bot_implementation() -> None:
    """Frontend va bot **bir xil** token ajratadi (uzluksiz import uchun)."""
    cases = [
        "AbC123_-xY",
        "t.me/Huge_cloud_bot?start=AbC123_-xY",
        "https://t.me/Huge_cloud_bot?start=AbC123_-xY",
        "https://cloud.example.com/api/v1/shares/Tok3n_abc",
        "havola: Tok3n_abc",
        "  Tok3n_abc  ",
        "",
        "salom dunyo",
        "ab",
        "https://t.me/Huge_cloud_bot?start=",
    ]
    output = _run_node(
        "var cases = "
        + json.dumps(cases)
        + ";\nconsole.log(JSON.stringify(cases.map(function (value) { return extractShareToken(value); })))"
    )
    for raw, frontend in zip(cases, output, strict=True):
        assert frontend == extract_share_token(raw), f"mos kelmadi: {raw!r}"


def test_human_date_handles_missing_values() -> None:
    """Sana bo'lmasa — ``—`` (bo'sh kartalar chiqmasin)."""
    output = _run_node("console.log(JSON.stringify([humanDate(null), humanDate('')]))")
    assert output == ["—", "—"]


# --------------------------------------------------------------------------- #
# 4. Integratsiya: fayl haqiqatan SPA sifatida beriladi
# --------------------------------------------------------------------------- #
def test_root_serves_the_new_spa(session: Any, client: Any) -> None:
    """``GET /`` (Accept: text/html) yangi dizayn tizimini qaytaradi."""
    response = client.get("/", headers={"Accept": "text/html"})
    assert response.status_code == 200
    body = response.text
    assert "#0066FF" in body and "#0F172A" in body and "#1E293B" in body
    assert 'id="fabDock"' in body and 'id="player"' in body
    assert 'data-theme' in body


def test_index_html_route_serves_same_app(session: Any, client: Any) -> None:
    """``/index.html`` ham bir xil ilovani beradi (zaxira manzil)."""
    response = client.get("/index.html")
    assert response.status_code == 200
    assert 'id="fabUpload"' in response.text


#: Frontend o'zi yasaydigan kodlar (backend ularni qaytarmaydi).
FRONTEND_ONLY_CODES = {
    "network",        # fetch tarmoq xatosi
    "not_streamable", # fayl turi onlayn ijroga yaroqsiz
}

#: Auth oqimi kodlari: ``services/auth.py`` ular uchun aniq o'zbekcha matn
#: qaytaradi, shuning uchun frontend ularni qayta yozmaydi.
AUTH_CODES = {"no_account", "unknown_login", "wrong_password", "locked", "blocked"}


def _backend_error_codes() -> set[str]:
    """Backend qaytaradigan barcha xato kodlari — manba fayllardan yig'iladi."""
    codes: set[str] = set()

    # DomainError oilasi (services/exceptions.py)
    exceptions = (ROOT / "services" / "exceptions.py").read_text(encoding="utf-8")
    codes.update(re.findall(r'^\s+code = "([a-z_]+)"', exceptions, re.MULTILINE))

    # HTTP qatlami: api/ ostidagi barcha fayllar (routes + app + middleware)
    for path in (ROOT / "api").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        codes.update(re.findall(r'"code":\s*"([a-z_]+)"', text))
        codes.update(re.findall(r'code="([a-z_]+)"', text))

    # Auth natijalari (AuthOutcome StrEnum)
    auth = (ROOT / "services" / "auth.py").read_text(encoding="utf-8")
    start = auth.index("class AuthOutcome")
    codes.update(re.findall(r'= "([a-z_]+)"', auth[start : auth.index("@dataclass", start)]))

    codes.discard("ok")
    return codes


def _frontend_error_map() -> tuple[set[str], set[str]]:
    """``ERROR_MESSAGES`` va ``SERVER_LOCALIZED_CODES`` to'plamlari."""
    mapped = set(re.findall(r'^    ([a-z_]+): "', _code_block("var ERROR_MESSAGES"), re.MULTILINE))
    localized = set(re.findall(r"([a-z_]+): 1", _code_block("var SERVER_LOCALIZED_CODES")))
    return mapped, localized


def test_frontend_covers_every_backend_error_code() -> None:
    """Har bir backend xato kodi frontend'da tarjima yoki server matniga ega."""
    mapped, localized = _frontend_error_map()
    backend = _backend_error_codes()
    assert backend, "backend kodlari topilmadi — regex buzilgan"

    missing = sorted(backend - mapped - localized)
    assert not missing, f"frontend bu kodlarni qoplamaydi: {missing}"

    invented = sorted(mapped - backend - FRONTEND_ONLY_CODES)
    assert not invented, f"frontend backend'da mavjud bo'lmagan kodlarni o'ylab topgan: {invented}"


def test_auth_codes_let_server_message_win() -> None:
    """Auth xatolari serverda aniq o'zbekcha — frontend ularni umumiy matn bilan almashtirmaydi."""
    _, localized = _frontend_error_map()
    assert AUTH_CODES <= localized, f"server matniga ustunlik berilmagan: {sorted(AUTH_CODES - localized)}"

    fn = _code_block("function localizedError")
    assert fn.index("SERVER_LOCALIZED_CODES[code] && fallback") < fn.index("ERROR_MESSAGES[code]"), (
        "server matni tekshiruvi lug'atdan oldin kelishi kerak"
    )
    # HTTP holat bo'yicha zaxira matnlar ham bor
    for status in ("401", "403", "404", "410", "429", "500"):
        assert f"status === {status}" in fn or f"status >= {status}" in fn, f"{status} uchun zaxira yo'q"


def test_prefs_are_patched_as_flat_fields_not_nested_settings() -> None:
    """``PATCH /auth/me`` tekis maydon kutadi: ``{"theme": ...}``, ``{"settings": {...}}`` emas."""
    code = _code_block("function syncPrefs")
    assert 'method: "PATCH"' in code
    assert "body: JSON.stringify(patch)" in code
    assert '"settings"' not in code and "settings:" not in code, "ichma-ich settings obyekti yuborilmasin"

    script = _main_script()
    assert "syncPrefs({ theme: mode })" in script
    assert 'syncPrefs({ view_mode:' in script
    assert "syncPrefs({ sort_by: pair[0], sort_desc: Boolean(pair[1]) })" in script


def test_layout_and_sort_changes_are_synced_to_server() -> None:
    """Ro'yxat/grid va saralash tanlovi server sozlamalariga yoziladi."""
    layout_fn = _code_block("function setLayout")
    assert "if (!options || options.sync !== false) syncLayoutToServer(layout);" in layout_fn

    sync_fn = _code_block("var syncLayoutToServer")
    assert "debounce(" in sync_fn, "tez-tez bosilganda serverni ortiqcha so'rov bilan bosmaslik uchun"
    assert 'syncPrefs({ view_mode: layout === "list" ? "list" : "grid" })' in sync_fn

    script = _main_script()
    assert "syncPrefs({ sort_by: pair[0], sort_desc: Boolean(pair[1]) })" in script
    assert script.count("syncPrefs(") >= 4, "mavzu, ko'rinish, saralash va boot sinxronizatsiyasi kutiladi"
    # boot paytida aks-sado (echo) yuz bermasligi uchun sinxronizatsiya o'chiriladi
    assert 'setLayout(state.layout, { sync: false })' in _code_block("async function boot")


def test_player_probes_ticket_before_playing() -> None:
    """Ijro boshlanishidan oldin ``HEAD`` pre-flight: 4x = fatal, 5x/network = qayta urinish."""
    probe = _code_block("function probeStream")
    assert 'method: "HEAD"' in probe
    assert "if (response.ok) return true" in probe
    assert "error.fatal = true" in probe and "throw error" in probe
    assert "if (response.status >= 400 && response.status < 500)" in probe
    assert probe.count("return false") == 2, "5xx va offline holatlari qayta urinishga qoldiriladi"

    for name in ("async function playFile", "function playPublic", "async function recoverPlayback"):
        block = _code_block(name)
        assert "probeStream(" in block, f"{name} pre-flight ishlatishi kerak"
        # Manba endi ``applyMediaSource`` orqali o'rnatiladi (``<source
        # type="...">`` bilan); tartib shartnomasi o'zgarmagan: avval probe.
        setter = "applyMediaSource(" if "applyMediaSource(" in block else "media.src"
        assert block.index("probeStream(") < block.index(setter), f"{name}: avval probe, keyin src"
        assert "media.src" in block or "applyMediaSource(" in block

    recovery = _code_block("async function recoverPlayback")
    assert "player.retry = player.maxRetry" in recovery, "fatal xato qayta urinish zanjirini uzishi kerak"


def test_retries_use_incremental_backoff() -> None:
    """Qayta urinishlar ortib boruvchi kechikish bilan (1.2s → 2.4s → 4.8s)."""
    recovery = _code_block("async function recoverPlayback")
    assert "player.retry += 1" in recovery
    assert "await sleep(Math.min(1200 * Math.pow(2, player.retry - 1), 8000))" in recovery
    assert "maxRetry: 3" in _code_block("var player = {")
    assert "function sleep(ms)" in _main_script()


def _css_without_token_blocks() -> str:
    """CSS'dan ``:root`` token bloklarini va izohlarni olib tashlaydi.

    Izohlar palitra hujjati ("#0F172A fon, #1E293B sirtlar") — ular uslub
    emas, shuning uchun tekshiruvdan chiqariladi.
    """
    css = re.sub(r"/\*.*?\*/", "", _css(), flags=re.DOTALL)
    kept: list[str] = []
    index = 0
    while True:
        # Token bloklari bir nechta atribut bilan ham yozilishi mumkin:
        # ``:root[data-design="v2"][data-theme="dark"]`` (v2 dizayn qatlami).
        match = re.search(r":root(?:\[[^\]]*\])*\s*\{", css[index:])
        if not match:
            kept.append(css[index:])
            break
        start = index + match.start()
        kept.append(css[index:start])
        depth = 0
        cursor = index + match.end() - 1
        while cursor < len(css):
            if css[cursor] == "{":
                depth += 1
            elif css[cursor] == "}":
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        index = cursor + 1
    return "".join(kept)


def test_palette_lives_only_in_tokens_and_theme_map() -> None:
    """Ranglar faqat token bloklarida va Telegram ``THEME_COLORS`` xaritasida."""
    leaked_css = sorted(set(re.findall(r"#[0-9A-Fa-f]{3,8}\b", _css_without_token_blocks())))
    assert not leaked_css, f"komponent CSS'ida qattiq ranglar: {leaked_css}"

    # JS'da izohlar (palitra hujjati) hisobga olinmaydi — faqat bajariladigan kod
    script = re.sub(r"/\*.*?\*/", "", _main_script(), flags=re.DOTALL)
    script = re.sub(r"(?m)^\s*//.*$", "", script)
    theme_map = _code_block("var THEME_COLORS")
    leaked_js = sorted(set(re.findall(r"#[0-9A-Fa-f]{3,8}\b", script.replace(theme_map, "", 1))))
    assert not leaked_js, f"JS'da qattiq ranglar (THEME_COLORS'dan tashqari): {leaked_js}"

    # <meta name="theme-color"> — paint'dan oldingi yagona ruxsat etilgan istisno
    head = _html()[: _html().index("<style>")]
    assert re.findall(r"#[0-9A-Fa-f]{3,8}\b", head) == ["#0F172A"]


def test_theme_is_applied_before_first_paint_and_before_any_request() -> None:
    """Mavzu birinchi paint'dan oldin qo'llanadi (FOUC yo'q) va tarmoqdan oldin."""
    early = _scripts()[0]
    assert 'localStorage.getItem("hugecloud:theme")' in early
    assert 'setAttribute("data-theme", mode)' in early
    assert "prefers-color-scheme" in early, "tizim tanlovi zaxira sifatida ishlatilishi kerak"

    apply_fn = _code_block("function applyTheme")
    assert 'setAttribute("data-theme", next)' in apply_fn
    assert "THEME_COLORS.header[next]" in apply_fn
    assert "THEME_COLORS.background[next]" in apply_fn
    assert 'aria-checked' in apply_fn, "almashtirgich ARIA holatini yangilashi kerak"

    boot = _code_block("async function boot")
    assert boot.index("applyTheme(resolveInitialTheme(), { sync: false })") < boot.index("await authenticateWithTelegram()")


def _i18n_dictionary() -> dict[str, dict[str, str]]:
    """Asosiy skriptdagi ``I18N`` lug'atini JSON sifatida o'qib oladi."""
    script = _main_script()
    start = script.index("var I18N = {")
    start = script.index("{", start)

    depth, index, in_string, escaped = 0, start, False, False
    while index < len(script):
        char = script[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                break
        index += 1

    body = script[start + 1 : index]
    # Kalitlar JS uslubida yozilgan (``ru:``/``en:``) — JSON uchun qo'shtirnoq qo'shamiz.
    body = re.sub(r'\n(\s*)(ru|en): \{', lambda m: f'\n{m.group(1)}"{m.group(2)}": {{', body)
    return json.loads("{" + body + "}")


def test_frontend_has_language_switcher() -> None:
    """Til almashtirgich: uz/ru/en, saqlanadi va ``<html lang>`` yangilanadi."""
    html = _html()
    assert 'id="btnLang"' in html, "sarlavhada til tugmasi yo'q"
    assert 'id="langBtnCode"' in html, "joriy til kodi ko'rsatilmaydi"
    assert 'class="icon-btn lang-btn"' in html
    for code in ['code: "uz"', 'code: "ru"', 'code: "en"']:
        assert code in html, code

    script = _main_script()
    assert 'lang: CONFIG.storePrefix + "lang"' in script, "til kaliti namespaced emas"
    assert 'lang: { current: currentLang' in script, "tashqi API'da til yo'q"

    apply_fn = _code_block("function applyLanguage")
    assert 'document.documentElement.setAttribute("lang", next)' in apply_fn
    assert "store(STORE.lang, next)" in apply_fn
    assert "i18nWalk(document.body" in apply_fn

    # Til birinchi so'rovdan oldin qo'llanadi (FOUC yo'q).
    boot = _code_block("async function boot")
    assert boot.index("applyLanguage(resolveInitialLang()") < boot.index("await authenticateWithTelegram()")
    assert "i18nWatch()" in boot, "yangi tugunlar kuzatilmaydi"


def test_i18n_dictionary_keys_exist_in_source() -> None:
    """Lug'at kaliti — o'zbekcha manba matnining **aynan** o'zi."""
    html = _html()
    dictionaries = _i18n_dictionary()
    assert set(dictionaries) == {"ru", "en"}
    ru, en = dictionaries["ru"], dictionaries["en"]
    assert len(ru) >= 300, f"lug'at juda kichik: {len(ru)}"
    assert set(ru) == set(en), "ru/en kalitlari mos emas"

    for key, value in ru.items():
        assert key == key.strip() and "  " not in key, f"kalitda bo'shliq: {key!r}"
        assert value.strip(), f"bo'sh tarjima: {key!r}"
        assert key in html, f"kalit manbada yo'q: {key!r}"
    for key, value in en.items():
        assert value.strip(), f"bo'sh tarjima (en): {key!r}"

    # Son bilan yasalgan matnlar uchun shablon qoidalari ham bo'lishi shart.
    rules = _code_block("var I18N_RE")
    assert "ta fayl$" in rules and "kun oldin$" in rules


def test_theme_toggle_icons_are_centered() -> None:
    """Quyosh/oy belgisi tugma markazida (``margin:auto`` ularni burchakka qisardi)."""
    block = re.search(
        r"\.theme-toggle \.icon-sun, \.theme-toggle \.icon-moon \{(.*?)\}",
        _css(),
        re.DOTALL,
    )
    assert block, ".theme-toggle belgi qoidalari topilmadi"
    body = block.group(1)
    assert "display: flex" in body
    assert "align-items: center" in body
    assert "justify-content: center" in body
    assert "margin: auto" not in body


def test_local_storage_keys_are_namespaced() -> None:
    """Barcha saqlash kalitlari ``CONFIG.storePrefix`` (``hugecloud:``) orqali yasaladi."""
    script = _main_script()
    assert 'storePrefix: "hugecloud:"' in script

    store = _code_block("var STORE")
    entries = re.findall(r"^    (\w+): (.+?),?$", store, re.MULTILINE)
    assert entries, "STORE xaritasi topilmadi"
    for name, value in entries:
        assert value.startswith("CONFIG.storePrefix + "), f"{name} prefikssiz yasalgan: {value}"

    raw = re.findall(r'localStorage\.\w+Item\("([^"]+)"', _html())
    assert raw, "to'g'ridan-to'g'ri localStorage chaqiruvi kutiladi"
    bad = sorted(key for key in raw if not key.startswith("hugecloud:"))
    assert not bad, f"prefikssiz kalitlar: {bad}"

    # Har bir chaqiruv try/catch ichida (Safari private mode)
    assert "catch (e) { /* Safari private mode" in script


def test_stream_urls_are_relativized_for_proxy() -> None:
    """``BASE_URL`` noto'g'ri yoki ilova proksi ortida bo'lsa ham ijro ishlashi kerak."""
    probe = """
    console.log(JSON.stringify([
      toMediaUrl("http://localhost:8000/api/v1/stream/abc", "https://app.example", "/api/v1"),
      toMediaUrl("https://app.example/api/v1/stream/abc", "https://app.example", "/api/v1"),
      toMediaUrl("/api/v1/stream/abc", "https://app.example", "/api/v1"),
      toMediaUrl("https://cdn.other.com/media/x.mp4", "https://app.example", "/api/v1"),
      toMediaUrl("blob:https://app.example/uuid", "https://app.example", "/api/v1"),
      toMediaUrl("https://files.example/assets/a.mp4", "https://app.example", "/api/v1"),
      toMediaUrl("", "https://app.example", "/api/v1"),
      toMediaUrl(null, "https://app.example", "/api/v1")
    ]));
    """
    assert _run_node(probe) == [
        "/api/v1/stream/abc",                        # localhost -> nisbiy (proksi ortida ishlaydi)
        "/api/v1/stream/abc",                        # o'z origin'i -> nisbiy
        "/api/v1/stream/abc",                        # allaqachon nisbiy
        "https://cdn.other.com/media/x.mp4",         # tashqi CDN -> tegilmaydi
        "blob:https://app.example/uuid",             # blob: -> tegilmaydi
        "https://files.example/assets/a.mp4",        # boshqa host, boshqa yo'l -> tegilmaydi
        "",                                          # bo'sh -> bo'sh
        None,                                        # null -> null
    ]


def test_stream_urls_are_normalized_at_api_boundary() -> None:
    """Normallashtirish API javobi olingan zahoti (chegarada) qo'llanadi."""
    ensure = _code_block("function ensureStream")
    assert "normalizeStreamUrls(" in ensure
    assert ensure.index("normalizeStreamUrls(") < ensure.index("player.stream = stream")

    open_token = _code_block("function openSharedToken")
    # Ikkala yo'l ham: parolli (unlock) va parolsiz (GET) havola
    assert open_token.count("normalizeStreamUrls(") == 2

    normalizer = _code_block("function normalizeStreamUrls")
    assert "window.location.origin" in normalizer
    assert "CONFIG.apiPrefix" in normalizer
    assert "toMediaUrl(payload.stream_url" in normalizer
    assert "toMediaUrl(payload.download_url" in normalizer


def test_deep_link_survives_auth_detour() -> None:
    """Kirish detour'idan keyin deep-link ochiladi — ``boot()`` bilan bir xil shaklda."""
    after = _code_block("function afterAuth")

    # 1) payload boot()'dagi kabi normallashtiriladi (to'liq deep link ham, raw token ham)
    assert "extractShareToken(String(payload).trim())" in after
    assert "if (token) await openSharedToken(token)" in after

    # 2) bir martalik iste'mol: keyingi kirishda eski havola qayta ochilmaydi
    assert "state.startParam = null" in after
    assert "state.pendingImport = null" in after
    assert after.index("var payload =") < after.index("state.startParam = null")

    # 3) manbalar tartibi: JWT claim -> import -> URL parametri
    assert "state.startParam || state.pendingImport || readStartParamFromUrl()" in after

    boot = _code_block("async function boot")
    assert "extractShareToken(String(state.startParam))" in boot
    assert 'params.get("startapp") || params.get("start_param") || params.get("start")' in _code_block(
        "function readStartParamFromUrl"
    )


#: Ikkala parser (JS va Python) ham shu kiritishlarni bir xil qayta ishlashi kerak.
DEEP_LINK_CASES = (
    "https://t.me/hugecloud_bot?start=ABCdef123",
    "t.me/hugecloud_bot?start=ABCdef123",
    "https://cloud.example.com/api/v1/shares/ABCdef123",
    "https://cloud.example.com/s/ABCdef123",
    "https://cloud.example.com/share/l/ABCdef123",
    "ABCdef123",
    "  ABCdef123  ",
    "import ABCdef123",
    "AbC123_-xY",
    "abc",                          # juda qisqa — token emas
    "/shares/ABCdef123",            # sxemasiz yo'l: ikki tomon ham rad etadi
    "https://t.me/bot?start=short", # start parametri pattern'ga tushmaydi
    "",
)


def test_deep_link_parsing_matches_bot_side() -> None:
    """Mini App (JS) va bot (Python) deep-link parserlari **bir xil** natija beradi.

    Deep-linking ikki qatlamda ishlaydi: bot ``?start=<payload>`` ni ushlab
    parklaydi, Mini App esa ``?startapp=`` / JWT claim orqali qabul qiladi.
    Shartnoma ikki joyda takrorlangani uchun ularning bir-biridan og'ishi
    foydalanuvchi uchun "havola ishlamadi" degani — shuning uchun bu test
    haqiqiy Python funksiyasini haqiqiy JS funksiyasi bilan solishtiradi.
    """
    from bot.handlers.miniapp import extract_share_token

    probe = (
        "var CASES = " + json.dumps(list(DEEP_LINK_CASES)) + ";\n"
        "console.log(JSON.stringify(CASES.map(function (value) { return extractShareToken(value); })));"
    )
    assert _run_node(probe) == [extract_share_token(case) for case in DEEP_LINK_CASES]

    # Bo'sh qiymatlar ham ikki tomonda bir xil
    assert _run_node(
        'console.log(JSON.stringify([extractShareToken(null), extractShareToken(undefined)]));'
    ) == [extract_share_token(None), extract_share_token(None)]

    # Kamida bitta haqiqiy deep-link tanilishi kerak (aks holda test "bo'sh"dan o'tadi)
    assert extract_share_token("https://t.me/hugecloud_bot?start=ABCdef123") == "ABCdef123"


# --------------------------------------------------------------------------- #
# 3. Rasm ko'ruvchi (lightbox) va pleer yangilanishlari
# --------------------------------------------------------------------------- #
def test_photo_lightbox_viewer() -> None:
    """🖼️ Rasmlarni to'liq ekranda ko'rish (lightbox) mavjud."""
    html = _html()
    script = _main_script()
    for element_id in [
        "viewerBackdrop", "viewerImage", "viewerPrev", "viewerNext",
        "viewerClose", "viewerShare", "viewerZoom",
    ]:
        assert f'id="{element_id}"' in html, element_id
    for anchor in [
        "function openViewer", "function closeViewer", "function viewerStep",
        "function toggleViewerZoom", "function viewerApplyTransform",
    ]:
        assert anchor in script, anchor
    # Rasm kartasi bosilganda viewer ochiladi (oddiy menyu emas)
    assert "openViewer(file)" in _code_block("function fileCard")


def test_player_has_queue_repeat_and_shuffle() -> None:
    """🎬 Navbat oynasi + 🔁 takrorlash / 🔀 aralashtirish rejimlari."""
    script = _main_script()
    assert 'id="btnQueue"' in _html()
    assert "function openQueueSheet" in script
    assert "player.repeat" in script and "player.shuffle" in script
    play_next = _code_block("async function playNext")
    assert "player.shuffle" in play_next and "player.repeat" in play_next


def test_player_speed_menu_and_long_press_two_x() -> None:
    """⏩ Tezlik menyusi va uzoq bosishda vaqtinchalik 2× tezlik."""
    script = _main_script()
    assert "function openSpeedMenu" in script
    assert '$("btnSpeed").addEventListener("click", openSpeedMenu)' in script
    assert "function startLongPress" in script and "function cancelLongPress" in script
    assert "is-fast-forward" in script
    assert 'id="ffBadge"' in _html()


def test_player_swipe_to_seek_and_audio_visualizer() -> None:
    """Gorizontal — seek; vertikal — keyingi/oldingi (TikTok); audio ekvalayzeri."""
    script = _main_script()
    assert 'id="seekPreview"' in _html()
    assert "seekPreviewTime" in script
    assert 'dragState.mode = "seek"' in script
    # Vertikal swipe endi navigatsiya (ovoz gesture'i olib tashlandi)
    assert "playNext(1)" in script and "playNext(-1)" in script
    css = _css()
    assert ".audio-art .eq" in css and "eq-bounce" in css


def test_player_has_wake_lock() -> None:
    """Ijro paytida ekran o'chmasligi uchun Wake Lock (best-effort)."""
    script = _main_script()
    assert "function requestWakeLock" in script
    assert "function releaseWakeLock" in script
    assert 'navigator.wakeLock.request("screen")' in script


# --------------------------------------------------------------------------- #
# 5. Glass effekt (Diyor spetsifikatsiyasi) va pastki panel qatlamlari
# --------------------------------------------------------------------------- #
#: Spetsifikatsiyadagi qiymatlarni tashuvchi tokenlar. Ular **faqat** token
#: bloklarida e'lon qilinadi, komponentlarda esa ``var(...)`` orqali ishlatiladi
#: (``test_palette_lives_only_in_tokens_and_theme_map`` shuni kafolatlaydi).
_GLASS_TOKENS = (
    "--glass-grad-top",
    "--glass-grad-bottom",
    "--glass-tint",
    "--glass-inner-shadow",
    "--glass-frame-shadow",
    "--glass-drop-shadow",
    "--glass-edge",
    "--glass-hairline",
    "--glass-blur",
    "--glass-saturate",
    "--glass-shadow-blur",
    "--glass-nav-blur",
    "--glass-nav-bg",
    "--glass-panel-bg",
    "--glass-fallback-bg",
)

#: Glass retseptini tashkil etuvchi kompozit tokenlar (faqat bazaviy ``:root``).
_GLASS_RECIPE_TOKENS = (
    "--glass-inner",
    "--glass-frame",
    "--glass-hairline-shadow",
    "--glass-shadow",
    "--glass-raise-shadow",
    "--glass-header-shadow",
)


def _token_blocks(theme: str | None = None) -> str:
    """``:root``/``:root[...]`` token bloklarini (ixtiyoriy mavzu bo'yicha) yig'adi.

    ``:root[data-theme="dark"] .theme-toggle ...`` kabi **uslub** qoidalari
    hisobga olinmaydi: selektor ``:root`` bilan boshlanib, atributlardan keyin
    darhol ``{`` kelishi shart.
    """
    css = _css()
    parts: list[str] = []
    for match in re.finditer(r"(:root(?:\[[^\]]*\])*)\s*\{", css):
        selector = match.group(1)
        if theme and f'[data-theme="{theme}"]' not in selector:
            continue
        depth, index = 1, match.end()
        while index < len(css) and depth:
            if css[index] == "{":
                depth += 1
            elif css[index] == "}":
                depth -= 1
            index += 1
        parts.append(css[match.end() : index - 1])
    assert parts, f"token bloklari topilmadi (theme={theme!r})"
    return "\n".join(parts)


def _token_value(blocks: str, name: str) -> str:
    match = re.search(re.escape(name) + r"\s*:\s*([^;]+);", blocks)
    assert match, f"{name} e'lon qilinmagan"
    return match.group(1).strip()


def _all_rules() -> list[tuple[str, str]]:
    """Barcha uslub qoidalari: ``(selektor, tana)`` juftliklari.

    Selektor vergul bilan sanab o'tilgan bo'lsa ham **butun** ro'yxat qaytadi;
    qidiruv esa aynan mos selektor bo'yicha (``_rules``) amalga oshiriladi —
    shunda ``.sheet-backdrop, ..., .bulk-bar { ... }`` kabi umumiy qoidalar
    boshqa elementning uslubi bilan aralashib ketmaydi.
    """
    css = re.sub(r"/\*.*?\*/", "", _css(), flags=re.DOTALL)
    rules: list[tuple[str, str]] = []
    for match in re.finditer(r"([^{}]+)\{", css):
        selector = match.group(1).strip()
        if selector.startswith("@"):
            continue
        depth, index = 1, match.end()
        while index < len(css) and depth:
            if css[index] == "{":
                depth += 1
            elif css[index] == "}":
                depth -= 1
            index += 1
        rules.append((selector, css[match.end() : index - 1]))
    return rules


def _rules(selector: str) -> list[str]:
    """Aynan shu selektorga tegishli barcha qoidalarning tanalari."""
    bodies = [
        body
        for selector_list, body in _all_rules()
        if selector in [part.strip() for part in selector_list.split(",")]
    ]
    assert bodies, f"{selector} uslubi yo'q"
    return bodies


def _rule(selector: str) -> str:
    """Aynan shu selektorli **birinchi** qoidaning tanasi."""
    return _rules(selector)[0]


def _any_rule(selector: str, needle: str) -> bool:
    return any(needle in body for body in _rules(selector))


def _rgba_alpha(value: str) -> float:
    match = re.search(r"rgba\([^)]*?,\s*([0-9.]+)\s*\)", value)
    assert match, f"rgba qiymat emas: {value!r}"
    return float(match.group(1))


def test_glass_tokens_exist_in_both_themes() -> None:
    """Glass tokenlari yorug' va qorong'i temada **alohida** to'plam bo'lib turadi."""
    light, dark = _token_blocks("light"), _token_blocks("dark")
    recipe = _token_blocks()
    for name in _GLASS_TOKENS:
        assert f"{name}:" in light, f"yorug' temada {name} yo'q"
        assert f"{name}:" in dark, f"qorong'i temada {name} yo'q"
    for name in _GLASS_RECIPE_TOKENS:
        assert f"{name}:" in recipe, f"glass retseptida {name} yo'q"


def test_glass_light_theme_matches_specification() -> None:
    """Yorug' tema: spetsifikatsiyadagi gradient, oq qirra va yumshoq soya."""
    light = _token_blocks("light")
    assert _token_value(light, "--glass-grad-top").upper() == "#0063E3"
    assert _token_value(light, "--glass-grad-bottom").upper() == "#3AABFD"
    assert _token_value(light, "--glass-frame-shadow") == "rgba(255, 255, 255, 0.30)"
    assert _token_value(light, "--glass-drop-shadow") == "rgba(0, 0, 0, 0.06)"
    assert _token_value(light, "--glass-inner-shadow") == "rgba(0, 0, 0, 0.25)"
    assert float(_token_value(light, "--glass-shadow-blur").rstrip("px")) == 74.0
    assert 8.0 <= float(_token_value(light, "--glass-blur").rstrip("px")) <= 12.0


def test_glass_dark_theme_is_a_separate_set() -> None:
    """Qorong'i tema — yorug'dan **mustaqil** to'plam (to'qroq gradient, chuqur soya)."""
    light, dark = _token_blocks("light"), _token_blocks("dark")
    assert _token_value(dark, "--glass-grad-top").upper() == "#0A4FA8"
    assert _token_value(dark, "--glass-grad-bottom").upper() == "#2E8FE0"
    for name in ("--glass-grad-top", "--glass-grad-bottom", "--glass-nav-bg", "--glass-panel-bg"):
        assert _token_value(light, name) != _token_value(dark, name), f"{name} bir xil qolgan"

    # Oq qirra alfa pasaytirilgan (10–16%), soya esa chuqurroq va qoraroq.
    frame_dark = _rgba_alpha(_token_value(dark, "--glass-frame-shadow"))
    frame_light = _rgba_alpha(_token_value(light, "--glass-frame-shadow"))
    assert frame_light > frame_dark
    assert 0.10 <= frame_dark <= 0.16
    assert _rgba_alpha(_token_value(dark, "--glass-drop-shadow")) >= 0.45
    assert _rgba_alpha(_token_value(dark, "--glass-drop-shadow")) > _rgba_alpha(
        _token_value(light, "--glass-drop-shadow")
    )

    # Blur qorong'ida biroz kattaroq (10–14px) va `saturate()` bilan.
    blur_light = float(_token_value(light, "--glass-blur").rstrip("px"))
    blur_dark = float(_token_value(dark, "--glass-blur").rstrip("px"))
    assert blur_light < blur_dark <= 14.0
    assert "%" in _token_value(dark, "--glass-saturate")


def test_glass_surfaces_use_tokens_not_hardcoded_values() -> None:
    """Glass faqat tokenlar orqali: panel/sheet/modal/sarlavha ``var(--glass-*)``."""
    for selector in (".bottom-nav", ".sheet", ".modal", ".app-header"):
        assert _any_rule(selector, "var(--glass-"), f"{selector} glass tokenlaridan foydalanmaydi"
    # Gradient token sifatida quriladi (komponentda hardcode emas).
    assert "linear-gradient" in _token_blocks()
    assert _any_rule(".bottom-nav", "var(--glass-fill)")

    # Blur har bir glass yuzasida bor, lekin ochiq holatda (yopiq sheet/modal
    # uchun blur hisoblash — sof isrof).
    blurred = " ".join(_glass_blur_selectors())
    for surface in (".app-header", ".bottom-nav", ".sheet", ".modal", ".bulk-bar"):
        assert surface in blurred, f"{surface} uchun glass blur yo'q"
    assert ".sheet-backdrop.is-open .sheet" in blurred
    assert ".modal-backdrop.is-open .modal" in blurred
    assert ".bulk-bar.is-open" in blurred


def _glass_blur_selectors() -> list[str]:
    """``blur(var(--glass-blur))`` qo'llanadigan qoidalarning selektorlari."""
    return [
        selector_list
        for selector_list, body in _all_rules()
        if "backdrop-filter: blur(var(--glass-blur))" in body
    ]


def test_glass_is_limited_to_few_surfaces() -> None:
    """Ishlash ogohlantirishi: ``backdrop-filter`` ko'p kartalarga qo'llanmaydi."""
    for body in _rules(".card"):
        assert "backdrop-filter" not in body
        assert "var(--glass-" not in body
    glass_rules = _glass_blur_selectors()
    assert 3 <= len(glass_rules) <= 8, f"glass blur juda ko'p qoidada: {glass_rules}"


def test_glass_fallback_and_accessibility_preferences() -> None:
    """``backdrop-filter`` yo'q bo'lsa zaxira fon; reduced-motion/transparency."""
    css = _css()
    assert "@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px)))" in css
    assert "background-color: var(--glass-fallback-bg)" in css
    assert "@media (prefers-reduced-transparency: reduce)" in css
    reduced_motion = re.search(
        r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", css, re.DOTALL
    )
    assert reduced_motion, "reduced-motion bloki yo'q"
    assert "blur(4px)" in reduced_motion.group(1), "harakat kamaytirilganda blur kamaytirilmaydi"


def test_layer_scale_is_single_and_tokenised() -> None:
    """Yagona qatlam shkalasi: kontent < sarlavha < panel < sheet < modal < toast."""
    css = _css()
    names = (
        "--z-content", "--z-header", "--z-nav", "--z-bulk", "--z-fab-backdrop", "--z-fab",
        "--z-sheet", "--z-player", "--z-viewer", "--z-modal", "--z-toast", "--z-upload",
    )
    values: dict[str, int] = {}
    for name in names:
        match = re.search(re.escape(name) + r"\s*:\s*(\d+)", css)
        assert match, f"{name} tokeni yo'q"
        values[name] = int(match.group(1))

    assert values["--z-content"] < values["--z-header"] < values["--z-nav"]
    assert values["--z-nav"] < values["--z-sheet"] < values["--z-modal"] < values["--z-toast"]
    assert values["--z-bulk"] > values["--z-nav"], "ommaviy amallar paneli navigatsiya ustida bo'lsin"
    assert values["--z-modal"] < values["--z-toast"] < values["--z-upload"]

    # Komponentlar endi xom raqam yozmaydi — faqat tokendan oladi.
    for selector, token in (
        (".bottom-nav", "--z-nav"),
        (".bulk-bar", "--z-bulk"),
        (".toast-stack", "--z-toast"),
        (".modal-backdrop", "--z-modal"),
        (".player-backdrop", "--z-player"),
        (".viewer-backdrop", "--z-viewer"),
        (".fab-dock", "--z-fab"),
    ):
        assert _any_rule(selector, f"z-index: var({token})"), f"{selector} -> {token}"
    # Xom raqamli qatlam qolmagan (0/1/2 kabi ichki indekslardan tashqari).
    leftover = sorted(
        set(re.findall(r"z-index:\s*([4-9]\d|100)\b", css))
    )
    assert not leftover, f"tokenlashtirilmagan z-index qiymatlari: {leftover}"


def test_bottom_nav_geometry_respects_safe_area() -> None:
    """Panel balandligi token bilan, ``env(safe-area-inset-*)`` hisobga olingan."""
    css = _css()
    assert "--nav-h: 64px" in css
    assert "--nav-gap: 10px" in css
    assert "--nav-space: calc(var(--nav-h) + var(--nav-gap))" in css
    assert "env(safe-area-inset-bottom" in css
    assert "env(safe-area-inset-top" in css

    nav = _rule(".bottom-nav")
    assert "height: var(--nav-h)" in nav
    assert "bottom: calc(var(--safe-b) + var(--nav-gap))" in nav
    assert "isolation: isolate" in nav, "panel ichidagi qatlamlar yopilmagan"

    body = re.search(r"(?:^|\n)body\s*\{(.*?)\}", css, re.DOTALL)
    assert body, "body uslubi topilmadi"
    assert "padding-bottom: calc(var(--nav-space) + var(--safe-b)" in body.group(1)

    assert "var(--safe-t)" in _rule(".app-header"), "notch uchun yuqori inset hisobga olinmagan"
    # Kontent qatlami alohida stacking context (karta animatsiyalari panelni bosmasin).
    assert "isolation: isolate" in _rule("#main")


def test_closed_overlays_never_capture_pointer_events() -> None:
    """Yopiq qatlamlar (pleer/sheet/modal) bosishni ushlamasligi shart.

    ``pointer-events: none`` ota-onada bo'lsa ham bola ``auto`` bo'lsa
    hit-test'ga qaytadi — shu sababli butun daraxt majburiy o'chiriladi.
    """
    css = _css()
    for selector in (
        ".player-backdrop",
        ".viewer-backdrop",
        ".sheet-backdrop",
        ".modal-backdrop",
        ".fab-backdrop",
        ".upload-ov",
    ):
        assert f"{selector}:not(.is-open)" in css, selector
        assert f"{selector}:not(.is-open) *" in css, f"{selector}: bolalar ham o'chirilmagan"

    guarded = [
        selector_list
        for selector_list, body in _all_rules()
        if "pointer-events: none !important" in body
    ]
    assert guarded, "yopiq qatlamlar uchun pointer-events qoidasi yo'q"
    for selector in (".player-backdrop", ".sheet-backdrop", ".modal-backdrop", ".viewer-backdrop"):
        assert any(
            f"{selector}:not(.is-open)" in selector_list
            and f"{selector}:not(.is-open) *" in selector_list
            for selector_list in guarded
        ), f"{selector} yopiq holatda bosishni ushlab qoladi"


def test_spiral_canvas_is_limited_to_v2_design() -> None:
    """Spiral qatlami faqat v2'da ko'rinadi (klassikda sahifani kengaytirmasin)."""
    css = _css()
    base = re.search(r"#spiral-canvas\s*\{(.*?)\}", css, re.DOTALL)
    assert base, "``#spiral-canvas`` bazaviy qoidasi yo'q"
    assert "display: none" in base.group(1), "klassik rejimda canvas yashirilmagan"

    v2 = re.search(r'html\[data-design="v2"\]\s+#spiral-canvas\s*\{(.*?)\}', css, re.DOTALL)
    assert v2, "v2 uchun canvas qoidasi yo'q"
    assert "display: block" in v2.group(1)
    assert "position: fixed" in v2.group(1)


# --------------------------------------------------------------------------- #
# 6. Diyor tuzatishlari: emoji -> SVG, neytral sarlavha, yuklash tartibi,
#    rasm ko'ruvchi (zoom-siz). (2026-09-25)
# --------------------------------------------------------------------------- #
#: Emoji diapazonlari (Diyor #2 talabida ko'rsatilgan regex).
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]"
)


def _rgba_channels(value: str) -> tuple[float, float, float]:
    """``rgba(r, g, b, a)`` dan ``(r, g, b)`` ni ajratib oladi."""
    match = re.search(r"rgba\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)", value)
    assert match, f"rgba qiymat emas: {value!r}"
    return float(match.group(1)), float(match.group(2)), float(match.group(3))


def test_index_html_has_no_emoji() -> None:
    """Diyor #2: ``index.html`` da bitta ham emoji qolmasin (monoxrom SVG)."""
    html = _html()
    leftovers = _EMOJI_RE.findall(html)
    assert not leftovers, f"emoji qoldiqlari: {sorted(set(leftovers))}"

    # Barqaror "oilaviy" uslub: hairline SVG ikonkalar.
    script = _main_script()
    assert "var SVG_PATHS" in script and "function svgIcon" in script
    assert 'viewBox="0 0 24 24"' in script
    assert 'stroke="currentColor"' in script
    assert 'stroke-width="1.5"' in script
    assert 'aria-hidden="true"' in script


def test_header_background_is_neutral_not_blue() -> None:
    """Diyor #1: sarlavha foni neytral (ko'k tint yo'q), light/dark farqli.

    Pastki panel (``.bottom-nav``) tegilmaydi — u ko'k glass bo'lib qoladi.
    """
    light, dark = _token_blocks("light"), _token_blocks("dark")

    # Ikkala mavzuda ham alohida neytral header tokenlari bor.
    for name in ("--glass-header-bg", "--glass-header-fill", "--glass-header-hairline"):
        assert f"{name}:" in light, f"yorug' temada {name} yo'q"
        assert f"{name}:" in dark, f"qorong'i temada {name} yo'q"

    light_bg = _token_value(light, "--glass-header-bg")
    dark_bg = _token_value(dark, "--glass-header-bg")
    assert light_bg != dark_bg, "light/dark header foni bir xil"

    # Piksellar bo'yicha neytral: kanallar orasidagi farq kichik (ko'k ustunlik yo'q).
    for bg in (light_bg, dark_bg):
        r, g, b = _rgba_channels(bg)
        assert max(r, g, b) - min(r, g, b) <= 6, f"header fonida rang tinti bor: {bg}"

    # Sarlavha endi ko'k ``--glass-nav-bg``/``--glass-fill`` dan foydalanmaydi.
    header = " ".join(_rules(".app-header"))
    assert "var(--glass-header-bg)" in header
    assert "var(--glass-nav-bg)" not in header
    assert "var(--glass-fill)" not in header
    # v2 qatlami ham neytral header tokenlarini ishlatadi.
    v2_header = [body for sel, body in _all_rules() if sel == 'html[data-design="v2"] .app-header']
    assert v2_header and all("var(--glass-header-bg)" in body for body in v2_header)

    # Pastki panel glass ko'rinishida qoladi (Diyor faqat tepani aytdi).
    nav = " ".join(_rules(".bottom-nav"))
    assert "var(--glass-fill)" in nav or _any_rule(".bottom-nav", "var(--glass-fill)")


def test_viewer_has_no_zoom_animation() -> None:
    """Diyor #4: preview'da ``scale`` zoom animatsiyasi yo'q, o'rniga fade.

    Zoom funksiyasi (pinch/double-tap) saqlanadi, lekin ochilish/yopish va
    rasm almashtirish **animatsiyasiz** (transform transition yo'q).
    """
    img = _rule(".viewer-img")
    # Transform uchun transition bo'lmasin — masshtab sakrab o'zgaradi.
    assert "transition: transform" not in img, "viewer'da transform (zoom) animatsiyasi qolgan"
    # O'rniga yumshoq opacity cross-fade (100-140 ms).
    assert re.search(r"transition:\s*opacity\s+1[0-4]\dms", img), "opacity fade yo'q"
    # Quti barqaror: thumbnail -> to'liq rasm o'tishida layout shift bo'lmasin.
    assert "width: 100%" in img and "height: 100%" in img

    # reduced-motion: umuman animatsiyasiz.
    # reduced-motion: umuman animatsiyasiz (bir nechta blok bo'lishi mumkin).
    reduced_blocks = [
        m.group(1)
        for m in re.finditer(
            r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", _css(), re.DOTALL
        )
    ]
    assert reduced_blocks, "reduced-motion bloki yo'q"
    assert any(
        ".viewer-img" in block and "transition: none" in block for block in reduced_blocks
    ), "reduced-motion'da viewer fade o'chirilmagan"

    script = _main_script()
    assert 'classList.add("is-ready")' in script
    assert 'classList.remove("is-ready")' in script


def test_upload_never_forces_new_file_to_top() -> None:
    """Diyor #3: yangi yuklangan fayl majburan tepaga qo'yilmaydi, skrol sakramaydi.

    Joy faqat joriy saralash tartibidan kelib chiqadi; ``unshift``/lokal
    "prepend" yo'q. Yuklashdan keyingi yangilanish skrolni saqlaydi.
    """
    script = _main_script()
    assert "unshift(" not in script, "ro'yxat boshiga majburiy qo'shish (unshift) qolgan"

    upload = script[script.index("function startUpload"): script.index("async function startImport")]
    assert "preserveScroll: true" in upload, "yuklashdan keyin skrol saqlanmaydi"
    assert "skeleton: false" in upload, "yuklashda skeleton ro'yxatni bo'shatib skrolni sakratadi"
    assert "scrollTo(0, 0)" not in upload and "scrollTo(0,0)" not in upload

    # ``loadFiles`` skrolni tiklaydi.
    load = _code_block("async function loadFiles")
    assert "preserveScroll" in load and "window.scrollTo(0, keepScroll)" in load

    # Saralash mantiqi o'zgarmagan: "new" -> created_at desc.
    assert '"new": ["created_at", true]' in script

