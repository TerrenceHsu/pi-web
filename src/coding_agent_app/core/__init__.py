"""Core coding-agent product composition API."""

from .prompts import PromptContribution, compose_system_prompt_suffix
from .sdk import *  # noqa: F403
from .sdk import __all__ as _sdk_all

__all__ = [*_sdk_all, "PromptContribution", "compose_system_prompt_suffix"]
