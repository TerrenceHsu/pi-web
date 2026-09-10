# OpenAI-compatible 工具流修复（2026-09-09）

## 故障与根因

Web 会话使用 OpenAI-compatible Profile 时显示 `provider connection failed`。
本机 OpenAI Python SDK 2.43.0 的 `chat.completions.stream()` 自动解析 helper
在 HTTP 发送之前拒绝非 strict function tools；项目工具保留普通 JSON Schema，
由自身的工具缓冲与校验层负责解析，不满足该 helper 的额外约束。
普通只读聊天也会装配 `read_tool_output`，因此并非仅主动调用工具时才受影响。

原适配器将该本地 `ValueError` 映射为可重试的连接错误，掩盖了根因。
本轮没有据此判定用户的 API Key 无效，也没有修改服务地址或切换模型。

## 修改

- 改用 `await chat.completions.create(stream=True, ...)`，直接转换原始 chunk。
  不强行给工具添加 `strict=true`，不改变既有参数 schema。
- 保留文本、reasoning、分片工具调用、usage-only、缺失 usage 与结束原因处理。
- 使用原始 AsyncStream 的异步上下文关闭响应；保留协作中止、任务取消传播、
  借用 client 不关闭、自有 client 幂等关闭。空 choices 的 usage chunk 也检查中止。
- 本地参数/解析异常和通用 SSE 错误改为固定脱敏、非重试的协议错误。
  认证、限流、网络/传输故障、HTTP 408/5xx 保留原有分类；
  HTTPX 的本地协议/不支持协议异常不冒充可重试网络故障。
- 请求参数构造进入脱敏异常边界；首事件前有限重试、已输出后禁止重试、
  SDK `max_retries=0`、TLS 校验与禁重定向保持不变。
- 测试替身改为原始流契约，增加真实 SDK + HTTPX MockTransport 回归，
  避免只模拟 helper 接口而漏掉 SDK 自身的工具校验。

本轮使用 openai-docs 指引核对官方流式 API，再结合本机已安装 SDK 的
`_streaming.py` 确认响应关闭行为，未升级运行环境或更改锁文件。
官方接口参考：[Chat Completions API](https://developers.openai.com/api/reference/cli/resources/chat/subresources/completions)。

## 验证

- 修复前：新增 non-strict 工具用例先失败，真实 SDK 在请求前报错。
- 修复后最终适配器专项：**95 passed**，2.54 秒，临时目录 `.t/raw-stream-final`。
  其中真实 SDK 离线回归 **27 项**，涵盖正常请求、碎片工具、reasoning 扩展字段、
  usage、length、signal/task/consumer 关闭、HTTP/SSE 安全错误及 ModelClient 重试边界。
- 扩大 Provider / ModelClient / Web Prompt / Regenerate 回归：
  **505 passed / 1 skipped**，143.15 秒，`.t/raw-stream-reg`。
  该批次收集早于最后四个补充用例；与 95 项专项存在重叠，不累加或宣称全项目全量通过。
- `ruff check src tests scripts evals` 通过。
- strict Mypy：Linux / Windows 各 **309 source files** 通过。
- `git diff --check` 通过；两位并行审查分别验证真实 SDK 契约和资源/错误安全边界。

复跑专项（PowerShell，项目根目录）：

```powershell
$env:PYTHONPATH = 'src'
$env:PYTHONDONTWRITEBYTECODE = '1'
D:\miniconda\envs\pipy\python.exe -m pytest tests/test_openai_compat_stream.py tests/test_openai_compat_tools.py tests/test_openai_compat_errors.py tests/test_openai_compat_security.py tests/test_openai_compat_real_sdk.py --basetemp=.t/raw-stream-check --no-cov -q
```

## 本机加载与边界

- 核实原后端 PID 30296 独占本机 8000 端口后，使用原启动配置重启为 PID 37660。
  日志：`.run-logs/backend-20260909-043755-a2e78a1d/`，Keyring write probe 通过。
- `127.0.0.1:8000` 正常监听；未登录访问 `/api/sessions` 返回预期 401，
  前端 `127.0.0.1:5173` 返回 200。旧登录按现有重启策略失效，需重新登录。
- 保留原 Provider、凭证、业务会话、Sandbox 选择与 Docker 服务。
- 测试全部使用合成输入、假凭证与离线 MockTransport；没有发送付费模型请求。
  因此当前真实 Provider 的认证、套餐使用范围及模型响应仍需独立确认。
- 本轮只修复适配器阻断及错误分类；重新生成路径的
  `regeneration produced no qualified assistant candidate` 外层错误展示尚未单独改造。
- 未重新运行前端全量、MinerU、真实 Docker 或付费模型验收；未提交或推送 Git。
