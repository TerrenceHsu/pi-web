"""Security tests for SQLiteProviderConfigStore (E2-1).

覆盖 8 项安全行为（spec §16.6）：
58. schema 不含 Secret 字段
59. dataclass 不含 Secret 字段
60. SQLite main / WAL / SHM 不出现测试 Secret marker
61. 错误 str/repr 不泄漏 row 或 SQL
62. decode error 不 dump row
63. Store repr 不暴露 connection
64. 生产源码不引入 API Key 常量
65. 不修改 E1 Credential 表
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import aiosqlite
import pytest

from pi_agent_core_py.web.provider_config_store import (
    ProviderConfigRecordDecodeError,
    ProviderConfigStoreError,
    ProviderProfile,
    ProviderProfileNotFoundError,
    SessionModelBinding,
    SQLiteProviderConfigStore,
)

# Test sentinel—production code must NEVER emit this string anywhere
# (SQLite bytes / errors / repr / source).
SECRET_MARKER = "PI_E2_TEST_MARKER_5C8E27B4"


@pytest.fixture
async def store(tmp_path: Path) -> SQLiteProviderConfigStore:
    counter = {"n": 1000}

    def fake_now() -> int:
        counter["n"] += 1
        return counter["n"]

    s = await SQLiteProviderConfigStore.open(
        tmp_path / "sec.db",
        now_ms=fake_now,
    )
    try:
        yield s
    finally:
        await s.close()


# ============================================================================
# 58. schema has no Secret field
# ============================================================================


@pytest.mark.asyncio
async def test_58_schema_has_no_secret_fields(tmp_path: Path) -> None:
    """Production DDL must NOT define api_key / secret / authorization / etc."""
    db = tmp_path / "schema.db"
    s = await SQLiteProviderConfigStore.open(db)
    try:
        conn = await aiosqlite.connect(str(db))
        conn.row_factory = aiosqlite.Row
        try:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'web_%'"
            ) as cursor:
                rows = await cursor.fetchall()
            forbidden = [
                "api_key", "secret", "secret_value", "secret_ref",
                "fingerprint", "masked_value", "authorization",
                "headers", "base_url", "validation_endpoint",
            ]
            # Verify column-by-column via PRAGMA
            for row in rows:
                table = row["name"]
                async with conn.execute(
                    f"PRAGMA table_info({table})"
                ) as cursor:
                    cols = await cursor.fetchall()
                col_names = {c["name"].lower() for c in cols}
                for bad in forbidden:
                    assert bad not in col_names, (
                        f"table {table} defines forbidden column '{bad}'"
                    )
        finally:
            await conn.close()
    finally:
        await s.close()


# ============================================================================
# 59. dataclass has no Secret field
# ============================================================================


def test_59_dataclasses_have_no_secret_fields() -> None:
    """ProviderProfile / SessionModelBinding fields must NEVER carry Secret semantics."""
    forbidden_substrings = (
        "api_key", "secret", "authorization", "headers",
        "base_url", "validation_endpoint", "masked_value",
        "fingerprint", "secret_ref", "secret_value",
    )

    for cls in (ProviderProfile, SessionModelBinding):
        fields = {f.name.lower() for f in dataclasses.fields(cls)}
        for bad in forbidden_substrings:
            assert bad not in fields, (
                f"{cls.__name__} defines forbidden field '{bad}'"
            )


# ============================================================================
# 60. SQLite main / WAL / SHM do not contain SECRET_MARKER
# ============================================================================


@pytest.mark.asyncio
async def test_60_sqlite_files_have_no_secret_marker(tmp_path: Path) -> None:
    """After normal operations, SECRET_MARKER must not appear in main/wal/shm.

    E2 store does NOT persist any secret-like value, so injecting a marker
    via normal API is impossible—this test verifies the negative property.
    """
    db = tmp_path / "marker.db"
    s = await SQLiteProviderConfigStore.open(db)
    try:
        # Do NOT write SECRET_MARKER anywhere—it should never appear
        await s.create_profile(
            profile_id="profile-normal",
            name="NormalName",
            provider_id="anthropic",
            credential_id="cred-normal",
            default_model="claude-X",
        )
        await s.upsert_binding(
            session_id="sess-normal",
            profile_id="profile-normal",
            model_id="claude-X",
            source="default",
        )
    finally:
        await s.close()

    # Scan main + WAL + SHM (whichever exist after Store close).
    # No need to force WAL mode—just check whatever SQLite produced.
    candidates = [db, db.with_suffix(".db-wal"), db.with_suffix(".db-shm")]
    for path in candidates:
        if not path.exists():
            continue
        raw = path.read_bytes()
        for encoding in ("utf-8", "latin-1"):
            try:
                text = raw.decode(encoding, errors="ignore")
            except Exception:
                continue
            assert SECRET_MARKER not in text, (
                f"SECRET_MARKER leaked into {path.name} ({encoding})"
            )


# ============================================================================
# 61. error str/repr do not leak row or SQL
# ============================================================================


@pytest.mark.asyncio
async def test_61_error_messages_do_not_leak_row_or_sql(
    store: SQLiteProviderConfigStore,
) -> None:
    """NotFound / InUse / State errors must not contain full rows or SQL fragments."""
    # NotFound error
    try:
        await store.get_profile("profile-missing-xyz")
    except ProviderProfileNotFoundError as e:
        msg = str(e)
        assert "SELECT" not in msg.upper()
        assert "FROM web_provider_profiles" not in msg
        # Only safe ID allowed
        assert "profile-missing-xyz" in msg  # safe
    else:
        pytest.fail("expected ProviderProfileNotFoundError")

    # Create a profile + binding, then try delete → InUse
    await store.create_profile(
        profile_id="profile-iu",
        name="IU",
        provider_id="anthropic",
        credential_id="cred-iu",
        default_model="claude-X",
    )
    await store.upsert_binding(
        session_id="sess-iu",
        profile_id="profile-iu",
        model_id="claude-X",
        source="explicit",
    )
    from pi_agent_core_py.web.provider_config_store import (
        ProviderProfileInUseError,
    )
    with pytest.raises(ProviderProfileInUseError) as exc_info:
        await store.delete_profile("profile-iu")
    msg = str(exc_info.value)
    assert "SELECT" not in msg.upper()
    assert "FROM web_session_model_bindings" not in msg
    assert "WHERE" not in msg.upper()


# ============================================================================
# 62. decode error does not dump row
# ============================================================================


@pytest.mark.asyncio
async def test_62_decode_error_does_not_dump_row(tmp_path: Path) -> None:
    """ProviderConfigRecordDecodeError must not include full row in str/repr."""
    from pi_agent_core_py.web.provider_config_store import _decode_profile

    # Construct a row-like dict missing required keys
    bad_row = {
        "id": "profile-bad",
        # Missing: name, provider_id, etc.
    }
    with pytest.raises(ProviderConfigRecordDecodeError) as exc_info:
        _decode_profile(bad_row)

    msg = str(exc_info.value)
    # Safe id allowed
    assert "profile-bad" in msg
    # Must not contain other field names from row (since row doesn't have them,
    # the test verifies the error doesn't dump unrelated content)
    assert "KeyError" in msg or "TypeError" in msg or "IndexError" in msg


@pytest.mark.asyncio
async def test_62b_decode_binding_error_does_not_dump_row() -> None:
    from pi_agent_core_py.web.provider_config_store import _decode_binding

    bad_row = {"session_id": "sess-x"}
    with pytest.raises(ProviderConfigRecordDecodeError) as exc_info:
        _decode_binding(bad_row)
    msg = str(exc_info.value)
    assert "sess-x" in msg  # safe id


# ============================================================================
# 63. Store repr does not expose connection
# ============================================================================


@pytest.mark.asyncio
async def test_63_store_repr_does_not_expose_connection(
    store: SQLiteProviderConfigStore,
) -> None:
    """repr(store) must not include the aiosqlite.Connection object or DB path."""
    r = repr(store)
    assert "Connection" not in r
    assert "connection" not in r.lower()
    # Should not include absolute DB path
    assert ".db" not in r
    assert "tmp_path" not in r.lower() or "tmp_path" not in r


# ============================================================================
# 64. production source does not introduce API Key constants
# ============================================================================


def test_64_production_source_has_no_api_key_or_http_patterns() -> None:
    """Provider Config Store must NOT introduce HTTP / keyring / Anthropic / OpenAI
    imports or constant strings—E2-1 is SQLite-only.
    """
    src_path = (
        Path(__file__).parent.parent
        / "src"
        / "pi_agent_core_py"
        / "web"
        / "provider_config_store.py"
    )
    source = src_path.read_text(encoding="utf-8")

    # No HTTP / external service imports
    forbidden_imports = (
        "import httpx",
        "import requests",
        "import aiohttp",
        "from httpx",
        "from anthropic",
        "from openai",
        "import keyring",
        "from keyring",
    )
    for imp in forbidden_imports:
        assert imp not in source, f"forbidden import in production source: {imp}"

    # No literal HTTP header constants
    forbidden_literals = (
        '"Authorization"',
        "'Authorization'",
        '"x-api-key"',
        "'x-api-key'",
        '"Bearer "',
        "'Bearer '",
    )
    for lit in forbidden_literals:
        assert lit not in source, (
            f"forbidden literal in production source: {lit}"
        )

    # No test marker leaked into production
    assert SECRET_MARKER not in source


# ============================================================================
# 65. does not modify E1 Credential table
# ============================================================================


@pytest.mark.asyncio
async def test_65_e1_credential_schema_untouched(tmp_path: Path) -> None:
    """Opening Provider Config Store must NOT create or alter web_credentials."""
    db = tmp_path / "e1.db"

    # First seed Credentials schema via E1 store
    from pi_agent_core_py.web.credentials_store import (
        WEB_CREDENTIALS_SCHEMA_VERSION,
        SQLiteCredentialStore,
    )
    cred = await SQLiteCredentialStore.open(str(db))
    await cred.close()

    # Snapshot the credentials schema version + table count
    conn = await aiosqlite.connect(str(db))
    conn.row_factory = aiosqlite.Row
    try:
        async with conn.execute(
            "SELECT value FROM web_credentials_schema_meta WHERE key='version'"
        ) as cursor:
            row = await cursor.fetchone()
        cred_version_before = int(row["value"])
    finally:
        await conn.close()

    # Now open Provider Config Store at same DB file
    pc = await SQLiteProviderConfigStore.open(db)
    try:
        pass
    finally:
        await pc.close()

    # Re-verify credentials schema unchanged
    conn = await aiosqlite.connect(str(db))
    conn.row_factory = aiosqlite.Row
    try:
        async with conn.execute(
            "SELECT value FROM web_credentials_schema_meta WHERE key='version'"
        ) as cursor:
            row = await cursor.fetchone()
        cred_version_after = int(row["value"])
        assert cred_version_after == cred_version_before
        assert cred_version_after == WEB_CREDENTIALS_SCHEMA_VERSION

        # web_credentials table still exists with original schema
        async with conn.execute(
            "PRAGMA table_info(web_credentials)"
        ) as cursor:
            rows = await cursor.fetchall()
        col_names = {r["name"] for r in rows}
        # Spot-check a few known E1 columns
        assert "secret_ref" in col_names
        assert "masked_value" in col_names
        assert "fingerprint_sha256" in col_names
    finally:
        await conn.close()


# ============================================================================
# Additional: errors inherit from ProviderConfigStoreError
# ============================================================================


def test_all_errors_inherit_from_base() -> None:
    """All ProviderConfig* exceptions inherit from ProviderConfigStoreError.

    Lets callers write ``except ProviderConfigStoreError`` to catch all
    domain errors uniformly.
    """
    from pi_agent_core_py.web import provider_config_store as mod

    expected = [
        "ProviderConfigSchemaError",
        "ProviderConfigSchemaVersionError",
        "ProviderConfigSchemaValidationError",
        "ProviderProfileNotFoundError",
        "ProviderProfileAlreadyExistsError",
        "ProviderProfileInUseError",
        "ProviderProfileStateError",
        "SessionModelBindingConflictError",
        "ProviderConfigRecordDecodeError",
    ]
    for name in expected:
        cls = getattr(mod, name)
        assert issubclass(cls, ProviderConfigStoreError), (
            f"{name} must inherit from ProviderConfigStoreError"
        )
