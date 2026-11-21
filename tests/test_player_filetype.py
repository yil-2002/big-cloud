"""Mini App pleer tanlovi — ``category``/``mime_type`` bo'yicha (kengaytma emas).

Diyor shikoyati: ``tmp_3295`` (kengaytmasiz) fayl uchun pleer ochilmasdi.
Sabab — frontend turni kengaytma/kategoriyaga qarab tanlardi. Endi:

* ``mediaKind`` server bergan ``mime_type`` + ``category`` asosida ishlaydi;
* kengaytmasiz ``tmp_3295`` + ``video/mp4`` → ``<video>``;
* ``video/x-matroska``/``video/x-msvideo`` → pleer o'rniga xabar + yuklab olish;
* light/dark mavzu token'lari va glass qatlamlar saqlanib qolgan.

Bu modul ``mediaKind`` ni Node'da **haqiqatan** ishga tushiradi (sof funksiya,
DOM talab qilmaydi) va HTML statik shartnomalarini tekshiradi.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB_FILE = ROOT / "web" / "index.html"

NODE = shutil.which("node")


def _html() -> str:
    return WEB_FILE.read_text(encoding="utf-8")


def _main_script() -> str:
    scripts = re.findall(r"<script>(.*?)</script>", _html(), re.DOTALL)
    assert scripts, "ichki <script> bloklari topilmadi"
    return max(scripts, key=len)


#: ``mediaKind`` bog'liq bo'lgan barcha konstantalar (tartib muhim emas).
_CONSTANTS = re.compile(
    r"var BROWSER_VIDEO_MIMES = \{.*?var IMAGE_EXTS = \{[^}]*\};",
    re.DOTALL,
)


def _extract_function(script: str, name: str) -> str:
    """IIFE ichidan bitta funksiyani qavslar muvozanati bo'yicha ajratish."""
    start = script.index("function " + name + "(")
    brace = script.index("{", start)
    depth = 0
    for index in range(brace, len(script)):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                return script[start : index + 1]
    raise AssertionError(f"{name} funksiyasi yopilmagan")


def _media_harness() -> str:
    script = _main_script()
    match = _CONSTANTS.search(script)
    assert match, "pleer mime jadvallari topilmadi"
    functions = "\n\n".join(
        _extract_function(script, name) for name in ("baseMime", "fileExtension", "mediaKind")
    )
    return match.group(0) + "\n\n" + functions


def _run_media_kind(cases: list[dict[str, Any]]) -> list[str]:
    if NODE is None:
        pytest.skip("Node.js o'rnatilmagan")
    harness = (
        _media_harness()
        + "\n\nvar CASES = "
        + json.dumps(cases, ensure_ascii=False)
        + ";\nconsole.log(JSON.stringify(CASES.map(function (f) { return mediaKind(f); })));\n"
    )
    tmp = Path("/tmp/hugecloud_mediakind_probe.js")
    tmp.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(tmp)], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, f"Node xatosi: {result.stderr[:800]}"
    return json.loads(result.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# Node'da haqiqiy ishga tushirish
# ---------------------------------------------------------------------------
def test_extensionless_tmp_file_with_mp4_opens_video() -> None:
    """``tmp_3295`` + ``video/mp4`` → ``<video>`` (asosiy shikoyat)."""
    kinds = _run_media_kind(
        [
            {"name": "tmp_3295", "category": "video", "mime_type": "video/mp4"},
            # Kategoriya eski yozuvlarda "other" bo'lishi mumkin — mime ustun.
            {"name": "tmp_3295", "category": "other", "mime_type": "video/mp4"},
            {"name": "tmp_3295", "category": "other", "mime_type": "video/mp4; codecs=avc1"},
        ]
    )
    assert kinds == ["video", "video", "video"]


def test_unsupported_containers_are_flagged() -> None:
    """mkv/avi/flv/ts → ``unsupported`` (pleer emas, xabar + yuklab olish)."""
    kinds = _run_media_kind(
        [
            {"name": "01.mkv", "category": "video", "mime_type": "video/x-matroska"},
            {"name": "[88].avi", "category": "video", "mime_type": "video/x-msvideo"},
            {"name": "tmp_1", "category": "video", "mime_type": "video/x-flv"},
            {"name": "tmp_2", "category": "video", "mime_type": "video/mp2t"},
            # Mime yo'q, lekin kengaytma ma'lum — eski yozuvlar uchun zaxira.
            {"name": "clip.mkv", "category": "video", "mime_type": None},
        ]
    )
    assert kinds == ["unsupported"] * 5


def test_audio_and_image_and_other() -> None:
    kinds = _run_media_kind(
        [
            {"name": "song", "category": "audio", "mime_type": "audio/mpeg"},
            {"name": "voice", "category": "voice", "mime_type": "audio/ogg"},
            {"name": "pic", "category": "photo", "mime_type": "image/png"},
            {"name": "anim.gif", "category": "animation", "mime_type": "image/gif"},
            {"name": "tmp_9", "category": "other", "mime_type": None},
            {"name": "doc.pdf", "category": "document", "mime_type": "application/pdf"},
        ]
    )
    assert kinds == ["audio", "audio", "image", "image", "other", "other"]


def test_plain_mp4_and_webm_stay_video() -> None:
    kinds = _run_media_kind(
        [
            {"name": "clip.mp4", "category": "video", "mime_type": "video/mp4"},
            {"name": "clip.webm", "category": "video", "mime_type": "video/webm"},
            {"name": "tmp_mov", "category": "video", "mime_type": "video/quicktime"},
            {"name": "clip.ogv", "category": "video", "mime_type": None},
        ]
    )
    assert kinds == ["video", "video", "video", "video"]


# ---------------------------------------------------------------------------
# HTML statik shartnomalar
# ---------------------------------------------------------------------------
def test_player_uses_source_type_from_mime() -> None:
    html = _html()
    assert 'id="mediaSource"' in html, "``<source>`` elementi bo'lishi kerak"
    assert re.search(r'<source id="mediaSource"[^>]*type=', html), "tur atributi e'lon qilinishi kerak"
    assert "function applyMediaSource(" in html
    assert "stream.mime_type" in html, "pleer server bergan mime'ni ishlatishi kerak"


def test_unsupported_container_shows_message_and_download() -> None:
    html = _html()
    assert "function offerUnsupportedMedia(" in html
    block = html[html.index("async function offerUnsupportedMedia(") :]
    block = block[: block.index("\n  async function downloadFile(")]
    assert "brauzerda ijro etilmaydi" in block, "aniq o'zbekcha sabab kerak"
    assert "Yuklab olish" in block, "yuklab olish tugmasi kerak"
    assert "downloadFile(file)" in block


def test_player_selection_uses_category_and_mime_not_extension() -> None:
    html = _html()
    play = html[html.index("async function playFile(") :]
    play = play[: play.index("\n  /** Navbat bo'ylab")]
    assert 'mediaKind(file) === "unsupported"' in play, "mkv/avi pleerdan oldin to'silishi kerak"
    assert "offerUnsupportedMedia(file)" in play
    # Eski kengaytmaga tayanadigan shart qolmagan.
    assert 'file.category === "photo"' not in play


def test_theme_tokens_and_glass_layers_preserved() -> None:
    """Light/dark token'lari va glass/pleer qatlamlari o'zgarmagan."""
    html = _html()
    # Yorug' mavzu
    assert "#FFFFFF" in html and "#0066FF" in html
    # Qorong'i mavzu
    assert "#0F172A" in html and "#1E293B" in html
    # Glass / pleer z-shkalasi
    assert "--z-player: 75" in html
    assert ".player-backdrop" in html
    assert "backdrop-filter" in html
    assert '[data-theme="light"]' in html and '[data-theme="dark"]' in html
