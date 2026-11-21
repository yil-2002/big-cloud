"""Password hashing helpers (stdlib only - no native build step).

The ``hugecloud`` spec allows *bcrypt or hashlib.sha256*. This module implements
the hashlib route as **PBKDF2-HMAC-SHA256**, which is the NIST-recommended way
to turn a low-entropy password into a hash using only the standard library:

* a fresh 16-byte random **salt** per account (so identical passwords differ);
* **210 000 iterations** by default (OWASP 2023 guidance for PBKDF2-SHA256);
* :func:`hmac.compare_digest` for **constant-time** comparison, which prevents
  timing side channels;
* the stored form is ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``, so the
  parameters travel with the hash and can be raised later without a migration.

If a deployment prefers real bcrypt, swap :func:`hash_password` /
:func:`verify_password` for ``bcrypt.hashpw`` / ``bcrypt.checkpw`` - the call
sites only depend on these two functions.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

__all__ = [
    "ALGORITHM",
    "DEFAULT_ITERATIONS",
    "hash_password",
    "is_password_hash",
    "needs_rehash",
    "verify_password",
]

#: Identifier written into the stored hash string.
ALGORITHM = "pbkdf2_sha256"
#: PBKDF2 rounds. Raise over time; :func:`needs_rehash` flags outdated hashes.
DEFAULT_ITERATIONS = 210_000
#: Salt length in bytes (128 bits of entropy).
_SALT_BYTES = 16


def _pbkdf2(password: str, salt: bytes, iterations: int) -> bytes:
    """Raw PBKDF2-HMAC-SHA256 derivation."""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=32
    )


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    """Return ``pbkdf2_sha256$iterations$salt$hash`` for ``password``.

    The salt is generated per call, so hashing the same password twice yields two
    different (equally valid) strings.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _pbkdf2(password, salt, iterations)
    return f"{ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check of ``password`` against a stored hash.

    Returns ``False`` (never raises) for malformed or empty hashes, so a corrupt
    row simply denies access instead of crashing the handler. Legacy plain
    ``sha256$<salt>$<hash>`` values are also accepted for compatibility.
    """
    if not stored or not password:
        return False

    parts = stored.split("$")
    try:
        if len(parts) == 4 and parts[0] == ALGORITHM:
            _, raw_iterations, salt_hex, hash_hex = parts
            iterations = int(raw_iterations)
        elif len(parts) == 3 and parts[0] == "sha256":
            # Legacy single-round sha256(salt + password) - accepted, then upgraded.
            _, salt_hex, hash_hex = parts
            iterations = 1
        else:
            return False

        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False

    if iterations == 1:  # legacy scheme
        candidate = hashlib.sha256(salt + password.encode("utf-8")).digest()
    else:
        candidate = _pbkdf2(password, salt, iterations)
    return hmac.compare_digest(candidate, expected)


def is_password_hash(value: str | None) -> bool:
    """``True`` when ``value`` looks like a hash produced by this module."""
    return bool(value) and value.split("$", 1)[0] in {ALGORITHM, "sha256"}


def needs_rehash(stored: str | None, *, iterations: int = DEFAULT_ITERATIONS) -> bool:
    """``True`` when the hash should be recomputed on the next successful login.

    Used to transparently migrate legacy single-round SHA-256 hashes (or hashes
    created with fewer iterations) to the current parameters.
    """
    if not stored:
        return True
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != ALGORITHM:
        return True
    try:
        return int(parts[1]) < iterations
    except ValueError:
        return True
