"""Compatibility alias for the canonical SQLite repository module.

Historically the complete implementation lived in ``store.py`` and callers
patched module-private helpers such as ``_gen_id``.  A real module alias keeps
that behavior while allowing the backend to use the same physical ownership
as pi-agent's SQLite session backend.
"""

from __future__ import annotations

import sys

from . import repo as _repo
from .repo import *  # noqa: F403
from .repo import __all__ as __all__
from .repo import _gen_id as _gen_id
from .repo import _serialize_message as _serialize_message

sys.modules[__name__] = _repo
