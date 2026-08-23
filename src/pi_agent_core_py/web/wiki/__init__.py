"""New page-centric LLM Wiki persistence package.

The legacy chunk-RAG implementation remains isolated under ``web.knowledge``
until the explicit retirement cutover.  This package never imports it.
"""

from .errors import (
    LegacyRetirementError,
    WikiErrorCode,
    WikiMirrorError,
    WikiPathError,
    WikiSchemaError,
    WikiStoreError,
)
from .files import WikiFileStore
from .legacy import retire_legacy_knowledge
from .models import (
    WIKI_LEGACY_MANIFEST_SCHEMA,
    WIKI_SCHEMA_VERSION,
    WIKI_SPACE_MANIFEST_SCHEMA,
    WikiLegacyBackupReceipt,
    WikiMirrorRepairReport,
    WikiSpace,
    WikiSpaceManifest,
    WikiSpaceStatus,
    validate_space_id,
)
from .store import WIKI_APPLICATION_ID, LegacyPolicy, WikiStore

__all__ = [
    "WIKI_LEGACY_MANIFEST_SCHEMA",
    "WIKI_SCHEMA_VERSION",
    "WIKI_SPACE_MANIFEST_SCHEMA",
    "LegacyRetirementError",
    "LegacyPolicy",
    "WIKI_APPLICATION_ID",
    "WikiErrorCode",
    "WikiLegacyBackupReceipt",
    "WikiFileStore",
    "WikiMirrorError",
    "WikiMirrorRepairReport",
    "WikiPathError",
    "WikiSchemaError",
    "WikiSpace",
    "WikiSpaceManifest",
    "WikiSpaceStatus",
    "WikiStoreError",
    "WikiStore",
    "retire_legacy_knowledge",
    "validate_space_id",
]
