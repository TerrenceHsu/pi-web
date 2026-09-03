"""Internal authentication records and safe public projections."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuthUserRecord:
    """Persisted account row. Password material is deliberately repr-hidden."""

    id: str
    name: str
    password_hash: str = field(repr=False)
    is_admin: bool = False
    created_at: int = 0
    updated_at: int = 0


@dataclass(frozen=True)
class AuthUser:
    """Safe authenticated-user identity used outside the auth store."""

    id: str
    name: str
    is_admin: bool = False


def to_auth_user(record: AuthUserRecord) -> AuthUser:
    return AuthUser(id=record.id, name=record.name, is_admin=record.is_admin)


__all__ = ["AuthUser", "AuthUserRecord", "to_auth_user"]
