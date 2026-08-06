# P2-R2-C4-R Investigation Status — BLOCKED

> 报告日期：2026-08-05  
> HEAD：`f211bd2`

## 当前状态：BLOCKED — 不能确定 root cause

C4-R 已完成项目 ✅：
- [1] 15 失败锁定（13 + 2，2 次连续跑 deterministic）
- [3] G1 stash 等价性审计（patch-id + blob hash 全一致，CONTENT EQUIVALENCE CONFIRMED）
- [4] 第 3 个 skip 归档（3 个 skip 全部预期，文档化）

C4-R 未完成项目 ❌：
- [2] 5 种 ordering 对照（部分完成，未确定污染源）
- [6] 资源释放审计（已识别 4 个 store leak 模式，但与 pytest.raises 失败的因果链未建立）
- [7] 完整套件 0-fail × 2
- [5] C4-R freeze 报告

## 核心阻塞：pytest.raises 不捕获的机制 UNEXPLAINED

### 现象

15 个失败测试**全部**用 `pytest.raises(X)`，traceback 显示 X 确实被 raise，但 `pytest.raises` 没捕获。HTTP 层失败测试拿到 500 而非预期 415/409。

### Native traceback 关键发现

```
File "...pytest_asyncio/plugin.py", line 905, in inner
    runner.run(coro, context=context)
File "...asyncio/runners.py", line 118, in run
    return self._loop.run_until_complete(task)
File "...asyncio/base_events.py", line 691, in run_until_complete
    return future.result()
File "...tests/test_upload_api.py", line 284, in test_streaming_enforces_max_size
    await upload_service.upload_stream(
File "...upload_service.py", line 539, in _stream_to_staging
    raise UploadTooLargeError(...)
pi_agent_core_py.web.knowledge.upload_service.UploadTooLargeError: ...
```

- pytest-asyncio 1.4.0 使用 `asyncio.Runner.run(coro, context=context)` 模式
- `context=context` 传递 `contextvars.Context`
- 异常从 `_stream_to_staging` 一路传播出 `await`，**逃逸了 `with pytest.raises` 的同步 CM**
- 同样测试在 isolation / 小 subset 全部 PASS

### 已排除假设

1. ❌ **filterwarnings 配置** — pyproject.toml 无 filterwarnings，warning 不升级为 error
2. ❌ **类身份不匹配** — 测试和源码都从 `pi_agent_core_py.web.knowledge.upload_service` 导入 `UploadTooLargeError`，同一 class object
3. ❌ **嵌套 pytest.raises** — 测试只有一层 `with pytest.raises`
4. ❌ **anyio 冲突** — `-p no:anyio` 后仍 16 failed（多 1 个，非根因）
5. ❌ **subset 复现** — 单文件 / 多文件 subset 都 PASS，污染仅在完整套件累积

### 未排除假设

- 累积 aiosqlite thread 泄漏 → asyncio 基础设施退化 → exception propagation 在 contextvars/Runner 边界失效
- 但无法构造实验直接证明

## 已识别 leak 源（可修复但非确定根因）

### 5 个 SQLite store 的 `open()` 模式

| Store | open() 异常时是否 close conn？ |
|---|---|
| `KnowledgeStore` | ❌ NO（`_initialize_schema` 抛异常时 conn 泄漏） |
| `SQLiteCredentialStore` | ❌ NO |
| `ExtensionStore` | ❌ NO |
| `SessionStore` | ❌ NO |
| `SQLiteProviderConfigStore` | ✅ YES（`try/except BaseException: await conn.close()`） |

修复：复制 `SQLiteProviderConfigStore.open` 的 try/except 模式到其它 4 个 store。  
影响：仅消除 schema-init 失败路径的 thread 泄漏（~3-5 个 thread per suite）。**不足以解释 Thread-1100**。

## 推荐下一步（需用户决策）

### 选项 A：盲修 + 重跑（pragmatic 但违反 "ROOT CAUSE PROVEN" 原则）

1. 修复 4 个 store.open() 加 try/except close 保护
2. 跑完整套件 2 次
3. 若 0 fail → 假设成立，C4-R 通过；若仍 fail → 深入调查

**风险**：若 fix 无效，浪费 1-2 套件运行（每次 4-6 分钟），仍无 root cause。

### 选项 B：深度调查 pytest-asyncio + contextvars 交互（求真）

1. 写一个最小复现：在 suite 上下文中跑 `async def test_X(): with pytest.raises(Y): await raises_Y()`
2. 用 `--pdb` drop in 失败点检查 `sys.exc_info()`、`contextvars` 状态
3. 检查 pytest-asyncio 1.4.0 changelog 是否有相关 regression
4. 若证明是 pytest-asyncio bug → 降级或绕过

**时间成本**：高（可能 1-2 小时调试）。但能真正建立因果链。

### 选项 C：升级 / 降级 pytest-asyncio 看是否绕过

1. 试 pytest-asyncio 0.21.x（成熟稳定版，使用旧 runner）
2. 跑完整套件验证
3. 若 0 fail → 锁定 pytest-asyncio 1.4.0 为问题源

**风险**：可能引入新问题；违反"不修改依赖" 默认。

### 选项 D：暂停 C4-R，回到用户决策

报告当前发现，让用户判断走 A/B/C 或其它路径。  
**当前推荐**。

## 已交付文档

- `docs/validation/p2-r2/C4_R_SKIP_AUDIT.md` — 3 个 skip 完整归档
- `docs/validation/p2-r2/C4_R_RESOURCE_AUDIT.md` — 资源审计（已识别 + 未确定）
- `docs/validation/p2-r2/C4_R_INVESTIGATION_STATUS.md` — 本文档
