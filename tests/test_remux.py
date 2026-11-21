"""``services.remux`` — ixtiyoriy ffmpeg remux (mkv/avi -> mp4) testlari.

Testlar ffmpeg **talab qilmaydi**: tashqi jarayon ``runner`` orqali
almashtiriladi. Faqat bitta integratsion test (ffmpeg bo'lsa) haqiqiy
konteynerni remux qiladi va u ham mavjud bo'lmasa o'tkazib yuboriladi.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from services import remux


class FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_runner(probe: str = "5.0", calls: list[list[str]] | None = None, fail: bool = False):
    """``ffprobe`` ga javob beradi, ``ffmpeg`` esa chiqish faylini yozadi."""

    def run(command, **_kwargs):
        if calls is not None:
            calls.append(list(command))
        if "ffprobe" in command[0]:
            return FakeCompleted(0, stdout=probe + "\n")
        if fail:
            return FakeCompleted(1, stderr="boom: unsupported codec")
        Path(command[-1]).write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 16)
        return FakeCompleted(0)

    return run


@pytest.fixture()
def fake_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remux.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_remux_missing_file_is_reported(tmp_path: Path) -> None:
    result = remux.remux_to_mp4(tmp_path / "yoq.mkv")
    assert result.ok is False
    assert result.reason == "missing"


def test_remux_respects_size_limit(tmp_path: Path, fake_ffmpeg: None) -> None:
    source = tmp_path / "katta.mkv"
    source.write_bytes(b"x" * 2048)
    result = remux.remux_to_mp4(source, max_size=16)
    assert result.ok is False
    assert result.reason == "too_large"


def test_remux_without_ffmpeg_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remux.shutil, "which", lambda name: None)
    source = tmp_path / "clip.mkv"
    source.write_bytes(b"x" * 64)
    result = remux.remux_to_mp4(source)
    assert result.ok is False
    assert result.reason == "ffmpeg_missing"


def test_remux_uses_copy_and_writes_cache(tmp_path: Path, fake_ffmpeg: None) -> None:
    source = tmp_path / "01 - klip.mkv"
    source.write_bytes(b"x" * 128)
    calls: list[list[str]] = []

    result = remux.remux_to_mp4(source, cache_dir=tmp_path / "cache", runner=_fake_runner(calls=calls))

    assert result.ok is True
    assert result.reason == "remuxed"
    assert result.output is not None and result.output.is_file()
    ffmpeg_calls = [call for call in calls if "ffmpeg" in call[0]]
    assert len(ffmpeg_calls) == 1
    assert "-c" in ffmpeg_calls[0] and "copy" in ffmpeg_calls[0], "qayta kodlash bo'lmasligi kerak"
    assert "-movflags" in ffmpeg_calls[0]


def test_remux_reuses_cache(tmp_path: Path, fake_ffmpeg: None) -> None:
    source = tmp_path / "clip.mkv"
    source.write_bytes(b"x" * 128)
    cache = tmp_path / "cache"
    first = remux.remux_to_mp4(source, cache_dir=cache, runner=_fake_runner())
    assert first.ok is True

    calls: list[list[str]] = []
    second = remux.remux_to_mp4(source, cache_dir=cache, runner=_fake_runner(calls=calls))
    assert second.ok is True
    assert second.reason == "cached"
    assert calls == [], "kesh mavjud bo'lsa ffmpeg chaqirilmasligi kerak"


def test_remux_respects_duration_limit(tmp_path: Path, fake_ffmpeg: None) -> None:
    source = tmp_path / "uzun.mkv"
    source.write_bytes(b"x" * 128)
    result = remux.remux_to_mp4(
        source, cache_dir=tmp_path / "cache", max_duration=10, runner=_fake_runner(probe="9999")
    )
    assert result.ok is False
    assert result.reason == "too_long"


def test_remux_failure_falls_back_to_download(tmp_path: Path, fake_ffmpeg: None) -> None:
    source = tmp_path / "buzuq.mkv"
    source.write_bytes(b"x" * 128)
    result = remux.remux_to_mp4(
        source, cache_dir=tmp_path / "cache", runner=_fake_runner(fail=True)
    )
    assert result.ok is False
    assert result.reason == "ffmpeg_failed"
    assert "unsupported codec" in result.message


def test_cache_path_includes_size(tmp_path: Path) -> None:
    source = tmp_path / "clip.mkv"
    source.write_bytes(b"x" * 10)
    first = remux.cache_path_for(source, tmp_path / "c")
    source.write_bytes(b"x" * 20)
    second = remux.cache_path_for(source, tmp_path / "c")
    assert first != second, "fayl hajmi o'zgarsa kesh boshqacha bo'lishi kerak"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg o'rnatilmagan")
def test_remux_real_mkv_to_mp4(tmp_path: Path) -> None:
    """Haqiqiy ffmpeg bilan mkv -> mp4 (integratsiya)."""
    source = tmp_path / "real.mkv"
    make = remux.subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=64x64:rate=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "mpeg4",
            "-c:a",
            "mp3",
            "-shortest",
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if make.returncode != 0 or not source.is_file():
        pytest.skip(f"test mkv yasalmadi: {make.stderr[-200:]}")

    result = remux.remux_to_mp4(source, cache_dir=tmp_path / "cache")
    assert result.ok is True, result.message
    assert result.output is not None and result.output.is_file()
    assert result.output.read_bytes()[4:8] == b"ftyp"
