# 不可变 Sandbox 输出制品

输出制品是验证门禁与本机 Publisher 之间的唯一数据边界。Publisher 不读取仍在运行的 Sandbox，也不接受 Agent 提交的文件清单、验证结果或哈希。

## 冻结顺序

Coding/Plan 的 `SandboxOperation.freeze_output_artifact()` 只使用 operation 内部保存的最后一次成功固定验证证据，并按以下顺序执行：

1. 重新校验 `.pi-agent/sandbox.toml`、workspace revision 和完整工作区摘要。
2. 将 operation 标记为 frozen。此后 `run`、验证、write、patch 和 delete 全部返回固定错误 `operation_frozen`。
3. 服务端生成带随机 artifact ID、可信 baseline 和内部 validation evidence 的受限导出请求，并通过 SHA-256 固定请求内容。
4. 固定远端 helper 使用逐层 `O_NOFOLLOW` 的目录描述符扫描工作区，在 `/tmp` 创建确定性、未压缩 tar；它不会在 `/workspace` 内生成 cache 或制品文件。
5. 下载时 provider 校验预期 SHA；下载完成后服务端独立重算 archive size/SHA，并逐个读取 tar member 验证结构、配额、路径、内容 SHA 和二进制分类，全程不解压。
6. 服务端再次检查配置 SHA 和远端完整工作区摘要。只有仍与 validation evidence 相同才会签名。
7. 已验证 archive 写入 `artifacts/<archive_sha256>.tar` 内容寻址路径，并返回不可变 `SandboxOutputArtifact` receipt。

导出开始后采用 fail-closed 语义：即使下载、校验或签名失败，operation 仍保持 frozen，不能继续修改后复用旧验证证据。需要修改时必须销毁该 Sandbox 并从可信项目快照创建新 operation。

## 独立 Bash 与恢复（阶段 3）

只有服务端构造的独立 Bash operation 能使用 `pi-agent-bash-artifact/v1`，证据为
`bash-output-integrity/v1`。证据绑定获准 Session/request/task/scope、脚本 SHA、cwd、实际成功命令 ID / result SHA、
原 Workspace revision/tree SHA、冻结副本摘要与发布策略；它没有 `passed` / `checks`，不宣称功能验证。
Coding/Plan 无论最后执行哪个工具，都不能改用此证据。schema 与证据类型不匹配会被拒绝。
失败、无变更、受保护输出或链接/特殊文件不生成可发布制品；复制回主 Workspace 之前仍须单独确认。

回收计算资源与保留制品分离：私有 SQLite 保存服务端签名的恢复胶囊，绑定签名制品与原 baseline。
它不保存可执行句柄或重放许可。资源已确认回收的冻结制品可在重启后继续审阅；回收未确认则 fail closed。

## Archive v1（Coding 与 Bash 共用成员布局）

固定成员如下：

- `metadata/manifest.json`：规范 JSON manifest，自带 `manifest_sha256`。
- `metadata/validation-evidence.json`：按 manifest schema 选择固定验证证据或独立 Bash 输出完整性证据。
- `metadata/binary-diff.json`：二进制新增、替换和删除的 before/after size 与 SHA；新增或修改后的原始字节仍位于 `files/`。
- `metadata/deleted-files.json`：删除路径、baseline size/SHA 和二进制分类。
- `files/<relative-path>`：所有新增及修改文件的精确字节；未包含未变文件和已删除文件。

Manifest 固定 `artifact_id`、`operation_id`、workspace revision、baseline SHA、冻结工作区 SHA、validation evidence SHA、changed/deleted/binary counts 和 payload 总字节数。路径必须是已规范化相对 POSIX 路径，大小写折叠后也不得重复。Archive 不允许目录、链接、设备、额外 member 或未声明 payload。

文本/二进制分类是确定性的：包含 NUL 或不是严格 UTF-8 的内容视为 binary。修改文件只要 before/after 任一侧为 binary，就进入 binary diff。Publisher 无需应用不可信的二进制 patch；它使用 manifest 中的 hashes 和 `files/` 中的完整 replacement bytes。

## 服务端签名

`HMACSHA256ArtifactSigner` 接受服务端注入的 `key_id` 和至少 32 字节 secret。Secret 不写入请求、Sandbox、archive、receipt、日志或 `repr`。签名覆盖固定 context、算法、key ID、签名时间、最终 archive SHA 和 manifest SHA。

生产 Web 生命周期必须从 SecretStore/OS Keyring 注入稳定 secret；不能使用仓库文件、SQLite 明文、项目配置或 Agent 输入。当前核心包不自行创建或持久化密钥，避免越过应用的凭证所有权边界。真实 E2B smoke 使用进程内随机临时密钥，仅验证导出与签名协议；Web 接入和持久化恢复属于 TODO 第 9 项。

消费方必须调用 `verify_output_artifact()`：它会重新计算本地 archive size/SHA、完整验证 archive，并验证 server signature。仅校验 receipt 中的字符串、仅信任文件名或只检查 tar manifest 都不构成有效验证。

## 与 Publisher 的边界

TODO 第 8 项的本机事务 Publisher 只能接收已通过 `verify_output_artifact()` 的 `SandboxOutputArtifact`。发布时仍需重新验证本机项目 baseline SHA、目标路径与 reparse point，并通过 journal/backup/replace 实现事务；冻结和签名不等同于授权发布，也不允许 Sandbox 直接挂载真实工作区。
