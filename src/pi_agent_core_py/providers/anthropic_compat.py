"""Compatibility facade for the canonical Anthropic adapter."""

from ..ai.providers.anthropic_compat import *  # noqa: F403
from ..ai.providers.anthropic_compat import (
    _extract_anthropic_usage as _extract_anthropic_usage,
)
