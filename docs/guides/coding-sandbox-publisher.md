# 本机事务 Publisher

`LocalTransactionalPublisher` 是不可变 Sandbox 制品与真实工作区之间唯一允许写入的边界。它只接收服务端持有的 `ProjectSnapshot`、签名后的 `SandboxOutputArtifact` 和对应 `ArtifactSigner`；Agent、Sandbox、Web 请求和制品内字段都不能直接提供本机目标哈希、备份路径或发布结果。

## Web Workspace 发布确认（阶段 3）

新的 Coding/Plan/Bash 操作在计算资源确认回收后才允许发布。Web 确认绑定 artifact ID、archive SHA 和
签名 review SHA；恢复胶囊还绑定 Session、purpose、scope、policy 与原始 baseline，不接受模型提供的发布清单。
独立 Bash 完整性证据不能作为 Coding 固定验证证据。界面把两者区分展示。

`WorkspaceSandboxArtifactPublisher` 先在隔离镜像里复用下面的 LocalTransactionalPublisher，再在
Session mutation lock 内复核制品签名/用途/当前策略、完整 Workspace 基线与保护路径/purpose，原子提交整个批次。
新的任务绑定制品即使只遇到未触及文件的 Workspace 升版，也返回冲突；不自动 rebase 或重新执行。
提交成功后保存私有 `.workspace-published/` 回执（不进入用户文件列表或 Sandbox 输入），覆盖提交成功但响应丢失的窗口。
重启后再次批准同一制品时只核对既有提交与最终文件，返回已有事务，不重复写入；未提交事务仍按原 journal 回滚。

## 固定发布顺序

一次 `publish()` 在项目级跨进程锁内执行以下步骤：

1. 启动恢复：扫描同一项目的未完成 journal。无 durable commit 的事务先回滚；已有 commit 的事务只重试清理，绝不回滚已提交内容。
2. 重新计算 snapshot archive 的 size/SHA，并完整校验 archive manifest；随后重新验证 artifact archive、validation evidence 和服务端签名。
3. 从可信 snapshot 与 artifact 生成排序后的完整 intent。新增、替换和删除都必须与 baseline 的 before size/SHA 一致。
4. 使用 snapshot 原始 `SnapshotPolicy` 重新扫描整个本机项目。任何未变路径、目标路径、文件模式或内容变化都会在写入前返回 `publish_conflict`。
5. 在项目外的 state root 写入 canonical、SHA-256 hash-chained JSONL `begin` 记录。每条记录 append、flush、`fsync` 后才允许执行对应文件系统效果。
6. 替换和删除项先复制完整备份并复核源文件身份、size/SHA；新增和替换 payload 从已验证 tar 重新读取，在目标父目录生成 transaction-specific 临时文件并复核 SHA。
7. 逐项应用：新增使用同文件系统 hard-link 的 no-clobber 创建，替换使用 `os.replace`，删除只在目标仍匹配 baseline SHA 时执行。每项都有 durable `apply_started` / `applied` 记录。
8. 重新扫描整个项目并再次验证 artifact。完全匹配预期最终 manifest 后写入 durable `commit`，随后清理 backup 和临时文件。

同一 baseline 与 artifact 的重试会返回 `already_published`，但只有当前工作区仍精确匹配已提交结果时才成立；提交后的用户修改会返回冲突，不会被重放覆盖。

## Journal 与恢复

状态目录布局为 `state/projects/<project-key>/transactions/publish-*/journal.jsonl`。完整 project digest 保存在不可变 intent 内，目录只使用 128-bit key 以控制 Windows 路径长度。Journal 行必须是 canonical JSON，sequence 连续、transaction ID 一致，且 `previous_sha256` 形成完整 hash chain。

进程在最后一行写到一半时，恢复会先按 SHA 保存 `journal.torn-*.bin` 证据，再将主 journal 截断到最后一个完整换行并继续。完整行、hash chain 或 intent 被篡改时固定返回 `journal_invalid`，不会继续修改工作区。

回滚按计划逆序执行：

- 新增目标只有仍匹配 artifact after SHA 时才删除。
- 替换或删除目标只有处于可信 before 状态或本事务的 after 状态时才恢复备份。
- 任意 post-crash 用户修改、非普通文件、symlink、junction 或 reparse point 都返回 `recovery_conflict`，不会猜测或覆盖。
- directory intent 在 `mkdir` 前落盘，directory completion 在创建并同步后落盘。缺少 completion 时恢复不删除同名目录，因为它可能是崩溃后的用户修改。

`publish()` 与 `recover_pending()` 被调用方取消时，后台线程仍完成提交或回滚后再传播取消，避免请求取消把本机项目留在未知状态。

## 部署约束

- `state_root` 必须是绝对路径且位于项目根目录之外，防止 journal、备份和锁进入项目快照、制品或 Sandbox。
- Sandbox 只能看到上传的 snapshot archive，永远不得挂载或获得真实 `project_root`、Publisher state root 或本机路径。
- Publisher 不执行制品中的命令、不应用模型生成 patch，也不信任模型提供的验证结果或 SHA。
- 服务启动时必须先对将要发布的每个项目调用 `recover_pending()`，再开放新的发布请求。
- `PublisherError` 只公开固定错误码和已规范化相对路径，不包含文件内容、签名 secret 或原始 OS 异常文本。

当前 v1 journal 和 intent schema 分别为 `pi-agent-publisher-journal/v1` 与 `pi-agent-publisher-intent/v1`。Schema 升级必须提供显式恢复兼容策略，不能静默忽略旧事务。
