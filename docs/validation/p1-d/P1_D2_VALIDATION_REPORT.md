# P1-D2 Regenerate Validation Report

> **阶段**: P1-D2（Regenerate——非破坏性最新回答重生成）
> **状态**: ✅ FROZEN（按既定 tag 策略，D2/D3 合并到 `v0.0.27-product-actions`，**不打独立 tag**）
> **HEAD**: `d53f331`
> **完成日期**: 2026-07-16

## 1. 范围

支持用户对最新 assistant 回答触发 Regenerate，**非破坏性**地在原 message_id 位置替换内容；流式期间旧回答始终可见；error / abort / server restart 都不能让 session 进入"既无旧回答也无新回答"的中间态。

**核心模型**：`messages` 表是 active canonical source；`web_message_revisions` 表存历史与生成尝试。详见 [Regenerate Revision Model](../../architecture/regenerate-revision-model.md)。

## 2. 实现阶段

| 阶段 | Commit | 内容 |
|---|---|---|
| D2 准备工作 | `9264267` | 设计定稿 + gitignore dev DB |
| D2-1 | `564c3f5` | Diff-based `replace_messages`——历史 message ID + created_at 稳定 |
| D2-2 | `e25b319` | `web_message_revisions` schema + migration v1→v2 |
| D2-3 | `813831d` | Revision repository（CRUD + finalize + 状态机） |
| D2-4 | `260bbff` | Execution/persistence split + Harness canonical reset |
| D2-5 | `72fa2f4` | Regenerate HTTP API + request metadata + 错误映射 |
| D2-6 | `9b6db3d` | Startup sweep + Web PersistedMessage DTO |
| D2-7 | `2175faa` | Frontend regenerate flow + message_id reconciliation |
| D2-8 | `484c338` | E2E isolation fix + Regenerate E2E + Freeze |
| Freeze 补齐 | `d53f331` | Next-prompt-after-regenerate 后端集成测试 |

## 3. 最终冻结证据（HEAD `d53f331`）

### Coverage

- **84.20%** ≥ 75% PASS（1131 passed, 14 deselected）
- 关键模块：`loop.py` 81% / `agent.py` 79% / `harness.py` 76% / `context.py` 78% / `events.py` 100% / `stream_events.py` 100%
- D2 改动模块：`web/app.py` 80% / `web/extension_store.py` 86% / `session_sqlite.py` 96%

### Production Hooks Scan

- 构建命令：`npm run build`（非 `build:e2e`）
- 输出位置：`src/pi_agent_core_py/web/static/assets/`
- `__storeHooks`: **0 occurrences** in production JS
- `__e2eHooks`: **0 occurrences** in production JS
- 门控：`main.ts:27-28` `import.meta.env.DEV || VITE_E2E_HOOKS === "true"`，production build 被 tree-shake 干净

### Core Runtime Diff（`9264267..d53f331`）

**零修改** ✅：以下文件未出现在 diff：
- `loop.py`
- `agent.py`
- `context.py`
- `providers/`（整个目录）
- `events.py`
- `stream_events.py`
- `mcp/`（整个目录）
- `tools/`（整个目录）
- `skill_loader.py`

允许的 Web/持久化层改动：
- `web/app.py` / `web/extension_store.py` / `web/serializers.py` / `web/state.py` / `session_sqlite.py`

### Git 状态

- `git status --short`：clean
- `git diff --check`：无空白错误

### Ruff

- `ruff check src tests scripts`：All checks passed

## 4. E2E 测试基线

**37/37 PASS**（28 既有 + 9 Regenerate 新增）

### Regenerate 实际新增 9 个 test case

| # | describe | test | Playwright # |
|---|---|---|---|
| 1a | Regenerate 1: button visibility | 最新 persisted assistant 显示；历史不显示 | 19 |
| 1b | Regenerate 1: button visibility | active request 期间不可用 | 20 |
| 2 | Regenerate 2 | 流式期间旧回答保留 | 21 |
| 3 | Regenerate 3 | 同 message_id 就地更新 | 22 |
| 4 | Regenerate 4 | Abort 保留旧回答 | 23 |
| 5 | Regenerate 5 | **WS reconnect recovery**（**不是完整 reload**） | 24 |
| 6 | Regenerate 6 | 双击只一个 request | 25 |
| 7 | Regenerate 7 | Export 不含 revision 元数据 | 26 |
| 8 | Regenerate 8 | 下一轮用新 active answer | 27 |

### Test #5 范围声明

`regenerate.spec.ts:243-290` test #5 仅覆盖 **WebSocket reconnect recovery**。**完整浏览器 reload 后恢复原 session 依赖 URL routing，当前未实现**——列入 [Known Limitation](../../../STATUS.md#已知限制)，P2 处理。

### Test #8 断言强度（freeze 补齐 @ `d53f331`）

E2E test #8 验证 persisted assistant 数 = 2 + 第一个 `message_id === originalId`。

后端集成测试 `test_next_prompt_after_regenerate_sees_b_not_a` 直接观察 `FakeProviderAdapter.all_messages_calls[-1]`，断言：
- ✅ 包含 B（regenerate 后的 active answer）
- ✅ 不包含 A（superseded）
- ✅ B 位于 user C 之前
- ✅ assistant turn 不重复（只有 1 个）

## 5. 关键架构决策

1. **`completed` ⟺ `active`**：去掉 `is_active` 字段，用 `status='completed'` + partial unique index 表达 active
2. **Revision 0 延迟创建**：只在第一次 regenerate 成功 finalize 时归档旧 active
3. **流式期间不写 DB**：delta 只存在前端 chatStore + WS 事件流
4. **Finalize 单 transaction**：5 步原子切换，step 3（旧→superseded）必须先于 step 4（running→completed）
5. **`base_content_sha256`**：创建 revision 时的原始 `content_json` UTF-8 SHA-256（**禁止** json.loads/dumps 重序列化）；用作 optimistic concurrency token
6. **`_reset_harness_to_session`**：所有 regenerate 路径退出都调，恢复 SQLite canonical context
7. **不调 `replace_messages`**：`_persist_regeneration_result` 只调 finalize——保持 message_id 稳定
8. **不实现 revision history UI / drawer**：D2 显式不做
9. **不实现 `revision_finalized` WS event**：polling + reconcile 已足够

## 6. 已知限制

- **不支持完整浏览器 reload 恢复 active Regenerate draft**——列 P2 URL routing
- **不支持手动切换历史 revision 为 active**——D2 显式不做
- **不持久化 "regenerated" badge**——PersistedMessage DTO 无对应字段
- **revision history UI / drawer 不实现**——D2 显式不做
- **仅支持最新 assistant regenerate**——不支持历史 message regenerate

## 7. 显式不做

- ❌ Revision history UI / drawer
- ❌ 手动切换历史 revision 为 active
- ❌ 持久化 "regenerated" badge
- ❌ `revision_finalized` WS event
- ❌ 完整浏览器 reload 恢复（P2 URL routing）
- ❌ 历史非最新 assistant regenerate

## 8. 相关文档

- [Regenerate Revision Model](../../architecture/regenerate-revision-model.md)——长期架构
- [Archived P1-D2 Design](../../archive/superseded-designs/P1_D2_REGENERATE_DESIGN.md)——完整设计过程与决策点
- [Persistence and Startup](../../architecture/persistence-and-startup.md)——schema 与 migration
