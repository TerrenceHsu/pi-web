"""Private bounded Bash records; no execution or recovery replay entry point."""

from collections.abc import AsyncIterator

import aiosqlite
import pytest

from coding_agent_app.execution.models import ExecutionDenied
from coding_agent_app.execution.runtime import ExecutionRequest
from coding_sandbox.bash import BashRequest
from pi_agent_core_py.web.bash import BashHistory


@pytest.fixture
async def history() -> AsyncIterator[BashHistory]:
    async with aiosqlite.connect(":memory:", isolation_level=None) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        await db.execute("INSERT INTO sessions VALUES ('session')")
        history = BashHistory(db)
        await history.init()
        yield history


def request(session_id: str = "session") -> ExecutionRequest:
    return ExecutionRequest(
        account_id="account", session_id=session_id, request_id="request",
        workspace_id=session_id,
    )


async def test_history_replay_fk_restart_and_private_detail(history: BashHistory) -> None:
    bash = BashRequest(script="printf 'private script'")
    run = await history.create(request(), "one", bash)
    with pytest.raises(ExecutionDenied, match="bash_call_replay_or_invalid_session"):
        await history.create(request(), "one", bash)
    with pytest.raises(ExecutionDenied, match="bash_call_replay_or_invalid_session"):
        await history.create(request("missing"), "two", bash)
    assert await history.read("other", run) == []
    assert "script" not in (await history.read("session"))[0]
    assert (await history.read("session", run))[0]["script"] == bash.script
    await history.init()
    assert (await history.read("session", run))[0]["status"] == "interrupted"
    await history._db.execute("DELETE FROM sessions WHERE id='session'")
    assert await history.read("session", run) == []


async def test_history_live_quota_never_evicts_live_runs(history: BashHistory) -> None:
    bash = BashRequest(script="true")
    runs = [await history.create(request(), str(i), bash) for i in range(200)]
    with pytest.raises(ExecutionDenied, match="bash_history_full"):
        await history.create(request(), "overflow", bash)
    # A finished, oldest row can be evicted; pending rows cannot.
    await history.finish("session", runs[0], "succeeded", {"stdout": "done"})
    await history._db.execute(
        "UPDATE web_bash_runs SET created_at_ms=0 WHERE run_id=?", (runs[0],),
    )
    await history.create(request(), "next", bash)
    assert await history.read("session", runs[0]) == []
    assert len(await history.read("session")) == 100  # list projection, not account quota
    async with history._db.execute("SELECT count(*) FROM web_bash_runs") as cursor:
        assert (await cursor.fetchone())[0] == 200


async def test_history_expiry_prunes_terminal_only_on_new_run(history: BashHistory) -> None:
    bash = BashRequest(script="true")
    done = await history.create(request(), "old-done", bash)
    pending = await history.create(request(), "old-pending", bash)
    await history.finish("session", done, "failed", {"exit_code": 1})
    await history._db.execute("UPDATE web_bash_runs SET created_at_ms=0")
    assert len(await history.read("session")) == 2  # GET does not mutate or replay
    await history.create(request(), "new", bash)
    assert await history.read("session", done) == []
    assert (await history.read("session", pending))[0]["status"] == "pending"


async def test_history_payload_limit_is_atomic(history: BashHistory) -> None:
    run = await history.create(request(), "one", BashRequest(script="true"))
    await history.finish("session", run, "running", {"stdout": "bounded"})
    with pytest.raises(ExecutionDenied, match="bash_history_limit"):
        await history.finish("session", run, "succeeded", {"stdout": "x" * 262144})
    record = (await history.read("session", run))[0]
    assert record["status"] == "running" and record["result"]["stdout"] == "bounded"
    await history.init()
    assert (await history.read("session", run))[0]["status"] == "interrupted"
