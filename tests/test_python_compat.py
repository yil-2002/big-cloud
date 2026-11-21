"""🐍 Python versiyasi mosligi: kod Docker obrazidagi interpreter'da ishlashi kerak.

Nega bu test muhim
------------------
``Dockerfile`` ``python:3.11-slim`` dan foydalanadi, lekin ishlab chiqish
muhiti 3.13 edi. Python 3.12 **f-string qoidalarini** o'zgartirdi: 3.12'dan
boshlab f-string ichida tashqi tirnoqni takrorlash va backslash ishlatish
mumkin. 3.11'da bu **SyntaxError** — ya'ni kod lokal ishlaydi-yu, obrazda
butunlay yiqiladi. Bu xatolik real ravishda ``bot/texts_uz.py`` da 5 joyda
uchragan va startup'da butun bot'ni o'chirgan bo'lardi.

Test ``ruff`` ni ``--target-version py311`` bilan ishga tushiradi (ruff
o'rnatilmagan bo'lsa — test o'tkazib yuboriladi, chunki bu faqat dev vositasi).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _dockerfile_python_version() -> tuple[int, int]:
    """``Dockerfile`` dagi ``FROM python:X.Y-...`` satridan versiyani olish."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"^FROM\s+python:(\d+)\.(\d+)", dockerfile, re.MULTILINE)
    assert match, "Dockerfile'da `FROM python:X.Y` topilmadi"
    return int(match.group(1)), int(match.group(2))


def _ruff_target_version() -> str:
    """``pyproject.toml`` dagi ``target-version`` qiymati (masalan ``py311``)."""
    with (ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    return str(config["tool"]["ruff"]["target-version"])


def test_dockerfile_and_ruff_target_agree() -> None:
    """Konfiguratsiya va obraz bir xil Python versiyasini nazarda tutishi kerak."""
    major, minor = _dockerfile_python_version()
    target = _ruff_target_version()
    assert target == f"py{major}{minor}", (
        f"Dockerfile python:{major}.{minor} ishlatadi, lekin ruff target-version={target} — "
        "ikkisi bir xil bo'lishi kerak, aks holda lint noto'g'ri qoidalarni tekshiradi"
    )


def test_no_syntax_requires_newer_python_than_image() -> None:
    """Asosiy tekshiruv: hech bir fayl obrazdagi Python'dan yangi sintaksis talab qilmasin."""
    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff o'rnatilmagan (dev bog'liqlik): pip install -r requirements-dev.txt")

    major, minor = _dockerfile_python_version()
    result = subprocess.run(  # o'z dev-vositamiz, argumentlar qat'iy (shell=False)
        [
            ruff,
            "check",
            ".",
            "--select",
            "ALL",
            "--target-version",
            f"py{major}{minor}",
            "--output-format",
            "concise",
            "--no-cache",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,  # ruff xato kodini o'zimiz tahlil qilamiz (stdout kerak)
    )

    offenders = [
        line
        for line in result.stdout.splitlines()
        if "invalid-syntax" in line or "Cannot use" in line or "Cannot reuse" in line
    ]
    assert not offenders, (
        f"python:{major}.{minor} da ishlamaydigan sintaksis topildi "
        f"(Docker obrazida SyntaxError bo'ladi):\n" + "\n".join(offenders[:20])
    )


def test_no_backslash_or_repeated_quotes_inside_fstring_expressions() -> None:
    """Zaxira tekshiruv (ruff bo'lmasa ham ishlaydi): f-string ifodalari 3.11'ga mos.

    Python 3.11'da f-string **ifodasi** ichida:

    * backslash (``\\'``) ishlatib bo'lmaydi;
    * tashqi tirnoq bilan bir xil tirnoq (``f"{d("k")}"``) ishlatib bo'lmaydi.

    Bu test shu ikki shaklni matn darajasida izlaydi — ruff o'rnatilmagan
    muhitlarda ham kafolat beradi.
    """
    pattern_backslash = re.compile(r"""f(?P<quote>["'])(?P<body>[^\n]*?(?P=quote))""")
    problems: list[str] = []

    for path in sorted(ROOT.rglob("*.py")):
        if any(part in {"__pycache__", ".venv", "venv", "old-bot", "dist", "data"} for part in path.parts):
            continue
        source = path.read_text(encoding="utf-8")
        for number, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for match in pattern_backslash.finditer(line):
                body = match.group("body")
                # f-string ichida qavslar orasidagi qismni ajratamiz.
                for expression in re.findall(r"\{([^{}]*)\}", body):
                    if "\\" in expression:
                        problems.append(
                            f"{path.relative_to(ROOT)}:{number} f-string ifodasida backslash: {{{expression}}}"
                        )
                    quote = match.group("quote")
                    if quote in expression:
                        problems.append(
                            f"{path.relative_to(ROOT)}:{number} f-string ifodasida tashqi tirnoq takrorlandi: "
                            f"{{{expression}}}"
                        )

    assert not problems, (
        "Python 3.11 bilan mos kelmaydigan f-string'lar:\n" + "\n".join(problems[:20])
    )


def test_requirements_do_not_pin_unsupported_python() -> None:
    """``python_requires``/classifier'lar (agar bo'lsa) obraz versiyasiga zid kelmasligi kerak."""
    with (ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)

    project = config.get("project", {})
    requires_python = project.get("requires-python")
    if requires_python is None:
        return  # majburiy emas

    major, minor = _dockerfile_python_version()
    match = re.search(r">=\s*(\d+)\.(\d+)", requires_python)
    if match:
        assert (int(match.group(1)), int(match.group(2))) <= (major, minor), (
            f"requires-python={requires_python} lekin obraz python:{major}.{minor}"
        )


def test_readme_documents_the_supported_python() -> None:
    """README'da e'lon qilingan versiya obraz versiyasidan kichik bo'lmasligi kerak."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    major, minor = _dockerfile_python_version()
    mentioned = re.findall(r"Python (\d+)\.(\d+)", readme)
    assert mentioned, "README'da Python versiyasi ko'rsatilmagan"
    lowest = min((int(a), int(b)) for a, b in mentioned)
    assert lowest <= (major, minor), (
        f"README eng kam {lowest[0]}.{lowest[1]} ni va'da qiladi, obraz esa python:{major}.{minor}"
    )
