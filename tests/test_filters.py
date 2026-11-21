"""🧲 aiogram filter regressiyasi: `Command(...) | F.text.in_(...)` — TAQIQLANADI.

Nima uchun bu test bor
----------------------
aiogram 3.31'da ``BaseFilter`` (``Command``, ``CommandStart``, ``StateFilter``)
va ``MagicFilter`` (``F.text...``) ni ``|``/``&`` bilan aralashtirish **runtime
xatosi** beradi::

    TypeError: unsupported operand type(s) for |: 'Command' and 'bool'

Xato faqat update keliganda ko'rinadi (import vaqti yig'ilishida emas),
shuning uchun oddiy ``compileall``/import tekshiruvi uni ushlamaydi. To'g'ri
yozuv — :func:`aiogram.filters.logic.or_f` / :func:`and_f`.

Bu test **barcha** handler fayllarini AST orqali ko'zdan kechiradi: shu xato
yana kiritilsa, test darhol yiqiladi va qaysi fayl/qator ekanini aytadi.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HANDLER_FILES = sorted((ROOT / "bot" / "handlers").glob("*.py"))
MIDDLEWARE_FILES = sorted((ROOT / "bot" / "middlewares").glob("*.py"))

#: ``BaseFilter`` sinflari — bular ``F.*`` bilan ``|``/``&`` qilinmasligi kerak.
BASE_FILTERS = {
    "Command", "CommandObject", "CommandStart", "StateFilter",
    "ChatTypeFilter", "ContentTypeFilter", "ExternalReplyFilter",
    "IsReplyFilter", "AndFilter", "OrFilter", "BaseFilter",
}


def _is_base_filter_call(node: ast.AST) -> bool:
    """``Command("x")`` kabi BaseFilter chaqiruvimi?"""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name in BASE_FILTERS


def _is_magic_filter(node: ast.AST) -> bool:
    """``F.text.in_(...)`` / ``~F.data`` kabi MagicFilter ifodasimi?"""
    if isinstance(node, ast.Attribute):
        root = node
        while isinstance(root, ast.Attribute):
            root = root.value
        return isinstance(root, ast.Name) and root.id == "F"
    if isinstance(node, ast.Call):
        return _is_magic_filter(node.func)
    if isinstance(node, ast.UnaryOp):  # ~F.data
        return _is_magic_filter(node.operand)
    if isinstance(node, ast.BoolOp):   # and/or
        return any(_is_magic_filter(value) for value in node.values)
    if isinstance(node, ast.BinOp):
        return _is_magic_filter(node.left) or _is_magic_filter(node.right)
    return False


def _mixed_filter_operations(tree: ast.AST) -> list[tuple[int, str]]:
    """Aralash (BaseFilter ⨯ MagicFilter) ``BinOp`` larni topish."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp):
            continue
        if not isinstance(node.op, (ast.BitOr, ast.BitAnd)):
            continue
        left, right = node.left, node.right
        mixed = (_is_base_filter_call(left) and _is_magic_filter(right)) or (
            _is_magic_filter(left) and _is_base_filter_call(right)
        )
        if mixed:
            found.append((node.lineno, ast.unparse(node)[:100]))
    return found


@pytest.mark.parametrize("path", HANDLER_FILES + MIDDLEWARE_FILES, ids=lambda p: p.name)
def test_no_mixed_filter_operators(path: Path) -> None:
    """Hech bir handler BaseFilter va MagicFilter'ni ``|``/``&`` bilan aralashtirmaydi."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems = _mixed_filter_operations(tree)
    assert not problems, (
        f"{path.name}: aralash filtr topildi — or_f()/and_f() ishlatilishi kerak:\n  "
        + "\n  ".join(f"qator {line}: {snippet}" for line, snippet in problems)
    )


def test_or_f_is_used_where_needed() -> None:
    """Buyruq + tugma matni kombinatsiyasi ``or_f`` orqali yozilgan."""
    commands_path = ROOT / "bot" / "handlers" / "commands.py"
    source = commands_path.read_text(encoding="utf-8")
    assert "from aiogram.filters.logic import or_f" in source
    # Legacy tugma matnlari bilan birga keladigan buyruqlar
    for command in ("storage", "search", "stats", "share", "settings", "help"):
        assert f'or_f(Command("{command}")' in source, command


def test_pure_magic_filters_are_still_allowed() -> None:
    """Sof MagicFilter ifodalari (``F.text & ~F.text.startswith``) ruxsat etilgan."""
    upload = (ROOT / "bot" / "handlers" / "upload.py").read_text(encoding="utf-8")
    tree = ast.parse(upload)
    assert _mixed_filter_operations(tree) == []
    assert "F.text" in upload
