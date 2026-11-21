"""🎬 Immersiv pleer va premium ko'rinish — statik UI shartnomasi.

Bu testlar ``web/index.html``ni brauzersiz tekshiradi (loyihaning boshqa
frontend testlari bilan bir xil uslub):

* **A. Pleer** — to'liq ekran (suzuvchi karta emas), safe-area, boshqaruv
  avtoyashirish (``controls-hidden``), drag-down yopish, ikki marta tegish
  indikatorlari, ``savedPosition``/``playNext``, landscape moslashuvi.
* **B. Premium** — chrome uchun yagona SVG ikonka tizimi (``svgIcon``),
  karta amallarida emoji yo'q, blur-up thumbnail, davomiylik nishoni,
  bo'sh holatda harakat tugmasi, tabular raqamlar.

Kategoriya identifikatsiyasi ham endi **monoxrom hairline SVG** (``SVG_PATHS``
xaritasi + ``data-icon`` o'rinbosarlari): ilovada birorta emoji qolmaydi.
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web" / "index.html"


def _html() -> str:
    return WEB.read_text(encoding="utf-8")


def _css(html: str | None = None) -> str:
    text = html or _html()
    return text[text.index("<style>") : text.index("</style>")]


def _script(html: str | None = None) -> str:
    text = html or _html()
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, re.DOTALL)
    return max(blocks, key=len)


# --------------------------------------------------------------------------- #
# A. Immersiv pleer
# --------------------------------------------------------------------------- #
def test_player_is_fullscreen_not_floating_card() -> None:
    """Pleer butun ekranni egallaydi — 720px'lik suzuvchi karta emas."""
    css = _css()
    player_block = css[css.index(".player-backdrop") : css.index(".player-head")]
    assert "max-width: 720px" not in player_block
    assert "inset: 0" in player_block
    assert "border-radius: 0" in player_block
    assert "var(--media-bg)" in player_block, "fon to'liq qora (--media-bg)"


def test_player_respects_safe_areas() -> None:
    """Notch va gesture-bar: head yuqori, controls pastki inset'ni hurmat qiladi."""
    css = _css()
    head = css[css.index(".player-head") : css.index(".player-title")]
    assert "env(safe-area-inset-top" in head
    controls = css[css.index(".player-controls") : css.index(".progress ")]
    assert "env(safe-area-inset-bottom" in controls


def test_player_controls_autohide() -> None:
    """Ijro paytida boshqaruv 3s dan keyin yashirinadi, tegilsa qaytadi."""
    css, script = _css(), _script()
    assert ".player.controls-hidden" in css
    assert "function showControls" in script
    assert "function toggleControls" in script
    assert "3000" in script, "3 sekund taymer"
    # Pauza bo'lsa yashirilmaydi
    assert "!media.paused" in script


def test_player_grip_closes_and_swipe_navigates() -> None:
    """Tutqich/✕ pleerni yopadi; vertikal swipe — keyingi/oldingi media (TikTok uslubi)."""
    html, script = _html(), _script()
    assert 'id="playerGrip"' in html and 'id="playerClose"' in html
    assert "onDragStart" in script and "onDragEnd" in script
    assert "function closePlayer" in script
    assert "playerGrip" in script and "closePlayer" in script
    # TikTok uslubi: pastga = oldingi, yuqoriga = keyingi (slide animatsiya bilan)
    drag_end = script[script.index("function onDragEnd") :][:1200]
    assert "playNext(-1)" in drag_end and "playNext(1)" in drag_end
    assert "is-sliding" in script


def test_player_double_tap_skip_indicators() -> None:
    """Ikki marta tegish: chap −10s / o'ng +10s, vizual indikator bilan."""
    html, script = _html(), _script()
    assert 'id="skipIndLeft"' in html and 'id="skipIndRight"' in html
    assert "flashSkipIndicator" in script
    # Sensor uchun pointerup asosidagi double-tap ham bor (dblclick faqat desktop)
    assert "lastStageTap" in script


def test_player_saved_position_and_playnext() -> None:
    """savedPosition (davomlash) va playNext (keyingi fayl + peek) mavjud."""
    html, script = _html(), _script()
    assert "function savedPosition" in script
    assert "function playNext" in script
    assert 'id="playerPeek"' in html
    # Yopilganda pozitsiya saqlanadi.
    # VPS'dagi kodda pozitsiya yagona API orqali boshqariladi: o'qish uchun
    # ``savedPosition(file)``, yozish uchun ``savePosition(force)``. Arxivdagi
    # ``savedPosition(file, value)`` setter varianti ikkinchi nusxa bo'lib,
    # JS'da oxirgi ta'rif ustun bo'lgani uchun "tugaganda boshidan" mantiqini
    # jimgina o'chirib qo'yardi — shu sababli yagona ``savePosition`` saqlandi.
    close = script[script.index("function closePlayer") :][:400]
    assert "savePosition(" in close
    # Tugagach keyingisiga o'tadi
    assert "playNext(1)" in script


def test_player_landscape_and_orientation_lock() -> None:
    """Landscape media query + fullscreen'da best-effort orientation lock."""
    css, script = _css(), _script()
    assert "orientation: landscape" in css
    assert 'screen.orientation.lock("landscape")' in script


def test_player_progress_touch_target() -> None:
    """Progress tegish maydoni ≥ 44px (barmoq uchun)."""
    css = _css()
    progress = css[css.index(".progress {") : css.index(".progress-track")]
    match = re.search(r"height:\s*(\d+)px", progress)
    assert match and int(match.group(1)) >= 44, progress


# --------------------------------------------------------------------------- #
# B. Premium ko'rinish
# --------------------------------------------------------------------------- #
def test_svg_icon_system_exists() -> None:
    """Yagona ikonka tizimi: svgIcon() + SVG_PATHS, currentColor stroke."""
    script = _script()
    assert "var SVG_PATHS" in script
    assert "function svgIcon" in script
    assert 'stroke="currentColor"' in script


def test_card_actions_use_svg_not_emoji() -> None:
    """Karta amallari (ijro/pin/ulashish/menyu) SVG — emoji emas."""
    script = _script()
    card = script[script.index("function fileCard") : script.index("function miniButton")]
    for emoji in ("▶", "📌", "📍", "🔗", "⋯", "👁"):
        assert emoji not in card, f"karta amalida emoji qolgan: {emoji}"
    assert 'miniButton("play"' in card
    assert 'miniButton("pin"' in card
    assert 'miniButton("link"' in card
    assert 'miniButton("dots"' in card
    # Nishonlar ham SVG + matn
    assert 'svgIcon("star"' in card and 'svgIcon("shield"' in card


def test_thumb_blur_up_and_duration_badge() -> None:
    """Thumbnail blur-up bilan paydo bo'ladi; video davomiyligi nishoni bor."""
    css, script = _css(), _script()
    assert ".thumb-img.is-loaded" in css
    assert "blur(8px)" in css
    assert 'onload: function () { this.classList.add("is-loaded"); }' in script
    assert ".dur-badge" in css
    assert 'class: "dur-badge"' in script


def test_empty_state_has_action_button() -> None:
    """Bo'sh holat: ikonka + izoh + "Birinchi faylni yuklang" tugmasi."""
    script = _script()
    empty = script[script.index("function renderEmpty") : script.index("function fileCard")]
    assert "empty-art" in empty
    assert "Birinchi faylni yuklang" in empty
    assert "startUpload" in empty


def test_category_icons_are_monochrome_svg() -> None:
    """Kategoriya identifikatsiyasi monoxrom hairline SVG — emoji emas.

    Ilgari bu yerda emoji saqlanishi talab qilinardi; yangi kontrakt esa
    "index.html'da 0 emoji" (Diyor #2). Har bir kategoriya uchun barqaror
    ``SVG_PATHS`` kaliti va yagona hairline uslub tekshiriladi.
    """
    html, script = _html(), _script()

    # 1) Hech qanday emoji qolmagan (spec diapazoni).
    assert not re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]", html)

    # 2) Yagona SVG ikonka tizimi va hairline uslub shartlari.
    assert "var SVG_PATHS" in script and "function svgIcon" in script
    assert 'viewBox="0 0 24 24"' in script
    assert 'fill="none"' in script
    assert 'stroke="currentColor"' in script
    assert 'stroke-width="1.5"' in script
    assert 'stroke-linecap="round"' in script and 'stroke-linejoin="round"' in script
    assert 'aria-hidden="true"' in script

    # 3) Har bir kategoriya ikonkasi ``SVG_PATHS`` da mavjud.
    icons_block = script[script.index("var SVG_PATHS"): script.index("function svgIcon")]
    for key in ("image", "film", "music", "mic", "file", "archive", "robot", "apple", "box"):
        assert re.search(rf"\b{re.escape(key)}\s*:", icons_block), key

    # 4) Guruh chiplarida barqaror SVG identifikatori (``icon`` maydoni).
    groups = script[script.index("var GROUPS = ["): script.index("var SORT_MAP")]
    for key in ("clipboard", "film", "image", "music", "file", "archive", "robot", "pin"):
        assert f'icon: "{key}"' in groups, key

    # 5) Statik markupdagi emoji o'rnini ``data-icon`` egallagan.
    assert 'data-icon="trash"' in html and 'data-icon="link"' in html
    assert "hydrateIcons(document)" in script

    # 6) ``ICONS`` xaritasi endi SVG kalitlariga ishora qiladi (emoji emas).
    icons_map = script[script.index("var ICONS = {"): script.index("var GROUPS = [")]
    assert "🖼" not in icons_map and '"image"' in icons_map and '"film"' in icons_map


def test_press_states_and_reduced_motion() -> None:
    """Bosish holatlari (scale) va prefers-reduced-motion hurmati."""
    css = _css()
    assert ".card:active" in css
    assert ".mini-btn:active" in css
    assert "prefers-reduced-motion: reduce" in css
