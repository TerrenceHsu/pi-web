# Durable Operation / Recovery 设计

> 状态：实现基线；2026-08-20。当前把通用 lane operation 原语落到
> SQLite，并用 `/checkpointer` 覆盖第一个真实跨 SQLite/文件系统事务。

## 不变量

1. 外部 effect 开始前先持久化 `operation_started`；记录固定 `lane`、
   immutable `source_leaf_id`、operation kind 与 secret-free dedupe payload。
2. operation record 只追加，不原地改写。一个 lane 最多有一个没有
   `operation_finished` 的 operation；相同 kind + dedupe key 的重试复用原 intent。
3. 外部 effect 已发布后追加 `effect_committed`。需要移动 lane 的终态变更与
   `operation_finished(completed)` 必须在同一个 SQLite 事务提交。
4. recovery 不调用 Provider、不猜测内存状态，只使用 operation records、
   immutable source leaf 和外部持久证据。
5. source leaf 已变化时不得清空新消息；operation 终止为 `conflict`，现有
   conversation 与已经发布的外部文件都保留供审计/后续显式处理。

## SQLite 数据模型

- `session_operations`：operation identity、Session/lane、kind、dedupe key、
  source leaf、接受时 payload 与时间。
- `session_operation_records`：按 Session 单调递增的 append-only record：
  `operation_started`、`effect_committed`、`operation_finished`。
- `start_operation()` 在一个 `BEGIN IMMEDIATE` 中校验 lane 的唯一 open
  operation，并插入 operation + start record。
- `complete_operation_and_reset_lane()` 校验 lane 仍位于 source leaf，随后在
  同一事务把 leaf 移到 root、刷新 active `messages` 投影并追加 completed。

operation payload 不保存 Provider credential、完整 prompt、完整消息或生成正文；
Checkpointer 只保存 source SHA-256、消息数、逻辑文件路径和文件 ID/SHA-256。

## Checkpointer 提交与恢复

正常路径：

```text
SQLite: operation_started(source leaf/hash)
Provider: generate cumulative Memory text
Files: publish immutable content generation + atomically replace metadata pointer
SQLite: effect_committed(file id/hash)
SQLite transaction: lane leaf -> root + messages projection -> empty
                    + operation_finished(completed)
```

启动时在工作区接受请求之前扫描 open checkpointer operations：

| 持久状态 | 恢复动作 |
|---|---|
| 没有匹配 source hash 的 `Memory.md`，也没有 effect record | `aborted`；消息保持 |
| `Memory.md` source hash 匹配，source leaf 未变化 | 补 effect record（如缺失），原子完成并清空原 lane |
| effect record 与文件证据矛盾 | `conflict`；消息保持 |
| `Memory.md` 匹配，但 source leaf 已变化 | `conflict`；新旧消息全部保持 |

同进程重试也复用该 reduction：文件已发布但 SQLite 收尾失败时不再反向覆盖或
删除 `Memory.md`，重试凭 source marker 前滚完成，不再次调用 LLM。

## 文件与旧 JSON Session

- `VirtualFileStore.update_text()` 不再依次覆盖正文和 metadata。新正文写入
  immutable generation，`metadata.json` 通过同目录 temp + replace 作为唯一
  commit pointer；旧 generation 在提交后清理，重启会修复旧协议遗留的
  backup/hash mismatch 并清理孤儿 generation。
- `JsonFileSessionStore` 保留 `{session_id}.json` 路径兼容，但新格式是
  header + append-only snapshot records。每次 save 只 append 一行并 flush/fsync；
  malformed final record 作为 torn tail 截断，中间损坏拒绝加载。
- 旧单对象 JSON 可直接只读加载；第一次 save 用 temp + replace 一次性迁移为
  journal，之后不再整文件覆盖。

## 明确边界

- 本项没有承诺跨重启继续正在进行的普通 Prompt、Provider stream、ToolCall 或
  Human Approval；这些仍是单独的 runtime durability 设计。
- 当前自动 reducer 只识别 `checkpointer` kind；SQLite operation API 可供后续
  compaction/navigation 使用，但未知 kind 不会被启动流程擅自结束。
- 进程崩溃级 durability 使用 flush/fsync 和 SQLite WAL；没有声明对磁盘控制器
  断电缓存提供额外硬件级保证。
