# Project Plan

The original Step 1–21 implementation plan has been completed and archived:

- [Archived Step 1–21 Plan](docs/archive/legacy-plans/PLAN_STEP_1_21.md)

The original Web Claude P0 plan is also archived:

- [Archived Web Claude Plan](docs/archive/legacy-plans/WEB_CLAUDE_PLAN_ORIGINAL.md)

> Both archived documents are retained for historical reference and are no longer the source of truth.

## Current project documents

- [Current Status](STATUS.md) — HEAD, latest tag, test baseline, frozen phases, blockers
- [Roadmap](ROADMAP.md) — **P1-E Multi-Provider Switching（M1 / M2 / M3 milestone）**，P1-F，P2 candidates
- [Changelog](CHANGELOG.md) — released versions
- [Current TODO](TODO.md) — current phase execution checklist（M1-0~M1-7 + M2 + M3）
- [Architecture docs](docs/architecture/) — runtime, web request lifecycle, persistence, regenerate model
- [API reference](docs/api/web-api.md)
- [Testing guide](docs/guides/web-testing.md)

> 当前本地基线主线：**P2-C Context Budget + Compaction UI** 已实施；底层 P2-B、P2-A、P0-AGENT-RUNTIME、P2-CHECKPOINTER、P2-AUTH、P2-SESSION-WORKSPACE 与 P1-E Provider Runtime 保持有效。
> 每个真实 LLM 调用前按 canonical messages、完整 system prompt、Skills、Tool schemas 与附件估算上下文；70% warning、85% 建议压缩、95% 阻止发送。模型窗口未知时明确显示 unknown，不伪造百分比。
> Provider Profile 可持久化 `context_window` / `max_output_tokens`；消息显示 Provider usage 与生成延迟；用户可按完整 Turn 安全压缩，摘要进入 canonical history 并在刷新后恢复。
> 高风险 ToolCall 可暂停并在当前 Turn 显示脱敏 Approval Card；Approve once 只批准该次调用，Deny/abort/shutdown 不执行工具；浏览器刷新恢复等待卡，后端重启不持久化审批。
> Session 切换同步 `/chat/{session_id}`；整页刷新恢复精确 Session、历史、`AGENT.md`、`Memory.md`、文件树及运行中 Prompt/Regenerate，非法/越权 ID 安全回退，登出不跨账号串状态。
> 当前首个命令 `/checkpointer` 把本 Session 对话累计总结到 `Memory.md`，文件提交后清空消息，失败保留并回滚；后续请求自动加载该记忆。
> 实施状态与验证基线见 [STATUS.md](STATUS.md)，后续优先级见 [ROADMAP.md](ROADMAP.md)。
