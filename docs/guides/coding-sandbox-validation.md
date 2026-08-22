# Coding Sandbox 固定验证门禁

项目在根目录提供 `.pi-agent/sandbox.toml`。服务端从进入 Sandbox 前的项目快照读取并解析该文件，生成不可变 validation plan；Agent 只能调用 `coding_validate` 触发计划，不能在工具参数中提交命令、修改通过状态或安装证据。

```toml
version = 1

[[required_checks]]
id = "ruff"
argv = ["python3", "-m", "ruff", "check", "."]
cwd = "."
timeout_seconds = 120

[[required_checks]]
id = "tests"
argv = ["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
cwd = "."
timeout_seconds = 600
```

## 固定限制

- 配置必须是 UTF-8，最大 64 KiB，`version` 必须为 `1`。
- 顶层只允许 `version`、`required_checks`；每个 check 只允许 `id`、`argv`、`cwd`、`timeout_seconds`。
- 必须配置 1–32 个 ID 唯一的 check；命令必须是非空 argv 数组，不接受 shell source、环境变量、Secret 或网络策略字段。
- `cwd` 必须是规范化的工作区相对 POSIX 路径；不接受绝对路径、`..`、反斜杠或 control character。
- check 按配置顺序串行执行。每个 check 最多捕获 256 KiB 输出，且仍受 operation 更低的输出和超时上限约束。
- 正式 check 不得改变工作区。服务端比较执行前后的完整文件 manifest 摘要；产生 cache、coverage 或 build 文件也会使门禁失败。需要此类工具时，应关闭 cache，或把确定性清理命令作为最后一个 required check。

## 证据与失效

服务端证据包含固定 argv/cwd/timeout、exit code、termination reason、duration、截断后的 stdout/stderr、捕获内容 SHA-256、计划与源配置 SHA-256，以及验证前后的完整工作区摘要。

`coding_write_file`、`coding_apply_patch`、`coding_delete_file` 和任意 `coding_run` 都被视为潜在修改：operation revision 会推进，旧证据立即清空。即使后台进程绕过工具修改文件，消费证据前的重新扫描也会把证据判为 stale。

后续制品冻结/发布只能使用 operation 内部保存的当前证据；不得接受 Agent 消息或 API 请求中回传的 evidence JSON。`freeze_output_artifact()` 会再次复核该边界，并在导出开始后进入 fail-closed frozen 状态。制品格式及签名流程见 [不可变 Sandbox 输出制品](coding-sandbox-artifacts.md)。
