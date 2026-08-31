# pi `ai` 模块对齐报告（2026-08-31）

## 范围

本轮只处理用户指定的第 1 项 `ai`：模型身份、消息边界和 Provider 调用。
对照源为 `D:\LLMTutorial\pi\pi-main\packages\ai`；本项目实现位于
`src/pi_agent_core_py/{messages,llm_messages,model_client,providers}`。`agent`、
`coding-agent`、SQLite session backend 和 telemetry 按后续阶段分别处理。

## 差异与处理结果

| 契约 | 对照前问题 | 本轮结果 |
|---|---|---|
| 模型身份 | `ModelClient` 没有从 Adapter 取得 `api_id`，切换 Provider 时无法准确判断 reasoning signature 是否可回放 | Adapter/Client 统一公开 provider + API + model；Anthropic Factory 保留真实 provider id |
| 历史转换 | 各 Adapter 自行转换，跨 Provider 会把 Anthropic signed/redacted thinking 原样发给别的模型 | 新增共享不可变转换；仅 exact model 回放签名，跨模型明文 thinking 降为 text、redacted 丢弃，error/aborted assistant 不进入请求 |
| 工具历史 | ID 未按目标协议统一规范；孤儿 result 或缺失 result 会让严格 Provider 拒绝整个上下文 | call/result 使用同一 ID 映射；过滤孤儿 result，在下一条普通消息前补 synthetic error result；Anthropic 非法 ID 使用稳定 SHA-256 ID |
| 多模态 | 核心只有文本和 FileBlock，Provider adapter 无图片协议 | 新增 raw-base64 + MIME 的 `ImageContent`；Anthropic/OpenAI 分别生成原生图片结构；不支持视觉时合并为明确占位，Context Budget 计入 payload |
| Usage | 只有 input/output/total，缓存与 reasoning 不可观测，cost 没有扩展位 | 增加 cache read/write、1h write、optional reasoning 与 optional cost；OpenAI 将 cached tokens 从非缓存 input 中拆出，Anthropic total 纳入缓存 token |
| 重试 | SDK 重试关闭后没有统一替代；限流或建连抖动直接失败 | `ModelClient` 提供有界指数退避、Retry-After 上限和 abort；只重试首事件前的 rate-limit/network failure，部分输出后不重放 |
| 错误映射 | Anthropic SDK 异常依赖通用兜底，瞬时/鉴权/协议错误无法稳定分类 | Anthropic 与 OpenAI 均映射为安全的 authentication/rate-limit/protocol/stream 错误，不暴露响应正文 |

## 保留边界

- 本轮没有复制上游数十个 Provider/API；当前产品仍只接 GLM、Anthropic、Qwen、Kimi。
- `Usage.cost` 在缺少可信、版本化模型价格时保持 `None`，不会把“未知价格”伪装成零成本。
- AI 核心已支持图片，但现有 Workspace 附件仍按 FileBlock 注入；“上传图片直接进入视觉模型”的产品接线属于后续 `coding-agent` 组装阶段。
- 远端模型目录、动态价格表和 Provider fallback 没有在没有产品决策的情况下引入。

## 验证

专项测试覆盖 exact/cross-model thinking、redacted block、图片支持/降级、图片预算、
tool-call 修复、Usage 分解、重试成功、部分输出禁止重试和 API identity。

| 门禁 | 结果 |
|---|---|
| AI/Provider/Compaction 邻接 | 159 passed |
| Backend 全量 | 2121 passed、7 skipped、9 deselected；coverage 77.13% ≥ 75% |
| Ruff | `ruff check src tests scripts` PASS |
| strict Mypy | 187 source files / 0 issues |
| Frontend | Vitest 183/183；typecheck、ESLint、production build PASS |
