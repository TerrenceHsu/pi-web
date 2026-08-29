"""Compatibility imports for the extracted :mod:`agent_workspace` package.

New code must import Workspace domain objects from :mod:`agent_workspace`.
This module remains temporarily so downstream users can migrate without a
breaking release.
"""

from agent_workspace.store import *  # noqa: F403
from agent_workspace.store import __all__ as __all__
from agent_workspace.store import _extended_length_path as _extended_length_path
