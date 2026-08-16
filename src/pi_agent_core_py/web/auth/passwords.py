"""PBKDF2 password hashing for local authenticated accounts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 600_000
PASSWORD_SALT_BYTES = 16
PASSWORD_DERIVED_KEY_BYTES = 32

_MIN_ACCEPTED_ITERATIONS = 100_000
_MAX_ACCEPTED_ITERATIONS = 2_000_000


def _encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_bytes(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(
    password: str,
    *,
    iterations: int = PASSWORD_ITERATIONS,
    salt: bytes | None = None,
) -> str:
    """Return a salted PBKDF2-HMAC-SHA256 encoded password hash."""

    if not isinstance(password, str):
        raise TypeError("password must be a string")
    if not (_MIN_ACCEPTED_ITERATIONS <= iterations <= _MAX_ACCEPTED_ITERATIONS):
        raise ValueError("password hash iteration count is outside safe bounds")
    actual_salt = os.urandom(PASSWORD_SALT_BYTES) if salt is None else salt
    if len(actual_salt) < PASSWORD_SALT_BYTES:
        raise ValueError("password hash salt is too short")
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        actual_salt,
        iterations,
        dklen=PASSWORD_DERIVED_KEY_BYTES,
    )
    return "$".join(
        (
            PASSWORD_ALGORITHM,
            str(iterations),
            _encode_bytes(actual_salt),
            _encode_bytes(derived),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    """Verify a password in constant time and treat malformed hashes as failures."""

    try:
        algorithm, iterations_text, salt_text, expected_text = encoded.split("$", 3)
        if algorithm != PASSWORD_ALGORITHM:
            return False
        iterations = int(iterations_text)
        if not (_MIN_ACCEPTED_ITERATIONS <= iterations <= _MAX_ACCEPTED_ITERATIONS):
            return False
        salt = _decode_bytes(salt_text)
        expected = _decode_bytes(expected_text)
        if len(salt) < PASSWORD_SALT_BYTES or len(expected) != PASSWORD_DERIVED_KEY_BYTES:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations,
            dklen=len(expected),
        )
    except (TypeError, ValueError, UnicodeError):
        return False
    return hmac.compare_digest(actual, expected)


__all__ = ["PASSWORD_ALGORITHM", "PASSWORD_ITERATIONS", "hash_password", "verify_password"]
