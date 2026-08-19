# Append-only Session Tree 设计

> 状态：实现基线；2026-08-20。本文聚焦 Session tree / lane 语义；durable
> operation 已见 [`DURABLE_OPERATIONS.md`](DURABLE_OPERATIONS.md)，turn-boundary
> compaction 仍是后续独立事项。

## 决策

采用 pi-agent Harness v2 的 lane 思路：一个 Session 包含不可变 entry 树，
每个 lane 持久化自己的 active leaf。`main` 是默认 lane；调用方可以从任意
entry 创建 fork、移动 lane leaf（branch）并给 entry 写 label。

现有 `messages` 表暂时保留，作为当前 active lane 的兼容投影。Web、regenerate、
revision 和 export 继续读取这一投影；`session_entries` 才是分支历史的事实源。
这样既不把历史 entry 原地改写，也不破坏现有稳定 `message_id` 契约。

## 数据模型

- `session_entries`：`(session_id, seq, id, parent_id, message_id, role,
  content_json, created_at)`；entry payload 永不 UPDATE/DELETE（删除整个 Session
  时由外键级联除外）。
- `session_lanes`：`(session_id, name, leaf_entry_id, created_at, updated_at)`；
  lane leaf 是可移动指针，`main` 不可删除。
- `session_facts`：append-only 的 `(session_id, seq, kind, entry_id, value_json,
  created_at)`；label 以同一 entry 最新 seq 为准，写 `null` 表示清除。
- `session_operations` / `session_operation_records`：lane operation identity 与
  append-only intent/effect/finish log；tree 仍只保存 conversation。
- `sessions.active_lane`：当前兼容投影对应的 lane；旧库迁移时默认 `main`。

旧库首次打开时按 `messages.idx` 建 parent chain，并把 `main` leaf 指到最后一条；
空 Session 的 leaf 为 `null`。迁移幂等，不重写已有 entry。

## 公开语义

- `append_entry(message, lane=None)`：默认写 active lane；parent 为该 lane 当前
  leaf，插入后原子移动 leaf。对 active lane 同时更新 `messages` 投影。
- `branch(entry_id | None, lane=None)`：只移动指定 lane 的 leaf；目标必须属于
  同一 Session 且在该 lane 当前路径上。`None` 表示回到根。移动 active lane
  时在同一事务内重建兼容投影。
- `fork(name, at=None, source_lane=None, activate=False)`：创建新 lane；`at`
  默认取 source lane leaf，且必须在 source lane 路径上。原 lane 不移动；
  `activate=True` 才切换 active lane 并重建投影。
- `set_active_lane(name)`：选择已有 lane，并将它的 root-to-leaf 路径物化到
  `messages`。每条 entry 保留自己的 `message_id`，因此往返分支不会为同一
  历史节点生成新 ID。
- `set_label(entry_id, value | None)`：追加 fact，不覆盖旧 fact；读取返回最新值。
- `list_entries(lane=None)`：只返回指定 lane 当前 root-to-leaf 路径；
  `list_all_entries()` 用于审计完整树，包括不再活跃的旧分支。

## 兼容与安全边界

1. 旧的 `append_message` / `replace_messages` / `list_messages` API 默认操作
   active lane，不要求既有调用方理解树。
2. `replace_messages` 计算最长相同前缀；不同后缀只追加新 entry 并移动 leaf，
   旧分支不删除。同 role 内容修订继续保留兼容投影的 `message_id`，但创建新的
   immutable entry payload。
3. branch/fork 的 Session、lane、entry 归属全部在事务内校验，禁止跨 Session
   parent、环和悬空 leaf。
4. durable operation 只在 finish 需要移动 lane 时复用 tree 原子事务；operation
   log 不进入消息上下文。当前自动恢复覆盖 Checkpointer，普通 run/tool replay
   仍不在本文的 tree 契约内。

## 验收

- 新库、旧线性库迁移和重复 init 均得到唯一 `main` lane 与正确 leaf。
- fork 后两条 lane 可独立追加，公共前缀 entry ID 相同，分叉后互不污染。
- branch 到祖先、切 active lane、重启恢复后，消息投影与 active leaf 一致。
- label 历史 append-only，读取采用最新事实，清除不会删除历史。
- 旧消息 ID 稳定性、regenerate revision、Web Session 和 export 回归不退化。
