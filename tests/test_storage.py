"""☁️ Saqlash yadrosi: cheksiz kvota, file_unique_id dedup, saralash, guruhlar."""

from __future__ import annotations

import datetime as _dt

import pytest

from db.models import FILE_GROUPS, FileCategory
from services.exceptions import InsufficientQuotaError, TooLargeError
from services.storage import StorageService
from tests.conftest import make_file


# --------------------------------------------------------------------------- #
# Kvota: 0 = "Cheksiz (Unlimited)"
# --------------------------------------------------------------------------- #
async def test_quota_zero_means_unlimited(storage: StorageService, user) -> None:
    """``STORAGE_QUOTA=0`` -> cheksiz; kvota hisoblanmaydi va to'sib qo'ymaydi."""
    assert storage.default_quota == 0
    assert storage.quota_for(user) == 0
    assert user.quota_is_unlimited(0) is True
    assert storage.quota_label(user) == "Cheksiz (Unlimited)"

    # 1 GB fayl ham muammosiz o'tadi (cheksiz rejimda joy tekshirilmaydi).
    record = await make_file(storage, user.telegram_id, name="katta.zip", size=1024 ** 3)
    assert record.id is not None


async def test_quota_enforced_when_configured(storage: StorageService, settings, user) -> None:
    """Kvota berilganda (0 emas) ``ensure_fits`` istisno qaytaradi."""
    await storage.set_user_quota(user.telegram_id, 2048)     # atigi 2 KB
    await storage.recalculate_storage(user.telegram_id)

    user.storage_used = 1024
    assert user.has_space(1024, 2048) is True
    assert user.has_space(2048, 2048) is False
    with pytest.raises(InsufficientQuotaError):
        storage.ensure_fits(user, 4096)

    assert storage.quota_label(user) != "Cheksiz (Unlimited)"


async def test_max_file_size_enforced(storage: StorageService, user) -> None:
    """``MAX_FILE_SIZE`` dan katta fayl rad etiladi (Telegram cheklovi bilan bog'liq)."""
    storage.settings.storage.max_file_size = 1024
    with pytest.raises(TooLargeError):
        storage.ensure_fits(user, 2048)


# --------------------------------------------------------------------------- #
# Deduplikatsiya (file_unique_id)
# --------------------------------------------------------------------------- #
async def test_duplicate_upload_reuses_metadata(storage: StorageService, user) -> None:
    """Bir xil ``file_unique_id`` ikkinchi marta yuklansa — YANGI fayl yaratilmaydi."""
    first = await storage.add_file(
        telegram_id=user.telegram_id,
        file_id="AgAD-AAA",
        file_unique_id="uniq-shared",
        file_name="video.mp4",
        file_size=5000,
    )
    assert first.is_new is True

    second = await storage.add_file(
        telegram_id=user.telegram_id,
        file_id="AgAD-BBB",             # Telegram boshqa file_id bersa ham
        file_unique_id="uniq-shared",   # unique_id bir xil -> dedup
        file_name="video.mp4",
        file_size=5000,
    )
    assert second.is_new is False
    assert second.deduplicated is True
    assert second.record.id == first.record.id

    items, total = await storage.list_files(user.telegram_id)
    assert total == 1                   # ikkita yozuv emas, bitta
    assert len(items) == 1


async def test_dedup_is_scoped_per_user(storage: StorageService, user) -> None:
    """Dedup faqat bitta foydalanuvchi doirasida: boshqa user uchun alohida yozuv."""
    other, _ = await storage.get_or_create_user(telegram_id=888, first_name="Other")
    await make_file(storage, user.telegram_id, name="x.mp4", unique_id="uniq-x")

    again = await storage.add_file(
        telegram_id=other.telegram_id,
        file_id="AgAD-CCC",
        file_unique_id="uniq-x",
        file_name="x.mp4",
        file_size=1024,
    )
    assert again.is_new is True         # boshqa foydalanuvchi — yangi yozuv
    assert (await storage.list_files(user.telegram_id))[1] == 1
    assert (await storage.list_files(other.telegram_id))[1] == 1


async def test_dedup_refreshes_file_id(storage: StorageService, user) -> None:
    """Dedup paytida yangi ``file_id`` saqlanadi (eskalab eskirgan bo'lishi mumkin)."""
    await storage.add_file(
        telegram_id=user.telegram_id, file_id="AgAD-OLD", file_unique_id="u1",
        file_name="f.txt", file_size=10,
    )
    result = await storage.add_file(
        telegram_id=user.telegram_id, file_id="AgAD-NEW", file_unique_id="u1",
        file_name="f.txt", file_size=10,
    )
    assert result.record.file_id == "AgAD-NEW"


async def test_storage_used_not_double_counted(storage: StorageService, user) -> None:
    """Dedup kvotani ikki marta hisoblamaydi (``storage_used`` oshmaydi)."""
    await storage.add_file(
        telegram_id=user.telegram_id, file_id="A", file_unique_id="dup",
        file_name="a.bin", file_size=1000,
    )
    used_once = user.storage_used
    await storage.add_file(
        telegram_id=user.telegram_id, file_id="B", file_unique_id="dup",
        file_name="a.bin", file_size=1000,
    )
    assert user.storage_used == used_once


# --------------------------------------------------------------------------- #
# Saralash (4 xil: vaqt ↑↓, hajm ↑↓) + nom
# --------------------------------------------------------------------------- #
async def _seed_three(storage: StorageService, telegram_id: int) -> None:
    """Uchta fayl: yaratilgan vaqti ham, hajmi ham turlicha.

    Nomlar ASCII tanlangan — SQLite ``ORDER BY`` ni bayt (BINARY collation)
    bo'yicha bajaradi, lotin harflarida tartib bir xil bo'ladi.
    """
    plan = [("aaa_kichik.txt", 100, 1), ("bbb_orta.txt", 2000, 2), ("ccc_katta.zip", 30000, 3)]
    for name, size, day in plan:
        record = await make_file(storage, telegram_id, name=name, size=size)
        record.created_at = _dt.datetime(2026, 1, day, tzinfo=_dt.UTC)
        storage.session.add(record)
    await storage.session.commit()


@pytest.mark.parametrize(
    ("sort_by", "sort_desc", "expected"),
    [
        ("created_at", True, ["ccc_katta.zip", "bbb_orta.txt", "aaa_kichik.txt"]),    # 🕒 Eng yangi
        ("created_at", False, ["aaa_kichik.txt", "bbb_orta.txt", "ccc_katta.zip"]),   # 🕒 Eng eski
        ("size", True, ["ccc_katta.zip", "bbb_orta.txt", "aaa_kichik.txt"]),          # 📦 Eng katta
        ("size", False, ["aaa_kichik.txt", "bbb_orta.txt", "ccc_katta.zip"]),         # 📦 Eng kichik
        ("file_size", True, ["ccc_katta.zip", "bbb_orta.txt", "aaa_kichik.txt"]),     # API alias
        ("name", True, ["ccc_katta.zip", "bbb_orta.txt", "aaa_kichik.txt"]),          # 🔤 Z→A
        ("name", False, ["aaa_kichik.txt", "bbb_orta.txt", "ccc_katta.zip"]),         # 🔤 A→Z
    ],
)
async def test_sorting_matrix(
    storage: StorageService, user, sort_by: str, sort_desc: bool, expected: list[str]
) -> None:
    await _seed_three(storage, user.telegram_id)
    items, total = await storage.list_files(
        user.telegram_id, sort_by=sort_by, sort_desc=sort_desc, page_size=10
    )
    assert total == 3
    assert [row.file_name for row in items] == expected


async def test_unknown_sort_key_falls_back_to_created_at(storage: StorageService, user) -> None:
    """Noma'lum kalit xato bermaydi — sukut ``created_at DESC`` ga qaytadi."""
    await _seed_three(storage, user.telegram_id)
    items, _ = await storage.list_files(
        user.telegram_id, sort_by="DROP TABLE files", sort_desc=True, page_size=10
    )
    assert [row.file_name for row in items] == ["ccc_katta.zip", "bbb_orta.txt", "aaa_kichik.txt"]


async def test_pinned_always_first(storage: StorageService, user) -> None:
    """📌 Muhim fayl saralashdan qat'i nazar ro'yxat boshida turadi."""
    await _seed_three(storage, user.telegram_id)
    items, _ = await storage.list_files(user.telegram_id, sort_by="size", sort_desc=True, page_size=10)
    oldest = items[-1]                                  # "aaa_kichik.txt"
    await storage.toggle_pin(oldest.id, user.telegram_id)

    items2, _ = await storage.list_files(user.telegram_id, sort_by="size", sort_desc=True, page_size=10)
    assert items2[0].file_name == "aaa_kichik.txt"
    assert items2[0].is_pinned is True


async def test_pagination_is_stable(storage: StorageService, user) -> None:
    """Sahifalash: ``page_size=5`` (legacy) va sahifalar kesishmaydi."""
    for index in range(12):
        await make_file(storage, user.telegram_id, name=f"f{index:02d}.txt", size=10 + index)

    first_page, total = await storage.list_files(user.telegram_id, page=1, page_size=5)
    second_page, _ = await storage.list_files(user.telegram_id, page=2, page_size=5)
    third_page, _ = await storage.list_files(user.telegram_id, page=3, page_size=5)

    assert total == 12
    assert len(first_page) == 5 and len(second_page) == 5 and len(third_page) == 2
    ids = [row.id for row in [*first_page, *second_page, *third_page]]
    assert len(set(ids)) == 12                          # takrorlanish yo'q


async def test_default_page_size_is_five(storage: StorageService, settings, user) -> None:
    """Legacy mexanika: sukut sahifa hajmi 5 ta element."""
    assert settings.storage.page_size == 5
    for index in range(7):
        await make_file(storage, user.telegram_id, name=f"g{index}.txt")
    items, total = await storage.list_files(user.telegram_id)
    assert len(items) == 5 and total == 7


# --------------------------------------------------------------------------- #
# Guruhlar / kategoriyalar (🎬 Medialar, 🎵 Audiolar, 📄 Hujjatlar, 📦 Arxivlar, 🤖 APK/IPA)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("group", "expected"),
    [
        ("media", {"photo", "video", "animation"}),
        ("audio", {"audio", "voice"}),
        ("document", {"document", "other"}),
        ("archive", {"archive"}),
        ("apps", {"apk", "ipa"}),
    ],
)
def test_file_groups_map_to_categories(group: str, expected: set[str]) -> None:
    """``FILE_GROUPS`` guruh -> kategoriya to'plami mosligi."""
    assert {c.value for c in FILE_GROUPS[group]} == expected


async def _seed_categories(storage: StorageService, telegram_id: int) -> None:
    plan = [
        ("a.jpg", FileCategory.PHOTO), ("b.mp3", FileCategory.AUDIO),
        ("c.mp4", FileCategory.VIDEO), ("d.pdf", FileCategory.DOCUMENT),
        ("e.zip", FileCategory.ARCHIVE), ("f.apk", FileCategory.APK),
        ("g.ipa", FileCategory.IPA), ("h.ogg", FileCategory.VOICE),
    ]
    for name, category in plan:
        record = await make_file(storage, telegram_id, name=name, size=1024)
        record.category = category
        storage.session.add(record)
    await storage.session.commit()


async def test_group_filter_returns_only_matching(storage: StorageService, user) -> None:
    await _seed_categories(storage, user.telegram_id)

    async def names(group: str) -> set[str]:
        items, _ = await storage.list_files(user.telegram_id, group=group, page_size=50)
        return {row.file_name for row in items}

    assert await names("media") == {"a.jpg", "c.mp4"}
    assert await names("audio") == {"b.mp3", "h.ogg"}
    assert await names("document") == {"d.pdf"}
    assert await names("archive") == {"e.zip"}
    assert await names("apps") == {"f.apk", "g.ipa"}
    assert await names("all") == {
        "a.jpg", "b.mp3", "c.mp4", "d.pdf", "e.zip", "f.apk", "g.ipa", "h.ogg"
    }


async def test_group_counts(storage: StorageService, user) -> None:
    """/files menyusi uchun guruhlar kesimi bitta so'rovda."""
    await _seed_categories(storage, user.telegram_id)
    counts = await storage.group_counts(user.telegram_id)
    assert counts["all"] == 8
    assert counts["media"] == 2
    assert counts["audio"] == 2
    assert counts["document"] == 1
    assert counts["archive"] == 1
    assert counts["apps"] == 2
    assert counts["pinned"] == 0


async def test_apps_only_filter(storage: StorageService, user) -> None:
    """🤖 APK/IPA tabi: ``is_app`` belgisi bo'yicha filtr."""
    await _seed_categories(storage, user.telegram_id)
    items, total = await storage.list_files(user.telegram_id, apps_only=True, page_size=50)
    assert total == 2
    assert {row.file_name for row in items} == {"f.apk", "g.ipa"}
    assert all(row.is_app for row in items)


async def test_pinned_only_filter_and_toggle(storage: StorageService, user) -> None:
    """📌 Muhimlar: belgilash -> ro'yxatda, olib tashlash -> yo'q."""
    record = await make_file(storage, user.telegram_id, name="muhim.pdf")
    assert record.is_pinned is False

    toggled = await storage.toggle_pin(record.id, user.telegram_id)
    assert toggled.is_pinned is True

    pinned, total = await storage.list_files(user.telegram_id, pinned_only=True)
    assert total == 1 and pinned[0].id == record.id
    assert len(await storage.list_pinned(user.telegram_id)) == 1

    await storage.toggle_pin(record.id, user.telegram_id)
    _, total_after = await storage.list_files(user.telegram_id, pinned_only=True)
    assert total_after == 0


async def test_search_matches_name_case_insensitive(storage: StorageService, user) -> None:
    """Qidiruv: nom bo'yicha, katta-kichik harfga befarq."""
    await make_file(storage, user.telegram_id, name="Hisobot_2026.pdf")
    await make_file(storage, user.telegram_id, name="photo.jpeg")

    found = await storage.search_files(user.telegram_id, "hisobot")
    assert len(found) == 1
    assert found[0].file_name == "Hisobot_2026.pdf"
    assert list(await storage.search_files(user.telegram_id, "topilmadi")) == []


async def test_search_respects_group(storage: StorageService, user) -> None:
    """Inline qidiruv guruh bilan toraytiriladi (``media:hisobot``)."""
    await _seed_categories(storage, user.telegram_id)
    media_only = await storage.search_files(user.telegram_id, "a", group="media")
    assert {row.file_name for row in media_only} == {"a.jpg"}


async def test_category_detection_from_extension(storage: StorageService, user) -> None:
    """Kategoriya kengaytmadan avtomatik aniqlanadi (legacy mantiq saqlangan)."""
    record = await make_file(storage, user.telegram_id, name="dastur.apk", size=10)
    assert record.category is FileCategory.APK
    assert record.is_app is True

    video = await make_file(storage, user.telegram_id, name="film.mp4", size=10)
    assert video.category is FileCategory.VIDEO


# --------------------------------------------------------------------------- #
# Foydalanuvchi va sozlamalar
# --------------------------------------------------------------------------- #
async def test_get_or_create_user_is_idempotent(storage: StorageService) -> None:
    first, created_first = await storage.get_or_create_user(telegram_id=555, username="a", first_name="A")
    second, created_second = await storage.get_or_create_user(telegram_id=555, username="b", first_name="B")
    assert created_first is True and created_second is False
    assert first.telegram_id == second.telegram_id
    assert second.username == "b"        # profil yangilanadi


async def test_user_sort_preference_persists(storage: StorageService, user) -> None:
    """Saralash tanlovi ``users.settings.sort`` da saqlanadi."""
    await storage.update_settings(user.telegram_id, {"sort": "big"})
    refreshed = await storage.get_user_or_raise(user.telegram_id)
    assert refreshed.setting("sort") == "big"

    # Noto'g'ri qiymat yozilsa ham sukut qiymatga qaytadi (xato bermaydi).
    assert refreshed.setting("unknown_key", "fallback") == "fallback"


async def test_stats_payload(storage: StorageService, user) -> None:
    """"Mening bulutim" ekrani uchun agregat raqamlar."""
    await make_file(storage, user.telegram_id, name="a.mp4", size=1000, category="video")
    await make_file(storage, user.telegram_id, name="b.mp3", size=500, category="audio")

    payload = await storage.stats(user.telegram_id)
    assert payload["files"] == 2
    assert payload["used"] == 1500
    assert payload["unlimited"] is True
    assert payload["quota"] == 0
    assert payload["quota_label"] == "Cheksiz (Unlimited)"
    assert payload["by_category"]["video"]["count"] == 1
    assert payload["by_category"]["audio"]["size"] == 500


async def test_delete_file_removes_row_and_recalculates(storage: StorageService, user) -> None:
    """Faylni o'chirish: yozuv yo'qoladi, ``storage_used`` qayta hisoblanadi."""
    record = await make_file(storage, user.telegram_id, name="vaqtinchalik.txt", size=2048)
    await storage.recalculate_storage(user.telegram_id)
    assert user.storage_used == 2048

    removed = await storage.delete_file(record.id, user.telegram_id)
    assert removed >= 1
    _, total = await storage.list_files(user.telegram_id)
    assert total == 0
    assert await storage.recalculate_storage(user.telegram_id) == 0
