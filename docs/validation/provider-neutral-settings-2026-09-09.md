# Provider 统一配置验证（2026-09-09）

## 用户确认范围

- 不按 Z.ai/GLM、Qwen、Kimi 等厂商分区，统一管理 Profile。
- 表单：名称、API 协议（OpenAI-compatible / Anthropic-compatible）、Base URL、Model ID、凭证；保留启用、默认与会话绑定操作。
- 移除 Context window / Max output tokens 表单输入，不删除后端预算功能或已有 override。
- 旧 Profile、Credential、Session binding ID 和 Keyring 引用不变；不自动更改真实服务地址、轮换密钥或执行付费探针。

## 实现与兼容

- Profile schema v1 → v2：验证旧结构后，在单事务内追加可空协议/地址列；旧记录按原 registry 默认值投影，不重建表或迁移密钥。
- 新 Profile 使用两个通用协议 ID；已有厂商 ID 留作兼容身份，允许编辑有效协议与地址。
- 请求选定后冻结协议、地址、模型与凭证引用；表单修改只影响后续请求。
- HTTPS 或明确 loopback HTTP；拒绝 userinfo、query、fragment、控制字符、非法端口等地址。前端在凭证变更之前预检，后端独立校验；SDK 不跟随重定向，保留 TLS 验证。
- 自定义端点不继承原厂凭证验证状态、静态模型建议或模型窗口 override。内部使用端点/协议作用域，消息与 API 保留原 Provider 身份。
- 历史没有可证明的端点来源：对自定义端点保守剥离输入/输出 thinking 签名、丢弃不透明 redacted 块，保留可读内容与普通事件生命周期，避免切换地址后跨服务重放签名；旧官方同源行为不变，不改已有历史。这也意味着同一自定义端点不保留签名重放能力。
- 未知上下文窗口显示 unknown，不臆造窗口或百分比。自动压缩的百分比阈值需要已知窗口；原有后端元数据接口及逐调用门禁保留。
- 保存不测试连接；实际模型请求会把对应 API Key 和请求内容发送至用户配置的地址，UI 明确提示信任边界。

## 验证记录

- 前端全量：36 files / **259 tests passed**；lint、类型检查通过。
- Python `ruff check src tests scripts evals` 通过；`mypy --platform linux src evals` 与 `--platform win32`：各 **309 source files** 通过。
- Provider / Credential 扩大回归：**891 passed / 1 skipped / 1859 deselected**，328.52 秒，`.t/provider-stable`。
- 后补 API 公开身份 / 端点隔离 / 运行时组合：**41 passed**，`.t/endpoint-final`。
- 最终历史签名双向隔离 / 预算 scope / 既有 ModelClient、Web 压缩组合：**45 passed**，17.59 秒。后补批次不累加到 891，也不称为全项目后端全量。
- Chromium：**28 passed**，2.8 分钟，`--retries=0`，隔离端口 8141；包含统一 Profile 新建、编辑、选择、刷新持久化和凭证 ID 不变。测试结束恢复 production build。
- 首次 Chromium 在受限沙箱中因 `spawn EPERM` 无法启动；获准后重新执行上述 28 项完整通过。遗留测试端口 8139 与正常测试端口 8141 均已退出。
- 全量前端首轮仅旧 context tooltip 文案断言失败，更新为“窗口未知、不臆造百分比”后 259 项重跑通过；未删除功能断言。

## 本机加载

- 已以原有 Keyring / Local Docker 配置重启业务后端，8000 与前端 5173 正常监听，Keyring write probe 通过。
- 浏览器已打开新版登录页。应用重启按原策略撤销旧登录；需用户重新登录后查看现有 Profile，未绕过认证。
- 本轮未更改真实 Profile 服务地址、密钥、默认选择或 Workspace 的 Sandbox 选择。

## 验证边界

- 使用临时 SQLite、合成凭证、Mock/Fake Provider；不调用真实模型，不修改业务密钥。
- 本轮是 Provider 配置链路验证，不等于 Worker、真实 MinerU、真实模型或 Docker 的重新验收。
- 本机此前 GLM 认证失败是独立连接问题；本次 UI/端点能力不证明现有 Key 或服务地址已可用。
