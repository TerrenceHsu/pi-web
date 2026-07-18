# P1-E2-3A Session Creation Audit — Binding Integration Strategy

> **状态**：AUDIT COMPLETE — integration strategy approved
> **基线**：`a2c7932`（master HEAD after E2-2 freeze）
> **范围**：E2-3A Session Creation Audit（无生产代码 / 无测试改动）
> **日期**：2026-07-19
> **审计模式**：read-only；调用链 + 删除能力 + 并发可见性 + ASGI 取消 + 三方案评估

## 0. 审计目标

只回答一个问题：

> 新建 Session 时如何安全创建默认 `SessionModelBinding`，Binding 创建失败时是否能可靠撤销 Session？

不写生产代码，不新增测试，不挂 API。E2-3 才实施。

## 1. 调用链审计

### 1.1 Session 创建入口

| 入口 | 位置 | 调用方 | 输入 | 输出 | 事务边界 | 错误映射 |
|---|---|---|---|---|---|---|
| `POST /api/sessions` | `web/app.py:2305 post_sessions` | HTTP client / 前端 "New chat" 按钮 | `{title?, metadata?}` | `{id, title, created_at, updated_at, metadata}` | 调用 `store.create_session()`——SQLite 隐式事务（autocommit）| `SessionNotFoundError → 404`；`SQLiteSessionError → 503` |
| `ensure_default_session(title="default")` | `session_sqlite.py:432` | Web app lifespan startup（`app.py:321`）| optional title | `SQLiteSession` 对象 | 先 `list_sessions()` 再条件 `create_session()`——两步独立事务 | 抛 `SQLiteSessionError` |
| Test helpers | `tests/test_web_sessions_sqlite.py` 等 | 测试 | TestClient + lifespan | HTTP response | 同 HTTP 入口 | — |

#### 1.1.1 隐式创建路径检查

**结论**：无隐式创建。

- `/api/prompt` / `/api/prompt/async` 在 `_validate_prompt_payload()`（`app.py:1153` 附近）**显式校验** `session_id` 存在性——不存在 → `PromptValidationError(404, "session {id} not found")`。
- 没有「Session 不存在则创建」的回退逻辑。
- 唯一的"自动"创建在 lifespan startup 时的 `ensure_default_session()`，只在启动时跑一次。

### 1.2 Session Store 内部

**SQLiteSessionStore**（`session_sqlite.py`）：

- Connection：`self._db`（aiosqlite.Connection，在 `init()` 中创建，`isolation_level=None` autocommit 模式）
- INSERT 位置：`session_sqlite.py:353-373 create_session(title, metadata)`
- COMMIT 时机：INSERT 立即 autocommit（line 369 附近）
- session_id 生成：**INSERT 前**由 Python 生成（`session_sqlite.py:123-125`），格式 `sess-{timestamp_ms}-{uuid4_hex[:8]}`
- 跨连接可见性：其他连接在 COMMIT 后立即可见（SQLite WAL 默认）

#### 1.2.1 Connection 布局（re-confirmed）

| Store | Connection | 事务模型 |
|---|---|---|
| SessionStore | 独立 aiosqlite.Connection | autocommit + 显式 `BEGIN IMMEDIATE` 用于并发 message append |
| ExtensionStore | **共享** SessionStore 的 connection（`:memory:` 模式必须）；文件模式可独立 | 共享连接意味着共享事务 |
| ProviderConfigStore（E2-1）| **独立** aiosqlite.Connection（`provider_config_store.py:517`，`isolation_level=None`）| autocommit + `BEGIN IMMEDIATE` 写事务 |
| CredentialStore（E1）| **独立** aiosqlite.Connection | 同上 |

**关键事实**：ProviderConfigStore 与 SessionStore **使用同一个 SQLite 文件，但不同 connection**。SQLite 标准不支持跨 connection 事务——一个 connection 的 COMMIT 无法在另一个 connection 中回滚。

### 1.3 Session 删除路径

**HTTP 入口**：`DELETE /api/sessions/{sid}`（`app.py:2360-2411 delete_session`）

| 清理对象 | 清理方式 | 状态 |
|---|---|---|
| session row | `DELETE FROM sessions WHERE id=?` | EXPLICIT |
| messages | FK `ON DELETE CASCADE` | CASCADE（schema 层）|
| snapshots | FK `ON DELETE CASCADE` | CASCADE |
| revisions（`web_message_revisions`）| FK `ON DELETE CASCADE` | CASCADE（`extension_store.py:280`）|
| files / file metadata | `file_store.delete_session_files(sid)` | EXPLICIT |
| uploaded file content（文件系统）| FileStore 内部目录移除 | EXPLICIT |
| active request registry | `state.active_request_by_session.pop(sid, None)` | EXPLICIT |
| current session reference | `state.current_session_id = None`（仅当指向该 sid）| EXPLICIT |
| WebSocket event buffer | 不按 session 清理 | NOT CLEANED（设计；buffer 有 maxlen 自然淘汰）|
| request_registry 全局 | 通过 `active_request_by_session` 间接清理 | EXPLICIT |

#### 1.3.1 删除行为

- **幂等性**：删除不存在的 Session 返回 **404**（`app.py:2380-2383`）；HTTP 入口严格
- **部分失败**：文件删除失败 **不阻塞** SQLite 删除（`app.py:2390-2394` 文件 cleanup 包在 try/except）
- **内部调用**：可直接调 `store.delete_session(session_id)`（`session_sqlite.py:414-420`）——不强制走 HTTP
- **底层语义**：Store 层 `delete_session` 在 session 不存在时抛 `SessionNotFoundError`；CASCADE 由 SQLite FK 保证

#### 1.3.2 关键问题：内部 helper 是否可用作补偿？

**结论**：可用，但需注意：
- 调用 `store.delete_session(sid)` 抛 `SessionNotFoundError` 表示「已被并发删除」——补偿调用必须吞掉此异常（补偿本就是 best-effort）
- 文件清理失败不阻塞——补偿时即便文件系统出错，Session row 仍会被删
- 不存在「误删其他 Session 资源」风险——CASCADE 是 `WHERE session_id = ?` 精确匹配

### 1.4 创建副作用时序

| 序号 | 副作用 | 可撤销？ | 客户端可见时机 |
|---|---|---|---|
| 1 | Python 生成 `session_id` | 否（仅本地变量）| 不可见 |
| 2 | INSERT session row | SQLite COMMIT 前可 ROLLBACK | 不可见 |
| 3 | COMMIT（autocommit）| **不可逆** | **其他 connection 立即可见** |
| 4 | HTTP response 构造 | n/a | 不可见 |
| 5 | HTTP response 发送 | 不可逆 | 客户端收到 session_id |

**关键观察**：
- 步骤 1-3 之间**无任何 await**——单条 INSERT 在 SQLite 内原子
- 步骤 3（COMMIT）到步骤 5（HTTP 响应）之间是「窗口期」——Session 已对其他连接可见，但客户端尚未收到 id

**无以下副作用**：
- ❌ 无 WebSocket 事件广播（无 `session_created` 事件类型）
- ❌ 无 `app.state.current_session_id` 更新（仅 startup / delete 时改）
- ❌ 无 harness / agent state attach
- ❌ 无 file 目录创建（按需创建于首次上传）
- ❌ 无内存 registry 注册

## 2. 并发可见性审计

### 2.1 Race A：Session 创建未响应，另一请求已读到

**触发条件**：另一请求如何获得 `session_id`？

- **HTTP 客户端**：必须等 HTTP response——窗口期不可见
- **WebSocket 客户端**：无 `session_created` 事件——不可见
- **`GET /api/sessions`** listing：可在窗口期读到该 Session（autocommit 后立即可见）
- **服务端内部并发**：理论上可读到（如另一协程正好 list sessions）
- **客户端预生成 id**：理论可能，但客户端无途径预先知道 id（`sess-{ts}-{uuid4_hex[:8]}` 不可预测）

**评估**：HTTP 客户端层面**无窗口期风险**。内部并发理论上存在但极罕见（需要预知 session_id）。

### 2.2 Race B：Session 已 commit，Binding 失败，补偿尚未完成

**窗口期内**：
- **Prompt 入口**：可发（Session 存在），但若补偿先完成则 Prompt 后续操作失败
- **文件上传**：可发（无 session 存在性预检？需在 E2-3 实施时验证）
- **Session listing**：**可见**——窗口期内 `GET /api/sessions` 列出该 Session
- **request_registry**：未被引用（Prompt 尚未发）

**评估**：窗口期长度 ≈ Binding 操作耗时 + 补偿删除耗时 ≈ 几十毫秒。**Medium risk**——需要补偿删除快速且可靠。

### 2.3 Race C：客户端在创建过程中断开

**ASGI cancellation 行为**：
- FastAPI / Starlette 触发 `asyncio.CancelledError` 投递到 coroutine
- INSERT 已 autocommit → 不可逆，Session row 留存
- 后续代码（Binding 写入 / HTTP response 构造）被取消
- **代码库内未发现 `asyncio.shield` 使用**——补偿逻辑若被取消则不会完成

**评估**：**Low-medium risk**。Session 创建本身快速（毫秒级 INSERT + COMMIT），客户端断开窗口短。但若在「Binding 写入中」断开，补偿必须用 `asyncio.shield` 保护，否则会留下孤儿 Session。

## 3. 三方案评估

### 3.1 方案 A：Session 创建 → Binding 初始化 → 失败时补偿删除

```
1. store.create_session()                # SQLite COMMIT
2. initialize_default_binding(session_id) # 可能失败
   ↓ failure
3. compensate: store.delete_session()    # 必须可靠
4. 返回固定错误响应
```

**优势**：
- 与现有 Session 创建语义一致（INSERT 是原子点）
- 现有 `delete_session()` 内部 helper 可用作补偿
- 不需新事务模型
- 补偿失败可观测（log + 监控）

**风险与缓解**：

| 风险 | 缓解 |
|---|---|
| 补偿删除被 CancelledError 中断 | 用 `asyncio.shield` 包住补偿调用 |
| 补偿删除失败（极罕见，如 SQLite 文件 corrupt） | log critical，返回固定错误；运维介入清理孤儿 |
| 窗口期内 Session listing 可见孤儿 | 接受（短窗口）+ 错误响应让客户端重试 |
| 文件上传到窗口期 Session | 文件 upload endpoint 已校验 session 存在；若补偿先完成则 upload 返回 404；窗口期内 upload 成功会创建文件，补偿时由 `delete_session_files` 清理 |

**实施约束**：
- 补偿调用必须 idempotent（吞 `SessionNotFoundError`）
- 补偿调用必须 `asyncio.shield` 保护
- 补偿失败必须可观测（log + 计数）

### 3.2 方案 B：先 Binding 后 Session

```
1. 查询默认 Profile
2. 生成 session_id
3. 写入 Binding（session_id 暂不存在）
4. 创建 Session
```

**不可行**——多个阻断：

1. `SessionModelBinding.session_id` 在 E2-1 schema 中是 PRIMARY KEY 但**不**对 `sessions` 表建 FK（设计决策：避免 Provider Config schema 与 Session Store 耦合）。但 Service 层 `set_session_binding` 强制校验 session 存在。
2. 若跳过 Service 校验直接写 Store：Binding row 可能指向永不存在的 Session（若步骤 4 失败）→ 孤儿 Binding
3. 孤儿 Binding 比 orphan Session **更难发现**——Session listing 不会显示它，但下次用户访问该 session_id 时会 404
4. 现有 Store 的 `upsert_binding` 通过 FK 仅校验 `profile_id` 存在，不校验 session——意味着孤儿 Binding 技术上可写入

**结论**：拒绝方案 B。

### 3.3 方案 C：Lazy Materialization

```
1. 创建 Session，不写 Binding
2. 首次 GET /api/sessions/{sid}/model-binding 时，若无 Binding 且有默认 Profile → 创建
3. 首次 Prompt 时同理
```

**拒绝**——违反 design doc §4.2：

- 全局默认在创建时**快照化**——之后修改默认 Profile 不影响已创建 Session
- Lazy 方案下，创建后修改默认 Profile 会让未使用的 Session 拿到**新**默认（违反语义）
- 首次 GET 有写副作用（违反 REST 语义预期）
- 并发首次访问需 CAS（复杂度激增）

**结论**：拒绝方案 C。

## 4. 选定策略

### **Chosen strategy: A — 创建 Session → 初始化 Binding → 补偿删除**

**理由**：
1. Session 创建副作用极简（仅 INSERT，无 WS / 内存状态 / harness attach）
2. 现有 `delete_session()` 内部 helper 可用作补偿
3. 补偿路径可观测（log / metrics）
4. 不需修改 Core Runtime 或 Session Store 架构
5. 不需新增跨 Store 事务层（SQLite 不支持）

## 5. E2-3 实施细节（待 E2-3 编码时落地）

### 5.1 Integration point

`POST /api/sessions` handler（`web/app.py:2305 post_sessions`）在 Session COMMIT 后、HTTP response 前调用：

```python
# 伪代码——E2-3 落地
session = await state.session_store.create_session(title, metadata)
try:
    if provider_config_service is not None:
        await provider_config_service.initialize_new_session_binding(session.id)
except DefaultBindingFailed:
    # 补偿——asyncio.shield 保护
    await asyncio.shield(_compensate_delete(state, session.id))
    raise SessionCreationRollbackError()
except asyncio.CancelledError:
    # 客户端断开——仍要尝试补偿
    await asyncio.shield(_compensate_delete(state, session.id))
    raise
return session
```

### 5.2 Compensation function

```python
async def _compensate_delete(state: WebAppState, session_id: str) -> None:
    """Best-effort compensation. Idempotent. Logs critical on failure."""
    try:
        await state.session_store.delete_session(session_id)
    except SessionNotFoundError:
        # 已被并发删除——幂等成功
        pass
    except Exception:
        # 补偿失败——log critical，运维介入
        logger.critical(
            "compensation delete failed; orphan session remains",
            extra={"session_id": session_id},
        )
```

### 5.3 Cancellation handling

- 补偿调用必须 `asyncio.shield` 包裹
- CancelledError 必须重新抛出（不吞）
- 补偿内部的 `SessionNotFoundError` 吞掉（幂等）

### 5.4 Visibility window

- **接受**：补偿进行中的几十毫秒内，`GET /api/sessions` 可能短暂列出孤儿
- **接受**：客户端可能在窗口期内成功 upload 文件（补偿会清理）
- **缓解**：补偿调用应在 `except` 块内**同步**触发，不引入额外 await

### 5.5 initialize_new_session_binding 内部逻辑

```
查询 is_default=True AND enabled=True 的 Profile
  → 不存在：直接返回（不抛异常，Session 仍正常创建）
  → 存在：
      profile_id = profile.id
      model_id   = profile.default_model
      source     = "default"
      调用 store.upsert_binding(...)
```

**关键不变量**：
- 默认 Profile 不存在时 **不阻塞** Session 创建
- Binding 内容只写：`session_id` / `profile_id` / `model_id` / `source="default"`
- **不复制**：`provider_id` / `credential_id` / `masked_value` / `validation_status` / `base_url`

### 5.6 错误语义（HTTP response）

固定业务错误（不暴露 SQLite / Session row / Credential id / model_id / 文件路径）：

| 业务错误 | HTTP code | 触发条件 |
|---|---|---|
| `provider_config_unavailable` | 503 | ProviderConfigService 未初始化（应用启动未完成 / 配置错误）|
| `default_binding_failed` | 500 | Binding 写入失败（非并发、非取消）+ 补偿成功 |
| `session_creation_rollback_failed` | 500 | Binding 失败 + 补偿也失败（**HIGH severity**，阻塞 E2-3 freeze）|
| `session_not_found` | 404 | 一般性查询不存在 |
| `profile_deleted_during_binding` | 409 | 默认 Profile 在初始化过程中被并发删除（罕见；按 `profile_in_use` 反向处理）|

**禁止**：
- HTTP response body 不含 SQLite 错误文本
- 不含 Session row dump
- 不含 Credential ID
- 不含 model ID
- 不含文件路径
- 不含内部 transaction 状态

## 6. 必需测试（E2-3 实施时落地）

| # | 测试 | 期望 |
|---|---|---|
| 1 | 默认 Profile 存在 + enabled → 创建 Session | Binding 写入（source=default，profile_id + default_model）|
| 2 | 默认 Profile 不存在 → 创建 Session | 无 Binding；Session 正常 |
| 3 | 默认 Profile disabled（auto-clear 已触发）→ 创建 Session | 无 Binding；Session 正常 |
| 4 | ProviderConfigService 未挂载（旧 App 配置）→ 创建 Session | Session 正常；不抛 provider_config_unavailable |
| 5 | Binding 写入失败（注入 Store 异常）→ 补偿删除触发 | 返回 default_binding_failed；Session row 不存在 |
| 6 | 补偿删除失败（注入 SessionStore 异常）→ log critical | 返回 session_creation_rollback_failed；Session 留存（运维清理）|
| 7 | 客户端断开（CancelledError 注入）→ 补偿仍运行 | asyncio.shield 保护；Session row 不存在 |
| 8 | 补偿删除幂等（Session 已被并发删除）| 吞 SessionNotFoundError；不抛 |
| 9 | 并发：Session 创建中 + 另一请求 list_sessions | 窗口期内可能列出（接受）；创建完成后一致 |
| 10 | 修改默认 Profile 后再创建 Session | 新 Session 拿到新默认；旧 Session 不变 |

## 7. Composition / 生命周期指针（E2-3 实施时落地）

### 7.1 Lifespan 接线位置

现有 lifespan（`web/app.py` 内嵌 `_lifespan`）顺序：
1. SQLiteSessionStore init
2. ensure_default_session
3. ExtensionStore init
4.（未来）CredentialRuntime init（E1）
5. **（E2-3 新增）** ProviderConfigStore.open(db_path) + ProviderConfigService 构造
6. app.state 挂载
7. yield（服务运行）
8. shutdown：反向 close

### 7.2 最小接线清单

| 接线点 | 文件 | 修改 |
|---|---|---|
| 打开 Provider Config Store | `web/app.py` lifespan | `await SQLiteProviderConfigStore.open(db_path)` 与 SessionStore 同一文件 |
| 构造 ProviderConfigService | 同上 | DI 注入 store + ProviderRegistry（E1 单例）+ CredentialService（E1）+ session_exists 包装 |
| `session_exists` callback | 同上 | 包装 `state.session_store.get_session(sid) is not None` 为 async callable |
| 挂载到 `app.state` | 同上 | `app.state.provider_config_service = svc` |
| Router include | `web/app.py create_app` | `app.include_router(build_provider_profiles_router(svc))`（E2-3 新增）|
| 关闭 Store | lifespan shutdown 反向 | `await provider_config_store.close()` |

### 7.3 部分初始化失败 rollback

E2-3 lifespan 应使用 `AsyncExitStack`（与 E1-4A CredentialRuntime 同模式）：
- 任一初始化步骤失败 → 自动 close 已 enter 的资源
- 不留下半初始化 runtime
- `app.state.provider_config_service` 仅在全部成功后赋值

## 8. 残余风险（接受）

| 风险 | 严重性 | 缓解 |
|---|---|---|
| 补偿删除 + 补偿失败 → 孤儿 Session | HIGH（但极罕见）| log critical；运维 runbook 清理；E2-4 freeze 前验证测试覆盖 |
| 窗口期 Session listing 短暂含孤儿 | LOW | 接受；窗口 < 100ms |
| 客户端在窗口期 upload 文件 | LOW | 补偿 `delete_session_files` 清理；客户端可重试 |
| SQLite 文件 corrupt 导致补偿失败 | LOW | 运维介入；与 Session 自身失败语义一致 |

## 9. 阻塞条件检查（spec §10）

| # | 条件 | 是否满足 | 证据 |
|---|---|---|---|
| 1 | Session 创建响应在 Binding 成功前不会返回 | ✅ | HTTP response 在 Binding 之后构造（§5.1）|
| 2 | Session ID 不会在创建响应前通过事件暴露 | ✅ | 无 WS 事件；`app.state.current_session_id` 不更新（§1.4）|
| 3 | 现有内部 delete 可以安全、完整、幂等清理新 Session | ✅ | CASCADE + 显式 cleanup；内部 helper 可调（§1.3）|
| 4 | Binding 失败时可以调用该 delete | ✅ | `store.delete_session(sid)` 可直接调用（§5.2）|
| 5 | Cancellation 路径能保证 rollback/compensation | ✅（with mitigation）| `asyncio.shield` 包裹补偿（§5.3）|
| 6 | 删除失败可以检测并报告 | ✅ | log critical + 固定错误响应（§5.2/§5.6）|
| 7 | 不需要修改 Core Runtime | ✅ | 不动 `loop.py` / `agent.py` / `harness.py` |
| 8 | 不需要重构 Session Store | ✅ | 复用现有 `delete_session` + CASCADE |

**全部条件满足**。

## 10. 最终状态

> **状态**：AUDIT COMPLETE — integration strategy approved

- **Chosen strategy**：A（Session 创建 → 初始化 Binding → 补偿删除）
- **Integration point**：`POST /api/sessions` handler，Session COMMIT 后、HTTP response 前
- **Compensation function**：`_compensate_delete(state, session_id)`——idempotent、`asyncio.shield` 保护、log critical on failure
- **Cancellation handling**：`asyncio.shield` 包裹补偿；CancelledError 重新抛出
- **Visibility window**：接受（< 100ms）；窗口期 listing/upload 可能短暂看到孤儿
- **HTTP error mapping**：固定 5 个业务错误码（§5.6）；不暴露内部状态
- **Required tests**：10 项（§6）；E2-3 实施时落地

### 10.1 E2-3 实施前置条件

- ✅ Session 创建 / 删除调用链已审计
- ✅ 补偿策略已选定
- ✅ 错误码已冻结
- ✅ 测试矩阵已定义
- ✅ Lifespan 接线位置已定位

**可进入 E2-3 编码**。

### 10.2 E2-3 显式不包含

- ❌ 不修改 `provider_config_store.py`（E2-1 已冻结）
- ❌ 不修改 `provider_config_service.py`（E2-2 已冻结；仅在 Service 内新增 `initialize_new_session_binding` 方法，**仍属 E2-3 范围**——属 Service 扩展不属 Store 改动）
- ❌ 不重构 `web/app.py`（仅最小接线：lifespan + 1 router include + 1 helper 函数）
- ❌ 不修改 Core Runtime / Agent Harness
- ❌ 不实现 E3 Provider 切换
- ❌ 不实现前端选择器（E2-4 / E4）

### 10.3 审计完成后的待办（不阻塞 E2-3 启动）

- E2-3 实施时需补一项测试：`tests/test_web_sessions_sqlite.py` 新增 "default binding initialization" 子集
- E2-3 实施时需验证：文件 upload endpoint 是否真的不预检 session 存在性（spec §5.5 假设）
- E2-4 freeze 前需验证：`session_creation_rollback_failed` 计数为 0（或可接受的低频）
