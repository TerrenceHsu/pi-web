"""Small parameterized-SQL composition helpers for the SQLite adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SqlFragment:
    """Trusted SQL text plus separately bound values."""

    text: str
    params: tuple[Any, ...] = ()


def sql(text: str, *params: Any) -> SqlFragment:
    """Create a parameterized fragment without interpolating user values."""

    return SqlFragment(text=text, params=params)


def join_sql_fragments(
    fragments: list[SqlFragment] | tuple[SqlFragment, ...], separator: str
) -> SqlFragment:
    """Join trusted fragments while retaining their binding order."""

    return SqlFragment(
        text=separator.join(fragment.text for fragment in fragments),
        params=tuple(param for fragment in fragments for param in fragment.params),
    )


__all__ = ["SqlFragment", "join_sql_fragments", "sql"]
