"""Module alias for the canonical SQLite session backend.

A real module alias (instead of symbol-only re-exports) preserves legacy
monkeypatch behavior for private test hooks such as ``_gen_id`` and the shared
``aiosqlite`` module.
"""

import sys

import aiosqlite as aiosqlite

from .session_backends.sqlite import store as _store
from .session_backends.sqlite.store import *  # noqa: F403
from .session_backends.sqlite.store import __all__ as __all__
from .session_backends.sqlite.store import _gen_id as _gen_id
from .session_backends.sqlite.store import _serialize_message as _serialize_message

sys.modules[__name__] = _store
