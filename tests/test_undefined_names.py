"""🔎 Aniqlanmagan nomlar (``NameError``) bo'yicha statik himoya.

Nega kerak
----------
Ikki real production xatosi aynan shu sinfdan chiqdi:

1. ``services/telegram.py`` — ``File.sanitize_filename``/``File.detect_category``
   runtime'da chaqirilardi, lekin ``File`` modul darajasida **import qilinmagan**
   edi → har qanday hujjat/rasm yuklashda ``NameError``.
2. ``bot/handlers/start.py`` — ``_open_share_token`` ichida ``state`` ishlatilgan,
   ammo funksiya parametri emas edi → himoyalangan havola oqimi ``NameError``.

Ikkalasi ham testlarda ko'rinmagan (mos yo'l chaqirilmagani uchun). Bu fayl
**barcha** modul va funksiyalarni skaner qilib, lokal bo'lmagan har bir nom
modul darajasida yoki ``builtins`` da mavjudligini tekshiradi — ya'ni xuddi
shu sinfdagi xato qayta kiritilsa, test yiqiladi.

Tashqi vosita talab qilinmaydi: ``ast`` + ``symtable`` (stdlib).
"""

from __future__ import annotations

import ast
import builtins
import keyword
import symtable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

#: Tekshiriladigan paketlar/fayllar (testlar va migratsiya skriptlari ham).
SCAN_TARGETS: tuple[str, ...] = (
    "api",
    "bot",
    "db",
    "services",
    "scripts",
    "config.py",
    "main.py",
)

#: Ko'rib chiqilmaydigan yo'l qismlari.
SKIPPED_PARTS: frozenset[str] = frozenset({"__pycache__", ".venv", "venv"})


def _python_files() -> list[Path]:
    """Skanerlanadigan barcha ``.py`` fayllar."""
    files: list[Path] = []
    for target in SCAN_TARGETS:
        path = ROOT / target
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(p for p in path.rglob("*.py") if not SKIPPED_PARTS.intersection(p.parts)))
    return files


def _module_names(tree: ast.Module) -> set[str]:
    """Modul darajasida bog'langan barcha nomlar (import, def, class, assign …)."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    continue
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            # Dekoratorlar/annotatsiyalar ichidagi nomlar ham modul darajasida.
            names.update(_bound_in_body(node))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_target_names(target))
        elif isinstance(node, ast.AnnAssign):
            if node.target is not None:
                names.update(_target_names(node.target))
        elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
            names.update(_bound_in_body(node))
        elif isinstance(node, ast.AugAssign):
            names.update(_target_names(node.target))
    return names


def _target_names(target: ast.expr) -> set[str]:
    """Turli assign-shakllaridan nomlarni chiqarish (tuple/starred unpacking ham)."""
    names: set[str] = set()
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            names.update(_target_names(element))
    elif isinstance(target, ast.Starred):
        names.update(_target_names(target.value))
    return names


def _bound_in_body(node: ast.AST) -> set[str]:
    """Ichki blok (if/try/with/for) ichida modul darajasida bog'langan nomlar."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, (ast.Import, ast.ImportFrom)):
            for alias in child.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(child, ast.Assign):
            for target in child.targets:
                names.update(_target_names(target))
        elif isinstance(child, ast.AnnAssign):
            names.update(_target_names(child.target))
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(child.name)
        elif isinstance(child, (ast.For, ast.AsyncFor)):
            names.update(_target_names(child.target))
        elif isinstance(child, (ast.With, ast.AsyncWith)):
            for item in child.items:
                if item.optional_vars is not None:
                    names.update(_target_names(item.optional_vars))
        elif isinstance(child, ast.ExceptHandler) and child.name:
            names.add(child.name)
        elif isinstance(child, (ast.NamedExpr,)):
            names.update(_target_names(child.target))
    return names


def _assigned_globals(table: symtable.SymbolTable) -> set[str]:
    """``global X`` deb e'lon qilinib, **haqiqatan yoziladigan** nomlar.

    Muhim farq: ``symtable`` da *har qanday* global murojaat ``is_global()``
    bo'ladi (shu jumladan hech qayerda bog'lanmagan ``MissingThing`` kabi
    nomlar ham). Shu sababli faqat ``is_assigned()`` ham ``True`` bo'lganlarni
    olamiz — aks holda skaner barcha aniqlanmagan nomlarni "modul darajasida
    bor" deb hisoblab, hech narsani topmay qo'yadi.
    """
    declared: set[str] = set()
    for symbol in table.get_symbols():
        if symbol.is_global() and symbol.is_assigned():
            declared.add(symbol.get_name())
    return declared


def _walk_tables(table: symtable.SymbolTable) -> Any:
    """Barcha ichki jadvallarni (rekursiv) qaytarish."""
    yield table
    for child in table.get_children():
        yield from _walk_tables(child)


def _collect_undefined(path: Path) -> list[str]:
    """Bitta fayldagi aniqlanmagan nomlar: ``["satr: nom (funksiya)"]``."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
        top = symtable.symtable(source, str(path), "exec")
    except SyntaxError as exc:  # pragma: no cover - sintaksis xatosi compileall'da chiqadi
        return [f"{exc.lineno}: SyntaxError ({exc.msg})"]

    module_scope = _module_names(tree)
    # ``global x`` + yozish orqali keyinroq bog'lanadigan nomlar ham hisobga olinadi
    # (masalan ``main._language_installed``).
    for table in _walk_tables(top):
        module_scope |= _assigned_globals(table)

    builtin_names = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__", "__builtins__", "__spec__"}

    problems: list[str] = []

    def check(table: symtable.SymbolTable, inherited: set[str]) -> None:
        local_scope = inherited | {symbol.get_name() for symbol in table.get_symbols() if symbol.is_local()}
        # Parametrlar va ichki def/class nomlari ham lokal hisoblanadi.
        for symbol in table.get_symbols():
            if symbol.is_parameter() or symbol.is_local():
                local_scope.add(symbol.get_name())

        for symbol in table.get_symbols():
            name = symbol.get_name()
            if name in local_scope or name in module_scope or name in builtin_names:
                continue
            if symbol.is_free() and name in inherited:
                continue
            if keyword.iskeyword(name):
                continue
            if name.startswith("__") and name.endswith("__"):
                continue
            problems.append(f"{symbol.get_name()} ({table.get_name()}, {table.get_lineno() if hasattr(table, 'get_lineno') else '?'})")

        for child in table.get_children():
            child_scope = local_scope | {
                symbol.get_name() for symbol in table.get_symbols() if symbol.is_local()
            }
            check(child, child_scope)

    for child in top.get_children():
        check(child, module_scope | builtin_names)

    return sorted(set(problems))


# --------------------------------------------------------------------------- #
# Testlar
# --------------------------------------------------------------------------- #
def test_scanner_covers_whole_project() -> None:
    """Skaner kamida 60 ta faylni ko'rib chiqishi kerak (aks holda u bo'sh)."""
    files = _python_files()
    assert len(files) >= 60, f"atigi {len(files)} fayl topildi"
    assert any(part.name == "main.py" for part in files)
    assert any(part.name == "telegram.py" for part in files)


def test_no_undefined_names_in_project() -> None:
    """Asosiy shart: hech bir funksiya aniqlanmagan nomga tayanmasin."""
    findings: dict[str, list[str]] = {}
    for path in _python_files():
        problems = _collect_undefined(path)
        if problems:
            findings[str(path.relative_to(ROOT))] = problems

    assert not findings, (
        "Aniqlanmagan nomlar topildi (runtime NameError xavfi):\n"
        + "\n".join(f"  {file}: {', '.join(items[:6])}" for file, items in sorted(findings.items())[:25])
    )


def test_scanner_detects_injected_bug(tmp_path: Path) -> None:
    """Skanerning o'zi ishlayotganini tekshirish: ataylab xato fayl."""
    broken = tmp_path / "broken_module.py"
    broken.write_text(
        """
import os


def uses_missing_name(value):
    return MissingThing(value) + os.sep


async def uses_missing_state(message):
    await start_flow(message, state)
""",
        encoding="utf-8",
    )
    problems = _collect_undefined(broken)
    joined = " ".join(problems)
    assert "MissingThing" in joined, problems
    assert "state" in joined, problems
    assert "start_flow" in joined, problems


def test_scanner_allows_valid_patterns(tmp_path: Path) -> None:
    """To'g'ri yozilgan kod noto'g'ri flaglanmasligi kerak (false positive yo'q)."""
    good = tmp_path / "good_module.py"
    good.write_text(
        """
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from db.models.file import File

logger = logging.getLogger(__name__)
CONSTANT = 1


def outer():
    captured = 42

    def inner():
        global CONSTANT
        CONSTANT = 2
        return captured + len("abc")

    return inner


class Handler:
    def method(self, value: Any) -> Any:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            logger.warning("%s", exc)
            return None


async def comprehension():
    return [item async for item in _aiter()]


async def _aiter():
    for index in range(3):
        yield index
""",
        encoding="utf-8",
    )
    problems = _collect_undefined(good)
    assert problems == [], problems


def test_known_bugs_are_fixed() -> None:
    """Ikkala tarixiy ``NameError`` ham qayta paydo bo'lmasligi kerak."""
    telegram_source = (ROOT / "services" / "telegram.py").read_text(encoding="utf-8")
    assert "from db.models.file import File" in telegram_source, (
        "File runtime importi yo'q — sanitize_filename/detect_category NameError beradi"
    )
    # TYPE_CHECKING bloki ichida bo'lib qolmasligi kerak (runtime'da ishlatiladi).
    assert "if TYPE_CHECKING:" not in telegram_source or "    from db.models.file import File" not in telegram_source

    start_source = (ROOT / "bot" / "handlers" / "start.py").read_text(encoding="utf-8")
    import inspect

    from bot.handlers.start import _open_share_token

    assert "state" in inspect.signature(_open_share_token).parameters
    # Chaqiruv joyida ham ``state`` uzatiladi.
    assert "_open_share_token(message, payload, user, storage, share, tg_storage, settings, state)" in start_source


def test_stream_module_imports_any() -> None:
    """``api/routes/stream.py`` annotatsiyalari ``Any`` ni talab qiladi."""
    source = (ROOT / "api" / "routes" / "stream.py").read_text(encoding="utf-8")
    assert "Any" in source.split("def ")[0], "modul boshida Any importi yo'q"
