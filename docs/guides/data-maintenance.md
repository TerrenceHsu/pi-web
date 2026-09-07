# 本机数据备份、恢复与 Wiki 升级

所有操作都要求管理员明确选择路径。工具不覆盖现有目标、不切换业务配置、不启动/停止服务，
失败时可能留下未完成的新目录；保留原目录，以检查成功的 manifest/report 作为完成凭据。
容量上限为 100,000 个文件 / 20 GiB，拒绝链接、reparse point 和特殊文件。
目录枚举遇到权限错误会中止，不跳过不可读子目录生成“完整备份”。

## 备份与恢复

1. 停止 Web **以及使用同一数据目录的解析 Worker**。新网关有 OS 级 `.maintenance.lock`，
   会阻止并行维护；旧启动器、外部 Worker、手动脚本不受此锁管理，必须自行确认停止。
2. 使用运行 Web 的 Python，设置 `PYTHONPATH=src`，执行：

```powershell
python scripts/maintain_data.py backup --source D:/YourData --destination D:/Backups/run-001 --offline
python scripts/maintain_data.py verify --source D:/Backups/run-001
python scripts/maintain_data.py restore --source D:/Backups/run-001 --destination D:/Restored/run-001 --offline
```

示例路径不是当前业务路径。`--source` 必须是包含 `auth.sqlite` 的完整安装数据根。
备份读取 SQLite 已提交 WAL，生成不依赖侧文件的独立快照；普通文件按 SHA/大小复验，
源数据前后清单不一致则失败。恢复前检查全部文件、SQLite integrity/foreign-key，目标必须全新。
SHA 校验只能检测完整性，不能认证备份来源；备份含私人数据和密码哈希，应由管理员限制访问。

OS Keyring、Docker 镜像、独立 Python 环境、外置 exchange/model 目录不会自动打包。
若配置将用户数据放在安装根之外，必须另行备份；不要把本工具描述为整机备份。
恢复后只有在同一 Windows 用户仍持有原签名密钥时，待发布制品才可能继续通过验证；
缺少密钥应重新配置并重新执行，不重新签名旧制品，也不恢复运行许可。
不建议导出明文密钥放进备份。验证恢复目录、重新登录并检查 Session/文件后，再人工决定切换。

## Wiki 升级

先完成整套安装备份，再对旧 Wiki 根执行；只生成新目录，旧库不修改。

```powershell
python scripts/maintain_data.py wiki-upgrade --source D:/YourData/users/USER/knowledge --destination D:/Upgrades/wiki-001 --data-root D:/YourData --mode compatible --offline
```

`compatible` 仅支持 schema v7 中所有表/列及内容均兼容当前约束的库。
新 schema 由当前可信 DDL 创建，原 ID、行和文件保留，重建可再生成的镜像及 FTS。
未知表、不同列、旧解析模式/证据不满足新约束时拒绝；不会只修改版本号或将旧证据冒充 MinerU。

对于不兼容的 v1–v7，必须显式选择另一全新目标和 `--mode rebuild-sources`：

- 完整旧库、原文件及所有历史保留在 `legacy/`，有独立 SHA 清单。
- `wiki/` 仅导入仍存在、大小和 SHA 均正确的未删除原件，生成新的 Space/Source ID 映射。
- 页面、旧解析、审批、对话和图谱**仅在归档中保留，不迁成当前可用记录**。
- 必须选择新 Space、重新解析和审批。旧会话引用仍指向旧证据，不能假称无损原地迁移。
- 查看 `upgrade-report.json` 后人工决定激活，旧业务目录和完整备份继续保留供回退。

缺失原件、损坏、未来 schema、未知旧字段或非法路径会拒绝。任何失败都不能删除原库来绕过。
