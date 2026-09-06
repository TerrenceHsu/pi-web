# 会话只读历史与结构化 Memory（阶段 1–4）

日期：2026-09-05。用户批准先实现方案第 1–4 节；自动压缩、压缩摘要器与 token 策略另行规划。

## 1. 职责与权限

- SQLite 的 immutable `session_entries` 保存原始证据；旧分支/压缩前记录仍可回查。
- Agent 通过 `search_session_history` / `read_session_history` 读取当前 Session，不能提交 SQL、
  数据库路径、账号、Workspace 或 Session ID。
- 文件型 Web Session 默认装配这两个核心只读工具，不需要打开可选分析工具或 MCP。
  `:memory:` 嵌入模式不装配数据库历史工具，不伪装为文件级只读连接。
- 后端固定数据库路径，Session 从请求 ContextVar 取得。外层 Auth gateway 的账号数据库隔离保持不变。
- `SQLiteHistoryReader` 每次使用 URI `mode=ro`、`query_only=ON` 和短读事务；不使用
  `immutable=1`，因为 Web 写连接仍在持续更新同一 WAL 数据库。
- Agent 工具只能查询允许的消息正文；thinking、工具参数、附件字节、自定义 entry、配置/凭证表、
  snapshot 与内部操作日志不对模型开放。常见凭证格式输出时脱敏，但不承诺识别任意敏感文本。
- 这是工具通道权限，不是操作系统沙箱；此前用户批准的本地 Python 仍以本机用户权限执行，
  不应通过 Python 绕过历史工具读取数据库。

## 2. 检索与恢复

核心实现：`src/pi_agent_core_py/session_backends/sqlite/history.py`。

- writer 在启动时以事务建立、回填正文投影 `session_history_text` 及 FTS5 索引；触发器与消息写入、
  删除共用原事务。中断回滚后可以重试，不会把半成品索引当成完成状态。
- SDK 的原有跨 Session typed search 保留兼容契约，但其索引初始化也移到 writer 启动；查询不再建表。
  面向 Agent 的正文投影与通用 SDK payload 搜索分离，不能通过 SDK 搜索直接绕开 Session 范围。
- 三字符及以上使用 trigram FTS；一至两个字符（例如“记忆”）在当前 Session 范围内用固定
  `instr` 查询回退。SQL 内先绑定权限范围，绝不把跨 Session 结果查出后才过滤。
- 搜索参数：`query` 最多 200 字符，`limit` 默认 5、最大 20，`before_seq` 游标；按序号倒序。
  每条片段最多 800 字符，返回 `next_before_seq`。
- 原文参数：`entry_id`、`offset`、`max_chars`（默认 6000、最大 12000）；返回 `next_offset`、
  父节点和至多 10 个子节点 ID，邻接读取仍须通过相同权限检查。
- SQLite 执行设置约 2 秒进度截止、1 秒锁等待与取消检测，超时/存储失败返回稳定错误而非物理路径。
- 返回 `entry_id / seq / role / timestamp / text / branch_status`，不返回 raw JSON。

`branch_status=active` 表示仍在当前活动分支；`archived_or_superseded` 明确表示可能是压缩前历史、
旧回答或其它分支，不能据此宣称结论仍有效。当前不改压缩模块来增加更细的历史覆盖映射。

## 3. 每轮结构化记忆

沿用现有 `auto_memory` 生命周期：一次用户请求及其工具调用结束、消息持久化后，再提炼记忆。
主回答已落库；记忆仍由该请求生命周期托管，不新增独立后台服务。默认 Web 入口已开启，Knowledge
Conversation 继续跳过；库嵌入者仍须显式开启。

- 先把本轮 canonical 消息绑定到 immutable entry ID；异步 Web 请求同时保存真实 request ID。
  老记录、同步请求或崩溃恢复时无法可靠确定 request ID 就保留空值，不把下一次请求 ID 冒充来源。
- 模型只收到既有 Memory 和有界本轮证据，直接返回 `MemoryDelta` JSON；这一调用不启用
  Agent loop、Tools、MCP 或 Skills。正常提炼一次模型调用。
- 允许六种条目：goal、decision、constraint、achievement、task、reference。
- `no_change=true` 不改 Memory 文件、不增加 Workspace revision；通过操作日志确认该轮已处理，
  下一轮不会反复摘要同一轮。effect 落库后中断也可恢复这一 no-op。
- 模型每次最多提出 12 项变更、单项 600 字符、至多 8 个来源。标识、时间戳、来源绑定及发布均由服务端负责。
- 原文检索工具结果、summary 和 custom entry 不是新证据；该轮若检索了历史，也不把随后的助手转述
  作为独立新证据，防止循环提炼。
- JSON/schema/来源/容量校验失败，保留旧文件及待处理 operation；不把正常回答倒退成失败。
  原有 Sandbox 待审批延迟、取消和 pending evidence 续接机制保持。

## 4. 来源、修正和文件契约

`Memory.md` 是唯一记忆事实文件，不另建一套 Memory 数据库。新正文包含
`pi-structured-memory/v1` 标记、可读条目，以及紧邻条目的托管元数据注释：

- 稳定 `mem-*` ID、kind、status、pinned、updated_at。
- source entry ID、已知 request ID、消息角色；不重复保存原始证据正文，按需通过历史工具回查。
- 外层仍沿用 `pi-checkpointer` 来源 hash 与提交标记；文件发布使用已有 SHA 乐观锁和版本机制。

合并规则：

1. 同一事实/同一输入重复处理是 no-op，不重复追加。
2. 目标、决策、约束要求引用用户消息；成果要求引用受支持执行工具的非错误结果，不能仅靠助手自述。
3. 更正已有条目要求 `replaces` 和本轮用户原文中的 `correction_quote`；旧项保留为 superseded，
   新项带自己的来源。语义是否真正蕴含该结论仍依赖模型判断和人工复核，来源校验不是形式化事实证明。
4. 用户可在现有 Workspace 文件编辑器改 Memory：改动托管条目的可见正文会将其视为用户固定条目，
   清除不再匹配的旧来源。自动更新不能覆盖它；需要再次通过用户编辑更正。
5. 旧版 Memory 与托管条目之外的手工笔记原样保留为 User notes，不由模型擅自重写。
6. 每次上下文装配重新检查来源是否在当前分支；离开活动分支的条目投影为 pending_confirmation，
   superseded 项不注入模型。不会因为 Regenerate 或换分支而继续无标记地使用旧答案。

当前用 16,000 字符文件安全上限保证托管结构能被现有读取器完整读取；容量不足显式返回
`memory_capacity_exceeded` 并保留旧文件，不截断条目、不淘汰用户固定内容。过大的旧文件返回
`legacy_memory_requires_review`。精细 token 分配、记忆淘汰/再摘要与自动压缩属于下一阶段，未实现。

原有 `/checkpointer` 入口保留：已有结构化 Memory 时采用相同校验和渲染，不退回无来源覆盖；
旧版空白/非结构化 Session 仍兼容原流程。阶段 1–4 当时未修改压缩；后续实现见
[Web Context Compaction](context-compaction.md)，原始历史和 Memory 的上述边界保持不变。

## 5. 使用

重启本机 Web 后端即可加载。正常聊天自动维护 Memory；可以要求 Agent：

- “查一下之前为什么决定使用 MinerU，给出历史来源。”
- “回顾前面确认过的执行约束，先查原文。”
- “把之前的某项约定改为……”，由增量记忆记录有来源的更正。

纠正/固定事实使用现有 Workspace 的 Memory.md 编辑入口，不需要新前端页面。
前端补充 `continuity.status=no_change` 契约；终态同步还会按原始顺序补回已持久化但因刷新遗漏的
工具结果，保留其详情、已挂载卡片身份及正在保存的界面状态，避免重复卡片。
扩展配置独立加载，不再阻塞刷新后恢复正在执行的请求。
没有新增自动压缩开关或记忆管理面板。

验证记录见 [`session-history-memory-2026-09-05.md`](../validation/session-history-memory-2026-09-05.md)。
