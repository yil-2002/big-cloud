"""Parol hashlash (PBKDF2-HMAC-SHA256) testlari."""

from __future__ import annotations

from db.models.password import (
    ALGORITHM,
    DEFAULT_ITERATIONS,
    hash_password,
    is_password_hash,
    needs_rehash,
    verify_password,
)


def test_hash_is_salted_and_unique() -> None:
    """Bir xil parol ikki marta hashlansa ikki xil natija (salt tasodifiy)."""
    first = hash_password("parol123")
    second = hash_password("parol123")
    assert first != second
    assert first.startswith(f"{ALGORITHM}${DEFAULT_ITERATIONS}$")


def test_verify_accepts_correct_and_rejects_wrong() -> None:
    stored = hash_password("Sobirjon2005")
    assert verify_password("Sobirjon2005", stored) is True
    assert verify_password("sobirjon2005", stored) is False
    assert verify_password("", stored) is False


def test_verify_never_raises_on_garbage() -> None:
    """Buzilgan/qisqa hash — ``False``, istisno emas."""
    for broken in ("", "abc", "pbkdf2_sha256$1$2", "$$$", "sha256$zz$yy", None):
        assert verify_password("x", broken) is False


def test_legacy_sha256_accepted_then_flagged() -> None:
    """Eski ``sha256$salt$hash`` sxemasi qabul qilinadi va yangilash belgilanadi."""
    import hashlib
    import os

    salt = os.urandom(16)
    digest = hashlib.sha256(salt + b"oldpass").hexdigest()
    legacy = f"sha256${salt.hex()}${digest}"
    assert is_password_hash(legacy)
    assert verify_password("oldpass", legacy) is True
    assert needs_rehash(legacy) is True
    assert needs_rehash(hash_password("oldpass")) is False
