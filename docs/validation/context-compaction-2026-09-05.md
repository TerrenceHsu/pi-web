# Web Context Compaction 验收（2026-09-05）

## 范围

按持久工作视图 → 工具输出外置 → 自动结构化摘要 → Web/Telemetry/Evals 四步实施。
本次未提交 Git、未重启用户开发后端，不改用户已有工作区修改；没有调用真实付费模型。

## 核心验收

- source entry ID、完整聊天、lane、Memory 不因压缩而重建；CAS 冲突与重启恢复。
- 工具原文先持久化再限长、当前轮即时回读、跨 Session 禁止、preview 无写入。
- 真实 system/tools/Provider 预算、80% 触发和 60% 目标、每请求最多两次、持久 Session 级熔断。
- 未落库尾部参与预算但不能冒充摘要来源；工具预览明确标记未完整阅读。
- 结构化 JSON/source 白名单、无工具摘要流、输出上限、完整 Done、超时/取消、实际节省才提交。
- 工作摘要与聊天分离、自动开关持久化、原始来源分页、错误保留旧摘要。
- Telemetry 固定安全字段，Evals 离线执行来源/失败经验/回读隔离，不冒称实际模型质量或账单节省。

## 验证结果

- 基础投影、持久化、工具与转换边界第一轮：98 passed。
- 产品摘要服务最终专项：23 passed。
- 独立真实 Web 生命周期审查：5 passed（含摘要中取消、跨模型实际绑定、两次调用上限）。
- Web 手动压缩/重启/来源/开关及实际输出额度：3 passed。
- 最终压缩/回读/状态机/Web/Telemetry 聚合回归：136 passed，15.49 秒。
- Frontend 最终全量：218 passed；typecheck、lint 通过。
- Ruff：通过。strict Mypy：284 source files，通过。
- Evals 全部内置 suite：candidate gate PASS；产物 `.eval/context-compaction-full-gate`。
- `uv lock --check --offline`：通过，115 packages。
- 完整 Chromium：24/24 passed，1.6 分钟，`--retries=0`；测试后生产构建成功，截图已检查。
- 完整 Backend 最终：2378 passed、7 skipped、9 deselected，630.24 秒；coverage 77.19%，
  达到 75% 门槛。使用项目默认短 `.pytest-tmp`，无测试重试或放宽断言。

## 迭代发现与处理

1. 请求中预算预览不能进入自动编排 wrapper，明确选择纯预览变换；运行中端点原有 409 防护保留。
2. 自动摘要收到停止 signal 后抛出 CancelledError，原 Agent 请求 future 不会正常结束。
   已修 loop，将合作式停止转换为既有 aborted 生命周期；真实 Web 回归验证不再卡住。
3. Preview 没有 request ID，不能只按当轮 namespace 查工具引用。增加严格同 Session/call/bodySHA
   的只读复用，防止刷新后重新膨胀或每轮重复外置。
4. 输出预留增加实际 Provider 配置；Profile 预览读取公开工厂默认字段，不构建 Adapter 或读取 Secret。
5. 第一轮全量 Backend：2362 passed、7 failed、7 skipped、9 deselected，619.33 秒。
   七项失败均为 Wiki 文件落地：本次额外长 basetemp 令临时路径达 264 字符，超过本机旧式
   Windows 260 字符边界。恢复项目既有 `.pytest-tmp` 后相关 18 项全部通过（15.83 秒）；
   随后重新执行完整门禁。不修改 Wiki 产品代码，也不降低断言。
6. 首次浏览器启动受 sandbox 的 EPERM 限制；依规范提权运行，仅清理自身测试 session。
   一次重启曾与未释放的测试端口冲突，确认端口释放后重新运行；未停止用户开发服务。
7. 浏览器发现异步开关被立刻还原，修为请求处理中保持用户点击值、失败才还原确认状态；
   增加回归单测。最终专项 E2E 1 passed / 20.2 秒 / 零重试，截图检查通过。

## 已知边界

估算不是官方 tokenizer；模型摘要的语义真实性仍需人工/真实任务复核。
工具原文上限 16 MiB，凭证脱敏为 best effort，图片或最小证据仍超预算时直接阻断。
重复摘要保留旧事实，不自动淘汰；达到容量上限则失败保留旧视图。
未知窗口不自动压缩；Knowledge 专用会话与 SDK 旧 compact 不走普通 Web 新编排。
熔断按 Session 而非 lane，避免换分支绕过失败限制；成功手动重试可恢复。
