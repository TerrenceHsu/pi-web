# P2-R2-C4-R — Resource Audit

> 对 P2-R2-C4 阶段引入的所有 async / IO / 后台 task / executor 资源做显式释放审计。
> 
> 目标：找出导致完整 backend 套件出现 15 failures + thread leak warning（Thread-1100、`Event loop is closed`）的根因。

## 1. 失败现象

- **完整 backend 套件（2 次跑）**：稳定 15 failed
  - `tests/test_upload_api.py`：13 failures
  - `tests/test_web_prompt_execution_split.py`：2 failures
- **Isolation / subset 运行**：全部 PASS（含 test_upload_api.py 整文件单独跑 25/26 PASS）
- **Thread leak warning**：`PytestUnhandledThreadExceptionWarning: Exception in thread Thread-1100 (_connection_worker_thread)` + `RuntimeError: Event loop is closed`
- **失败模式**：所有 15 个失败测试都用 `pytest.raises(X)`，traceback 显示 X 确实被 raise，但 `pytest.raises` 没捕获；HTTP 层失败测试拿到 500 而非 415/409

## 2. 候选泄漏源审计

### 2.1 `aiosqlite.connect()` 不在 `async with` 内的 5 个 store

| Store | 文件:行 | schema-init 失败时是否 close conn？ |
|---|---|---|
| `KnowledgeStore` | `src/pi_agent_core_py/web/knowledge/store.py:309-322` | ❌ NO |
| `SQLiteCredentialStore` | `src/pi_agent_core_py/web/credentials_store.py:~310` | ❌ NO |
| `ExtensionStore` | `src/pi_agent_core_py/web/extension_store.py:396` | ❌ NO（不同 pattern） |
| `SessionStore` | `src/pi_agent_core_py/session_sqlite.py:242` | ❌ NO |
| `SQLiteProviderConfigStore` | `src/pi_agent_core_py/web/provider_config_store.py:517` | ✅ YES（`try/except BaseException: await conn.close()`） |

**触发条件**：仅当 `_initialize_schema()` 抛异常时泄漏（如 `KnowledgeSchemaVersionError`）。
- `tests/test_knowledge_store.py::TestSchema::test_future_schema_version_raises` 触发 1 次
- `tests/test_credentials_store_schema.py` 等也可能触发

**单次跑贡献**：~1-3 个 leaked thread。**不足以解释 Thread-1100**，但是确定的可修复点。

### 2.2 Worker Manager 生命周期

- `IngestionWorkerManager.start()`（`web/knowledge/ingestion_worker.py:242`）：创建 `_worker_task = asyncio.create_task(self._run_loop(), ...)`
- `IngestionWorkerManager.stop()`：bounded grace + cancel + await task；正确 cleanup
- 测试 fixture `running_manager`（`tests/test_upload_api.py:177-183`）：`try/finally: await worker_manager.stop()` ✅
- `tests/test_ingestion_worker_lifespan.py::TestParserClose::test_parser_closed_on_shutdown`：覆盖 lifespan 顺序（manager 先停再 store 关） ✅

**判定**：Worker Manager 生命周期 clean；不是主要泄漏源。

### 2.3 测试中裸露 `asyncio.create_task`

- `tests/test_credentials_service_validation_concurrency.py:449` `stale_task = asyncio.create_task(...)`：未显式 cancel/await，依赖 test-end 自动 cancel
- `tests/test_credentials_service_validation_concurrency.py:182/231/282/350/351/379`：均有 `await asyncio.wait_for(...)` 或后续 await
- `tests/test_step_15_compaction.py:270`：单 task，后面有 cancel

`stale_task` 是**显式意图**——测试要验证 stale task 的行为，pytest-asyncio 在 test 结束时会自动 cancel 未完成的 task。**理论不泄漏 thread**（task 是 coroutine 不是 thread）。

### 2.4 UploadFile / file handles

- `upload_service._stream_to_staging`（`web/knowledge/upload_service.py:502-568`）：
  - `f = await asyncio.to_thread(staging_path.open, "wb")` 打开 staging 文件
  - `try/finally: await asyncio.to_thread(f.close)` 保证关闭 ✅
- HTTP `UploadFile`：FastAPI 在 request scope 结束时自动关闭 ✅

**判定**：file handle 生命周期 clean。

### 2.5 临时目录

- pytest `tmp_path` fixture：测试结束自动删除 ✅
- `_pdf_fixture_factory.write_text_pdf`：写入 tmp_path 内 ✅
- staging files：`upload_service._safe_unlink` 在 finally 内删除 ✅

**判定**：临时目录 clean。

### 2.6 monkeypatch

- pytest 内置 monkeypatch fixture：测试结束自动 undo ✅
- `parser.inspect = counting_inspect`（`test_no_parser_invocation_at_upload`）：手动 monkeypatch，**没有显式还原**
  - 影响：下一个测试如果复用同一个 parser fixture 会受影响
  - 实际：parser fixture 是函数级（每次新建 PypdfParser），所以不污染下游
  - **理论不泄漏**

### 2.7 全局 app state

- `create_app()`：每次调用创建独立 app instance
- 测试用 `_build_app(tmp_path)`：每次创建新 app + 新 db_path + 新 knowledge_root
- `app.state.web.ingestion_worker_manager`：app 关闭时 lifespan 应 stop manager
- `tests/test_integration_web_server.py::test_multiple_create_app_does_not_accumulate_hooks`：验证不累积 hooks ✅

**判定**：app state 隔离 clean。

### 2.8 default ThreadPoolExecutor

- `asyncio.to_thread` 用 default executor（`concurrent.futures.ThreadPoolExecutor`）
- Python 默认 max_workers = min(32, os.cpu_count() + 4) ≈ 5-20
- 套件中有 ~50 处 `asyncio.to_thread` 调用（upload_service、ingestion_orchestrator、app 等）
- 每个 `to_thread` 调用从 pool 借 thread，归还后复用
- **理论不泄漏 thread**（pool 复用）

### 2.9 aiosqlite worker thread 累积

**最可能根因**。每个 `aiosqlite.connect()` spawn 一个 `_connection_worker_thread`。该 thread 在 `conn.close()` 时通过 `connection.join()` 被 joined。

如果 conn 没 close → thread 没 join → thread 持有 future.get_loop() 引用前一个 event loop → 下个测试 event loop 关闭时触发 `Event loop is closed`。

**累积源**：
- 所有 fixture 创建 store 但 close 在 `finally` 内 — 正常
- 罕见路径（schema-init 失败、lifespan 抢占关闭）会绕过 close — 泄漏
- 跨 fixture 嵌套时若 close 顺序错误（`manager` 没 stop 就 `store.close()`），可能留下未释放 conn

## 3. 综合判定

### 已确定的可修复泄漏

1. **5 个 store 中 4 个的 `open()` 缺 try/except 保护**：
   - `KnowledgeStore.open` / `SQLiteCredentialStore.open` / `ExtensionStore` / `SessionStore.open`
   - 修复方案：复制 `SQLiteProviderConfigStore.open` 的 `try/except BaseException: await conn.close()` 模式
   - **影响**：罕见路径不再泄漏 thread；约 ~3-5 个 thread per suite

### 未确定的主导泄漏

- Thread-1100 表示 1100+ thread 创建。即使每个泄漏 1 个，也需要 ~1100 个泄漏点
- 单次 suite run 中 store 创建次数远小于此
- **可能的解释**：
  - aiosqlite 在 Python 3.12 + Windows 上有累积 issue（待验证）
  - 或 thread 编号被 default ThreadPoolExecutor 的 `to_thread` 调用累积（每次借/还可能用新 thread，不严格复用）

### pytest.raises 不捕获的机制

- **未确定**。traceback 显示 X 异常被 raise，但 `pytest.raises(X)` 没捕获
- 假设：累积的 `Event loop is closed` 状态在 `await` 边界产生不可见异常，破坏 exception propagation
- 修复 store leak 后重跑可验证此假设：若 failures 消失 → 假设成立

## 4. 修复优先级

| 修复 | 优先级 | 工作量 | 预期影响 |
|---|---|---|---|
| 4 个 store.open() 加 try/except close 保护 | P0 | 小 | 消除罕见路径泄漏 |
| 重跑完整套件验证 | P0 | 中（6 分钟/次） | 0-fail 确认 |
| 添加 fixture-level thread counter 测试 | P1 | 中 | 回归保护 |
| 若 store fix 无效，深入 aiosqlite 累积问题 | P1 | 大 | 平台级根因 |

## 5. 不修复项（明确 clean）

- Worker Manager 生命周期 ✅
- UploadFile / file handles ✅
- 临时目录 ✅
- monkeypatch ✅
- app state ✅
- default ThreadPoolExecutor ✅（理论）
