"""Ixtiyoriy ffmpeg remux: brauzer o'ynay olmaydigan konteynerni mp4 ga o'tkazish.

Nega kerak: ``mkv``/``avi`` kabi konteynerlar Chromium ``<video>`` da
ochilmaydi (qora ekran). Ko'pincha ichidagi kodek (H.264/AAC) allaqachon
qo'llab-quvvatlanadi — shuning uchun **qayta kodlash shart emas**: ffmpeg
``-c copy`` bilan faqat konteynerni almashtiradi (tez, sifat yo'qolmaydi).

Muhim siyosat:

* bu ish **so'rov paytida emas**, oldindan/lozim bo'lganda bajariladi (CLI:
  ``python -m services.filetype --remux <fayl>``);
* natija keshga yoziladi va qayta ishlatiladi;
* vaqt/hajm chegarasi qat'iy (sukut: 10 daqiqa / 500 MB);
* biror sabab bilan ishlamasa (ffmpeg yo'q, juda katta, xato) — **xato
  ko'tarilmaydi**: ``ok=False`` qaytadi va chaqiruvchi oddiy yuklab olishga
  qaytadi.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "RemuxResult",
    "cache_path_for",
    "probe_duration",
    "remux_to_mp4",
]

#: Sukut chegaralar (env orqali o'zgartiriladi).
DEFAULT_MAX_DURATION = 600.0                     # 10 daqiqa
DEFAULT_MAX_SIZE = 500 * 1024 * 1024             # 500 MB


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class RemuxResult:
    """Natija: ``ok=False`` bo'lsa ``reason`` sababni bildiradi."""

    ok: bool
    source: Path
    output: Path | None = None
    reason: str | None = None
    message: str = ""


def _cache_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("MEDIA_CACHE_DIR", "/var/lib/bigcloud/media")
    return Path(base).expanduser() / "remux"


def cache_path_for(source: str | os.PathLike[str], cache_dir: str | os.PathLike[str] | None = None) -> Path:
    """Kesh yo'li: nom + hajm (fayl o'zgarsa kesh yangilanadi)."""
    path = Path(source)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return _cache_root(cache_dir) / f"{path.name}.{size}.mp4"


def probe_duration(
    source: str | os.PathLike[str], *, runner: Any = subprocess.run
) -> float | None:
    """``ffprobe`` orqali davomiylikni soniyada olish (bo'lmasa ``None``)."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = runner(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    try:
        return float((result.stdout or "").strip())
    except (TypeError, ValueError):
        return None


def remux_to_mp4(
    source: str | os.PathLike[str],
    *,
    cache_dir: str | os.PathLike[str] | None = None,
    max_duration: float | None = None,
    max_size: int | None = None,
    force: bool = False,
    runner: Any = subprocess.run,
) -> RemuxResult:
    """``source`` ni mp4 konteynerga remux qilib keshga yozish.

    ``-c copy`` ishlatiladi — qayta kodlash yo'q (tez va sifatsiz yo'qotishsiz).
    """
    path = Path(source)
    limit_size = int(max_size if max_size is not None else _env_int("REMUX_MAX_SIZE", DEFAULT_MAX_SIZE))
    limit_duration = float(
        max_duration
        if max_duration is not None
        else _env_float("REMUX_MAX_DURATION", DEFAULT_MAX_DURATION)
    )

    if not path.is_file():
        return RemuxResult(False, path, reason="missing", message=f"Fayl topilmadi: {path}")

    try:
        size = path.stat().st_size
    except OSError as exc:  # pragma: no cover - fayl tizimi
        return RemuxResult(False, path, reason="unreadable", message=str(exc))

    if limit_size and size > limit_size:
        return RemuxResult(
            False,
            path,
            reason="too_large",
            message=f"Hajm chegaradan katta ({size} > {limit_size})",
        )

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return RemuxResult(False, path, reason="ffmpeg_missing", message="ffmpeg o'rnatilmagan")

    output = cache_path_for(path, cache_dir)
    if output.is_file() and not force:
        try:
            if output.stat().st_mtime >= path.stat().st_mtime:
                return RemuxResult(
                    True, path, output=output, reason="cached", message=f"Keshdan: {output}"
                )
        except OSError:  # pragma: no cover
            pass

    duration = probe_duration(path, runner=runner)
    if duration is not None and limit_duration and duration > limit_duration:
        return RemuxResult(
            False,
            path,
            reason="too_long",
            message=f"Davomiylik chegaradan katta ({duration:.0f}s > {limit_duration:.0f}s)",
        )

    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover
        return RemuxResult(False, path, reason="cache_unwritable", message=str(exc))

    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".mp4", dir=str(output.parent))
    os.close(tmp_fd)
    command: Sequence[str] = [
        ffmpeg,
        "-y",
        "-i",
        str(path),
        "-c",
        "copy",
        "-t",
        f"{limit_duration:.3f}",
        "-movflags",
        "+faststart",
        tmp_name,
    ]
    try:
        result = runner(list(command), capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        Path(tmp_name).unlink(missing_ok=True)
        return RemuxResult(False, path, reason="ffmpeg_failed", message=str(exc))

    if getattr(result, "returncode", 1) != 0:
        Path(tmp_name).unlink(missing_ok=True)
        tail = (getattr(result, "stderr", "") or "").strip().splitlines()[-3:]
        return RemuxResult(
            False,
            path,
            reason="ffmpeg_failed",
            message="ffmpeg xatosi: " + " | ".join(tail),
        )

    try:
        os.replace(tmp_name, output)
    except OSError as exc:  # pragma: no cover
        Path(tmp_name).unlink(missing_ok=True)
        return RemuxResult(False, path, reason="cache_unwritable", message=str(exc))

    logger.info("Remuxed %s -> %s", path, output)
    return RemuxResult(True, path, output=output, reason="remuxed", message=f"Tayyor: {output}")
