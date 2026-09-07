"""Authentication service: bootstrap, login sessions, and safe identities."""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
import weakref
from dataclasses import dataclass, field

from .models import AuthUser, to_auth_user
from .passwords import hash_password, verify_password
from .store import AuthStore

BOOTSTRAP_USER_NAME = "admin"
BOOTSTRAP_USER_PASSWORD = "123456"
DEFAULT_SESSION_TTL_SECONDS = 24 * 60 * 60

_USER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MIN_PASSWORD_LENGTH = 6
_MAX_PASSWORD_LENGTH = 128


@dataclass(frozen=True)
class LoginSession:
    user: AuthUser
    token: str = field(repr=False)
    expires_at: int = 0


def _normalize_name(name: str) -> str | None:
    if not isinstance(name, str):
        return None
    normalized = name.strip()
    return normalized if _USER_NAME_PATTERN.fullmatch(normalized) else None


def _valid_password(password: str) -> bool:
    return (
        isinstance(password, str)
        and _MIN_PASSWORD_LENGTH <= len(password) <= _MAX_PASSWORD_LENGTH
        and "\x00" not in password
    )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthService:
    def __init__(
        self,
        store: AuthStore,
        *,
        session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
    ) -> None:
        if session_ttl_seconds <= 0:
            raise ValueError("session_ttl_seconds must be positive")
        self._store = store
        self.session_ttl_seconds = session_ttl_seconds
        self._dummy_hash: str | None = None
        self._revocations: weakref.WeakValueDictionary[str, asyncio.Event] = (
            weakref.WeakValueDictionary()
        )

    async def ensure_initial_admin(self) -> tuple[AuthUser, bool]:
        """Idempotently create the initial ``admin / 123456`` account."""

        password_hash = await asyncio.to_thread(hash_password, BOOTSTRAP_USER_PASSWORD)
        record, created = await self._store.create_initial_user_if_empty(
            BOOTSTRAP_USER_NAME,
            password_hash,
            is_admin=True,
        )
        self._dummy_hash = password_hash
        return to_auth_user(record), created

    async def login(self, *, name: str, password: str) -> LoginSession | None:
        """Verify credentials and issue an opaque, server-side login session."""

        normalized = _normalize_name(name)
        candidate_valid = _valid_password(password)
        record = await self._store.get_user_by_name(normalized) if normalized else None
        encoded = record.password_hash if record is not None else self._dummy_hash
        if encoded is None:
            encoded = await asyncio.to_thread(hash_password, "invalid-login-placeholder")
            self._dummy_hash = encoded
        candidate = password if candidate_valid else "invalid-login-placeholder"
        matches = await asyncio.to_thread(verify_password, candidate, encoded)
        if record is None or not candidate_valid or not matches:
            return None

        token = secrets.token_urlsafe(48)
        created_at = int(time.time() * 1000)
        expires_at = created_at + self.session_ttl_seconds * 1000
        issued = await self._store.create_session(
            token_hash=_token_hash(token),
            user_id=record.id,
            created_at=created_at,
            expires_at=expires_at,
            expected_password_hash=record.password_hash,
        )
        if not issued:
            return None
        return LoginSession(
            user=to_auth_user(record),
            token=token,
            expires_at=expires_at,
        )

    def revocation_event(self, user_id: str) -> asyncio.Event:
        event = self._revocations.get(user_id)
        if event is None:
            event = asyncio.Event()
            self._revocations[user_id] = event
        return event

    async def change_password(
        self, user_id: str, *, current_password: str, new_password: str,
    ) -> bool:
        if (
            not _valid_password(current_password) or not _valid_password(new_password)
            or len(new_password) < 12 or new_password == current_password
        ):
            return False
        record = await self._store.get_user(user_id)
        if record is None or not await asyncio.to_thread(
            verify_password, current_password, record.password_hash,
        ):
            return False
        encoded = await asyncio.to_thread(hash_password, new_password)
        changed = await self._store.change_password(
            user_id, expected_hash=record.password_hash, new_hash=encoded,
        )
        if changed:
            event = self._revocations.pop(user_id, None)
            if event is not None:
                event.set()
        return changed

    async def resolve_session(self, token: str | None) -> AuthUser | None:
        if token is None or len(token) < 32 or len(token) > 256:
            return None
        record = await self._store.get_user_for_session(
            _token_hash(token),
            now_ms=int(time.time() * 1000),
        )
        return to_auth_user(record) if record is not None else None

    async def logout(self, token: str | None) -> None:
        if token is None or len(token) < 32 or len(token) > 256:
            return
        await self._store.revoke_session(_token_hash(token))


__all__ = [
    "BOOTSTRAP_USER_NAME",
    "BOOTSTRAP_USER_PASSWORD",
    "DEFAULT_SESSION_TTL_SECONDS",
    "AuthService",
    "LoginSession",
]
