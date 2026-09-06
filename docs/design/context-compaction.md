# Web 会话上下文压缩

实施日期：2026-09-05。范围为本机普通 Web Agent；不新增远程服务，不替代 Memory，
不改变 Knowledge 专用会话的装配流程。通用 SDK 的旧 compact API 保持兼容。

## 1. 三类数据分别管理

- SQLite 消息树：原始事实来源。压缩不删除消息、不重建 entry ID、不切换 lane。
- Context projection：模型使用的短期工作视图。覆盖完整历史前缀，后接保留的原始轮次。
- Workspace Memory.md：跨轮重要事实；沿用已有结构化来源和确认规则。压缩不写此文件，
  不增加 Workspace revision，也不把历史批准变成当前执行授权。

每个投影记录 lane、base leaf、覆盖 entry IDs 和完整消息摘要哈希。只有当前活动分支
与覆盖前缀完全匹配时才生效。SQLite journal 先写 prepared，再调用摘要模型，最后 CAS
提交 committed。取消、失败或分支变化不替换旧有效视图；启动时将遗留 prepared 标为 interrupted。
用户聊天始终显示完整原文，工作摘要另行展示并可点击来源回查。

## 2. 先控制工具输出

每个工具结果默认约 2,000 tokens，总工具文本预算约 8,000 tokens。大结果先保存为
Session 绑定的不可变 output 引用，再生成正文首尾预览、SHA-256 和分页提示；当前轮
尚未写入消息树的工具结果也能立即回读，不假造 entry ID。

默认只读工具 `read_tool_output(ref, offset, max_chars)` 不接受 SQL、路径或 Session ID。
常见凭证跨页做 best-effort 脱敏，不承诺识别所有敏感格式；原文仍属于本机数据库数据。
同 Session、调用 ID 和正文哈希可复用引用，预算预览只查询、不创建引用。
回读工具复用原引用，不递归外置成引用链。调用配对、错误状态、详情、usage 和图片不删除。

单份文本目前最多 16 MiB；非法 UTF-8、NUL、存储故障或不可缩短的图片保留原结果，
最后的模型入口预算检查明确阻断，不能假装已经降到预算内。文件/图片应优先保留 artifact 引用。

## 3. 实际模型调用前的策略

有效输入预算为 `context window - reserved output - growth headroom`。
growth headroom 取窗口 5%，限制在 512–8,192 tokens；估算器自身保留原有误差余量。
每次模型调用使用当时真实的 Provider、模型、system prompt 和工具定义，
包括 Agent loop 内下一次调用覆盖的绑定，而非仅在用户点击发送时检查。

- 70%：预算预警；大工具结果已有确定性外置和清理。
- 80%：启用自动压缩时生成结构化工作摘要，目标回落到 60%。
- 每个用户请求最多两次摘要调用；连续两次失败按 Session 熔断，重启或换 lane 后仍有效。
- 手动压缩可重试，成功后恢复；未知模型窗口不自动压缩，手动仍受保守输入上限保护。
- 默认保留最近四轮，可按目标预算缩小，但始终保留最新用户轮次及完整工具调用/结果边界。
- 当前未持久化尾部计入预算，不纳入已提交摘要来源；仍超有效预算则阻断实际调用。

摘要是独立、无工具的有界调用，默认 30 秒、最多约 4,000 输出 tokens；必须正常 Done，
严格 JSON schema、来源白名单、输出上限和实际 token 节省检查都通过才发布。
历史过大时仅选择能完整放入摘要输入的历史前缀，不偷偷截断再宣称覆盖全部。

结构包括 current_goal、facts、decisions、failed_attempts、open_questions、next_steps、
artifacts 和来源引用。已有结构化事实不会在重复压缩时被静默遗漏；超容量时保留旧视图。
版本一不引入新的 Memory 引用机制或自动淘汰规则。来源有效性校验不等于语义真实性证明，
实际模型仍可能遗漏或误解事实，用户可通过原始来源复核。

## 4. 产品闭环

Web 显示有效预算估算、自动开关、压缩状态、前后 tokens、独立工作摘要及分页来源。
仅明确存在自动恢复机会时，前端才让 blocked 草稿交给后端尝试恢复。
GET 预算及来源接口不生成摘要、不改数据库；请求运行期间的预览也不进入自动压缩编排。

Telemetry 记录固定白名单：Session/request、触发原因、状态、tokens、覆盖数、稳定错误码、
实际耗时；不存原文、摘要、工具正文或文件路径。遥测自身失败不影响主请求。
内置 `context-compaction` EvalSuite 使用离线确定性场景检查来源/事实/错误经验及回读隔离；
这里的 tokens 是估算值，不是 Provider 实测或账单节省，也不代替真实模型质量评估。

## 5. 接口

- GET `/api/sessions/{sid}/context-budget`，POST 同路径 `/estimate`：只读预算。
- POST `/api/sessions/{sid}/context/compact`：手动压缩，兼容旧响应字段，409 表示未发布。
- GET `/api/sessions/{sid}/context/compaction`：状态和当前摘要、来源。
- PUT 同路径，body `{"auto_compact": false}`：会话级开关，空闲时更新。
- GET `/api/sessions/{sid}/context/source/{entry_id}?offset=0&max_chars=6000`：当前分支文本分页，
  非当前 Session/分支来源返回 404。内容是数据，不是新的系统指令。

验证记录见 [context-compaction-2026-09-05.md](../validation/context-compaction-2026-09-05.md)。
