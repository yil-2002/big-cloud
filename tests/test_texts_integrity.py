"""``bot.texts`` butunligi: ishlatilgan **har bir** matn funksiyasi mavjudmi?

Sinf haqida
-----------
``bot/texts.py`` — bazaviy (inglizcha) matnlar. ``APP_LANGUAGE=uz`` bo'lganda
``main.install_language()`` ikki qatlamni ustiga qo'yadi:

1. ``bot.texts_uz.install()`` — faqat **bazada allaqachon mavjud** nomlarni
   almashtiradi (``hasattr`` tekshiruvi bilan);
2. ``bot.texts_hugecloud.install()`` — yangi nomlarni (masalan
   ``auth_required_action``, ``login_prompt``) ``setattr`` orqali **qo'shadi**.

Demak, ``install_language`` chaqirilmasa, hugecloud matnlari ``bot.texts`` da
umuman bo'lmaydi va bot birinchi xabarda ``AttributeError`` bilan yiqiladi.
Bu xato avval production startup'da yuz bergan (testlar esa conftest tufayli
matnlarni oldindan o'rnatib qo'yгани uchun uni ko'rmagan).

Bu fayl ikki himoya qatlamini beradi:

* **Statik**: ``bot/`` (handlers, middlewares, keyboards) va ``main.py`` dagi
  har bir ``texts.<nomi>`` murojaati AST orqali yig'iladi va o'rnatilgan
  ``bot.texts`` da mavjudligi tekshiriladi;
* **Dinamik**: ``install_language("uz")`` dan keyin barcha UZ jadval nomlari va
  bazaviy funksiyalar chaqiriladigan (callable) holatda bo'lishi kerak.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: ``import bot.texts as texts`` ishlatadigan kataloglar.
SCANNED_DIRS: tuple[str, ...] = ("bot",)
#: Qo'shimcha skanerlanadigan fayllar.
SCANNED_FILES: tuple[str, ...] = ("main.py",)

#: Skanerlashda e'tibor berilmasligi kerak bo'lgan modullar (matn manbalari).
SKIPPED_MODULES: frozenset[str] = frozenset({"bot.texts", "bot.texts_uz", "bot.texts_hugecloud"})


# --------------------------------------------------------------------------- #
# AST yordamchilari
# --------------------------------------------------------------------------- #
def _python_files() -> list[Path]:
    """Skanerlanadigan barcha ``.py`` fayllar (testlar va kesh bundan mustasno)."""
    files: list[Path] = []
    for directory in SCANNED_DIRS:
        base = ROOT / directory
        files.extend(sorted(path for path in base.rglob("*.py") if "__pycache__" not in path.parts))
    for name in SCANNED_FILES:
        candidate = ROOT / name
        if candidate.exists():
            files.append(candidate)
    return files


def _module_name(path: Path) -> str:
    """Fayl yo'lidan modul nomini yasash (``bot/handlers/files.py`` -> ``bot.handlers.files``)."""
    relative = path.relative_to(ROOT).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join(parts)


def _texts_aliases(tree: ast.AST) -> set[str]:
    """``bot.texts`` ga bog'langan import taxalluslarini topish.

    Qo'llab-quvvatlanadigan shakllar::

        import bot.texts as texts
        from bot import texts
        import bot.texts
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "bot.texts":
                    aliases.add(alias.asname or "bot.texts")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "bot" and any(alias.name == "texts" for alias in node.names):
                for alias in node.names:
                    if alias.name == "texts":
                        aliases.add(alias.asname or "texts")
            elif node.module == "bot.texts":
                # ``from bot.texts import x`` — nomlar to'g'ridan-to'g'ri tekshiriladi.
                aliases.add("__from_import__")
    return aliases


def _collect_text_references(path: Path) -> list[tuple[str, int]]:
    """Fayldagi ``texts.<nomi>`` murojaatlarini (nom, satr) ko'rinishida yig'ish."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    aliases = _texts_aliases(tree)
    if not aliases:
        return []

    references: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        owner = node.value
        if isinstance(owner, ast.Name) and owner.id in aliases or isinstance(owner, ast.Attribute) and _dotted(owner) in aliases:
            references.append((node.attr, node.lineno))

    # ``from bot.texts import name`` shakli ham tekshiriladi.
    if "__from_import__" in aliases:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "bot.texts":
                for alias in node.names:
                    if alias.name != "*":
                        references.append((alias.name, node.lineno))
    return references


def _dotted(node: ast.Attribute) -> str:
    """``ast.Attribute`` zanjirini nuqta bilan bog'langan nomga aylantirish."""
    parts: list[str] = [node.attr]
    current: ast.expr = node.value
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


# --------------------------------------------------------------------------- #
# Testlar
# --------------------------------------------------------------------------- #
def test_scanner_actually_finds_references() -> None:
    """Skaner bo'sh natija qaytarmasligi kerak (aks holda test hech nimani tekshirmaydi)."""
    total = sum(len(_collect_text_references(path)) for path in _python_files())
    assert total > 100, f"atigi {total} ta matn murojaati topildi — skaner ishlamayapti"


def test_every_texts_reference_exists_after_language_install() -> None:
    """Asosiy shart: ishlatilgan har bir ``texts.X`` o'rnatilgandan keyin mavjud."""
    from bot import texts
    from bot.texts_hugecloud import install_language

    install_language("uz")

    missing: list[str] = []
    for path in _python_files():
        module = _module_name(path)
        if module in SKIPPED_MODULES:
            continue
        for name, lineno in _collect_text_references(path):
            if not hasattr(texts, name):
                missing.append(f"{module}:{lineno} -> texts.{name}")

    assert not missing, (
        "bot.texts da mavjud bo'lmagan matn funksiyalari ishlatilgan "
        "(AttributeError xavfi):\n" + "\n".join(missing[:40])
    )


def test_every_texts_reference_is_callable_or_constant() -> None:
    """Mavjud nom yo chaqiriladigan funksiya, yoki KATTA_HARFLI konstanta bo'lishi kerak.

    Konstantalarga ruxsat beriladi (masalan ``texts.CATEGORY_TITLES`` — kategoriya
    sarlavhalari lug'ati), chunki ular ham ``install()`` orqali to'ldiriladi.
    Kichik harf bilan boshlanadigan nom esa **funksiya** bo'lishi shart — aks
    holda handler ``texts.foo()`` chaqiruvida ``TypeError`` oladi.
    """
    from bot import texts
    from bot.texts_hugecloud import install_language

    install_language("uz")

    problems: list[str] = []
    for path in _python_files():
        module = _module_name(path)
        if module in SKIPPED_MODULES:
            continue
        for name, lineno in _collect_text_references(path):
            attribute = getattr(texts, name, None)
            if attribute is None:
                continue  # mavjudlik boshqa testda tekshiriladi
            is_constant = name.isupper() or name.startswith(tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")) and name.isupper()
            if callable(attribute) or is_constant:
                continue
            problems.append(f"{module}:{lineno} -> texts.{name} ({type(attribute).__name__})")

    assert not problems, (
        "chaqiriladigan bo'lmagan (va konstanta ham emas) matn nomlari:\n" + "\n".join(problems[:20])
    )


def test_all_uz_table_names_are_installed() -> None:
    """``UZ`` jadvalidagi har bir nom ``bot.texts`` ga o'rnatilishi shart."""
    from bot import texts
    from bot.texts_hugecloud import UZ, install_language

    install_language("uz")

    absent = sorted(name for name in UZ if not hasattr(texts, name))
    assert not absent, f"UZ jadvalidan o'rnatilmagan nomlar: {absent}"


def test_uz_overrides_base_strings() -> None:
    """O'zbekcha qatlam haqiqatan bazani almashtiradi (UI tili = uz)."""
    from bot import texts
    from bot.texts_hugecloud import install_language

    install_language("uz")

    # Hugecloud qatlami: UZ funksiyasi bilan bir xil obyekt bo'lishi kerak.
    from bot.texts_hugecloud import UZ

    for name in ("auth_gate_title", "auth_required_action"):
        assert getattr(texts, name) is UZ[name], f"{name} o'zbekcha funksiyaga almashtirilmagan"


def test_legacy_uz_names_replace_existing_english() -> None:
    """``texts_uz.install()`` faqat mavjud nomlarni almashtiradi — yangi nom qo'shmaydi."""
    import bot.texts_uz as uz
    from bot import texts
    from bot.texts_hugecloud import install_language

    install_language("uz")

    replaced = [name for name in uz.translations if getattr(texts, name, None) is uz.translations[name]]
    # Kamida bir qismi haqiqatan almashtirilgan bo'lishi kerak.
    assert len(replaced) >= 30, f"atigi {len(replaced)} ta legacy matn almashtirildi"


def test_missing_keys_reports_nothing_for_uz() -> None:
    """``UZ`` jadvali ``EN`` bilan bir xil to'plamga ega (tarjima to'liq)."""
    from bot.texts_hugecloud import missing_keys

    assert missing_keys() == [], "UZ jadvalida yetishmayotgan kalitlar bor"


def test_main_calls_install_language_before_build() -> None:
    """``amain`` bot'ni qurishdan **oldin** tilni o'rnatishi kerak (tartib muhim)."""
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    ensure_pos = source.index("ensure_language(settings)")
    build_pos = source.index("build_bot(settings)")
    assert ensure_pos < build_pos, "til o'rnatish bot qurilishidan keyin keladi"

    # main() sinxron yo'lida ham o'rnatiladi.
    assert "install_language(settings)  # UI matnlari" in source


def test_install_language_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ensure_language`` ikki marta chaqirilsa ham matnlar bir marta o'rnatiladi."""
    import main as main_module
    from config import get_settings

    calls: list[str] = []

    def fake_install(language: str = "uz") -> dict[str, Any]:
        calls.append(language)
        return {"legacy": 0, "hugecloud": 0}

    monkeypatch.setattr(main_module, "_language_installed", False)
    monkeypatch.setattr("bot.texts_hugecloud.install_language", fake_install)

    settings = get_settings()
    assert main_module.ensure_language(settings) is True    # birinchi chaqiruv o'rnatadi
    assert main_module.ensure_language(settings) is False   # ikkinchisi hech narsa qilmaydi
    main_module.install_language(settings)                  # idempotent
    assert calls == [settings.app.language], f"kutilmagan chaqiruvlar: {calls}"


def test_force_reinstall_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """``force=True`` bilan qayta o'rnatish mumkin (tilni almashtirish uchun)."""
    import main as main_module
    from config import get_settings

    calls: list[str] = []
    monkeypatch.setattr(
        "bot.texts_hugecloud.install_language",
        lambda language="uz": calls.append(language) or {"legacy": 0, "hugecloud": 0},
    )
    monkeypatch.setattr(main_module, "_language_installed", True)
    main_module.install_language(get_settings())            # o'rnatilgan -> hech narsa
    assert calls == []
    main_module.install_language(get_settings(), force=True)
    assert len(calls) == 1


def test_no_handler_uses_undefined_text_at_import_time() -> None:
    """Modul darajasida (import paytida) matn chaqiruvi bo'lmasligi kerak.

    Import paytida ``texts.x()`` chaqirilsa, til o'rnatilishidan **oldin**
    bajariladi va yana ``AttributeError`` beradi. Faqat funksiya/guard ichida
    chaqirish xavfsiz.
    """
    offenders: list[str] = []
    for path in _python_files():
        module = _module_name(path)
        if module in SKIPPED_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = _texts_aliases(tree)
        if not aliases:
            continue

        # Modul darajasidagi ifodalarni yig'amiz (funksiya/sinf tanasidan tashqari).
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id in aliases
                ):
                    offenders.append(f"{module}:{sub.lineno} -> texts.{sub.func.attr}()")

    assert not offenders, (
        "Modul darajasida matn chaqiruvi topildi (import paytida bajariladi va "
        "til o'rnatilishidan oldin yiqiladi):\n" + "\n".join(offenders[:20])
    )


def test_texts_module_exposes_all_installed_names() -> None:
    """``bot.texts.__all__`` o'rnatilgan nomlarni o'z ichiga oladi."""
    from bot import texts
    from bot.texts_hugecloud import UZ, install_language

    install_language("uz")
    exported = set(getattr(texts, "__all__", []))
    absent = sorted(name for name in UZ if name not in exported)
    assert not absent, f"__all__ da yo'q: {absent}"


def test_describe_installation_is_stable() -> None:
    """``inspect`` orqali tekshirish: matn funksiyalari haqiqiy funksiyalar (str emas)."""
    from bot import texts
    from bot.texts_hugecloud import install_language

    install_language("uz")
    for name in ("auth_gate_title", "auth_required_action", "login_prompt"):
        function = getattr(texts, name, None)
        assert function is not None, f"texts.{name} yo'q"
        assert inspect.isfunction(function) or callable(function), f"texts.{name} chaqirilmaydi"


# --------------------------------------------------------------------------- #
# Dinamik butunlik: **har bir** matn ikki tilda ham render bo'ladimi?
# --------------------------------------------------------------------------- #
# Nega kerak: ``EN``/``UZ`` yozuvlari lambda — nomlar chaqiruv paytida izlanadi.
# Shu sababli yordamchi funksiya (masalan ``_duration``) yozib qo'yilmasa,
# modul import qilinadi, testlar "mavjud" deb o'tadi, lekin production'da
# birinchi chaqiruvda ``NameError`` chiqadi. Bu test buni oldindan ushlaydi.
class _ProbeUser:
    """``admin_user_card`` kabi matnlar uchun soxta ``User`` qatori."""

    telegram_id = 1
    login = "ali"
    username = "ali"
    display_name = "Ali"
    first_name = "Ali"
    is_authenticated = True
    is_blocked = False
    is_premium = False
    storage_used = 2048
    quota_bytes = 0
    last_login_at = None
    last_seen_at = None


class _ProbeEntry:
    """Audit yozuvini eslatuvchi obyekt."""

    success = True
    action = "login"
    actor_id = 7
    detail = "batafsil"
    created_at = None


_PROBE_STATS: dict[str, Any] = {
    "users": {"total": 3, "authenticated": 2, "with_account": 2, "blocked": 1, "premium": 0},
    "files": {
        "total": 5,
        "size": 2048,
        "pinned": 1,
        "public": 2,
        "archived": 1,
        "pending_archive": 4,
    },
    "folders": 2,
    "shared_links": 3,
    "active_sessions": 1,
    "active_window_hours": 24,
    "by_category": {"media": {"count": 2, "size": 1024}},
    "top_users": [
        {"telegram_id": 1, "name": "Ali", "login": "ali", "files": 2, "size": 1024, "blocked": False}
    ],
}
_PROBE_COUNTS: dict[str, int] = {"media": 2, "audio": 1, "document": 3, "archive": 1, "apps": 1}


def _probe_args(function: Any, key: str = "") -> dict[str, Any]:
    """Lambda imzosidan "o'rtacha" argumentlar to'plamini yasash.

    :param key: matn kaliti — bir xil parametr nomi turli ma'noda kelganda
        (masalan ``rows`` audit sahifasida yozuvlar, foydalanuvchilar
        sahifasida ``User`` qatorlari) to'g'ri probe'ni tanlash uchun.
    """
    arguments: dict[str, Any] = {}
    for name, parameter in inspect.signature(function).parameters.items():
        default = parameter.default
        if default is not inspect.Parameter.empty:
            arguments[name] = True if default is False else default
            continue
        lowered = name.lower()
        if "second" in lowered or "minute" in lowered:
            arguments[name] = 90
        elif lowered in {"stats", "counts", "rows", "user", "entry", "label"}:
            if lowered == "rows":
                arguments[name] = [_ProbeEntry()] if "audit" in key else [_ProbeUser()]
            else:
                arguments[name] = {
                    "stats": _PROBE_STATS,
                    "counts": _PROBE_COUNTS,
                    "user": _ProbeUser(),
                    "entry": _ProbeEntry(),
                    "label": None,
                }[lowered]
        elif lowered in {"used", "size", "quota", "total", "bytes"}:
            arguments[name] = 2048
        elif lowered in {"page", "pages", "count", "left", "limit", "file_count"}:
            arguments[name] = 2
        else:
            arguments[name] = "X"
    return arguments


@pytest.mark.parametrize("language_name", ["EN", "UZ"])
def test_every_text_renders_in_both_languages(language_name: str) -> None:
    """Ikki til jadvalining **barcha** yozuvlari istisnosiz render bo'ladi."""
    import bot.texts_hugecloud as bc

    table = getattr(bc, language_name)
    broken: list[str] = []
    for key, entry in table.items():
        if isinstance(entry, str):
            continue
        try:
            rendered = entry(**_probe_args(entry, key))
        except Exception as error:
            broken.append(f"{key}: {type(error).__name__}: {error}")
            continue
        assert isinstance(rendered, str) and rendered, key
    assert not broken, f"{language_name} render xatolari: {broken}"


#: Tilga bog'liq bo'lmagan yozuvlar (brend/emoji belgilari).
_LANGUAGE_NEUTRAL_KEYS: frozenset[str] = frozenset({"group_apps"})


def test_en_and_uz_tables_are_actually_translated() -> None:
    """EN va UZ bir xil matn bo'lib qolmasin (tarjima qatlami yo'qolmasin).

    Bu test avval real xatoni ushladi: yangi UI bloklari EN jadvaliga
    o'zbekcha nusxalanib qolgan edi — inglizcha rejimda ham o'zbekcha chiqardi.
    """
    import bot.texts_hugecloud as bc

    duplicates: list[str] = []
    for key in bc.EN:
        if key in _LANGUAGE_NEUTRAL_KEYS:
            continue
        english, uzbek = bc.EN[key], bc.UZ[key]
        if isinstance(english, str) or isinstance(uzbek, str):
            if english == uzbek:
                duplicates.append(key)
            continue
        try:
            if english(**_probe_args(english, key)) == uzbek(**_probe_args(uzbek, key)):
                duplicates.append(key)
        except Exception as error:  # render testi buni alohida ushlaydi
            duplicates.append(f"{key}: {type(error).__name__}")
    assert not duplicates, f"Tarjimasi bir xil qolgan kalitlar: {duplicates}"


def test_gate_texts_accept_parked_flag() -> None:
    """Deep-link park xabari ikki holatda ham (yangi/qaytgan) ko'rsatiladi."""
    import bot.texts_hugecloud as bc

    for table in (bc.EN, bc.UZ):
        parameters = inspect.signature(table["gate_new_session"]).parameters
        assert "parked" in parameters
        parked_text = table["gate_new_session"]("bot", "https://example.com", True)
        plain_text = table["gate_new_session"]("bot", "https://example.com", False)
        assert len(parked_text) > len(plain_text)

        returning = inspect.signature(table["gate_returning_session"]).parameters
        assert "parked" in returning
