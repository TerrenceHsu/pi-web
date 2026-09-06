# Docker 启动恢复与数据迁移记录

日期：2026-09-06。结果：已通过 Docker Desktop 官方设置迁移，Linux 引擎正常，原容器与镜像标识保留。

## 用户授权与范围

用户指定将 Docker 数据迁到 `D:\DockerData`，并批准先修复启动故障。不恢复出厂、不删除镜像或容器，
不安装/更新 Docker，不拉取或构建镜像。本轮不接入或启用 Agent Bash。

## 启动恢复

错误涉及旧 `sailor-ingest.sock` 与 `docker-secrets-engine/engine.sock`。Windows 对它们返回
`The file cannot be accessed by the system`。Docker 的 WSL 实例停止时，将仅含 0 字节运行 socket 的
普通父目录改名保留，并创建空运行目录；未读取凭据内容，未修改磁盘数据。保留位置：

- `C:\Users\Administrator\AppData\Local\Docker\run.pre-repair-20260906-01`
- `C:\Users\Administrator\AppData\Local\Docker\run.pre-repair-20260906-02`
- `C:\Users\Administrator\AppData\Local\docker-secrets-engine.pre-repair-20260906-01`

首次仅处理一个目录后，启动在第二个旧 socket 处失败；该次启动生成的运行文件也保留后，
两个运行目录均干净时成功启动。正常停止命令曾因错误窗口卡住，确认 WSL 已停止后使用官方
`docker desktop stop --force --timeout 20` 退出失败的 Desktop 进程。未执行 factory reset。
底层旧 socket 不可访问的长期成因尚未完全确定；本记录证明此次恢复和迁移重启成功，不承诺永不复发。

## 迁移前备份

Docker 完全停止时，将两份磁盘与旧 `settings-store.json` 复制到
`D:\DockerData\.migration-backup-20260906`。两份磁盘均核对源/副本 SHA256 一致：

| 文件 | 字节数 | SHA256 |
|---|---:|---|
| `docker_data.vhdx` | 32,836,157,440 | `2A28FCB1B1C538E5B12AEFBBE4589150D5431583A808311174F290E3D3634777` |
| `ext4.vhdx` | 100,663,296 | `CD8E0ED567244AD5414EDAD2C8D9CBE186FDAB5D1B956520B4EC7FD647982CD0` |

该备份约占 30.7 GiB，迁移验证完成后曾保留；用户随后明确要求删除多余备份，现已清理（见下节）。
它曾是启动恢复后、正式迁移前的离线磁盘恢复点，不代表所有用户配置/宿主 bind mount 文件的完整备份。
表中哈希仅为历史验证证据，不能再用于从这份已删除的备份恢复。

### 用户要求的备份清理

2026-09-06 核对活动磁盘路径、原镜像与容器状态后，使用明确的文件路径永久删除
`D:\DockerData\.migration-backup-20260906` 中的两份 VHDX、旧设置副本及空目录。
删除文件合计 32,936,821,026 字节（约 30.7 GiB），不经回收站，已无该迁移前恢复副本。
`D:\DockerData` 现仅保留 `DockerDesktopWSL`；再次确认解析容器 healthy，未停止引擎或修改活动数据。

另尝试清理上列三个旧 socket 备份目录，但 Windows 对其中 7 个 0 字节特殊文件返回
“系统无法访问此文件”。这些残留仍保留；未修改权限、强行移除 reparse 元数据或中断运行中的 Docker。
不能把此次清理描述为所有旧 socket 目录都已删除。

## 官方迁移及核验

使用 Desktop 的 Settings → Resources → Advanced → Disk image location，选择 `D:\DockerData`。
Docker 自动添加 `DockerDesktopWSL` 子目录。确认目标不存在旧磁盘后，Apply & restart → Yes, move it。
未手工更改注册表、创建 junction、修改 daemon data-root 或复制覆盖 Workspace。

- 实际数据磁盘：`D:\DockerData\DockerDesktopWSL\disk\docker_data.vhdx`。
- WSL 系统磁盘：`D:\DockerData\DockerDesktopWSL\main\ext4.vhdx`。
- UI 保存后显示 `D:\DockerData\DockerDesktopWSL`、Apply 禁用、Engine running。
- WSL `docker-desktop` 注册 BasePath 为 `\\?\D:\DockerData\DockerDesktopWSL\main`。
- 后端迁移日志显示移动 WSL 数据，并返回 `vm.resources.wslDataFolder=D:\DockerData\DockerDesktopWSL`。
- 原 C 盘 `wsl/disk/docker_data.vhdx` 已由官方迁移流程移走，不再存在。
- C 盘可用空间由 21,916,659,712 增至 55,660,548,096 字节，约 20.4 → 51.8 GiB；
  系统后台活动会影响实时余量，不将该差值当作精确磁盘文件大小。
- Docker Desktop 4.87.0 (236836)，Server Engine 29.7.2，linux/amd64 可正常连接。

迁移前后标识一致：

| 对象 | 标识 | 迁移后状态 |
|---|---|---|
| `pi-wiki-parser-worker-1` | `0eb6a9da36c3a67f98befd5d763844b8ffa610eb46040f0dc6dbe3cfb954b6bb` | running / healthy |
| `pi-wiki-parser-worker:0.0.28` | `sha256:d1e517ba7a1381255da24f61b97a374a62e74c9916c3e093f4a6e1bf2bf0368b` | 存在 |
| `python:3.12-slim-bookworm` | `sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134` | 存在 |
| `hello-world:latest` | `sha256:5dd0d3e6e255913fc30f90b9f2b1d359cc2cbdb48090cc4b65f1676e203243cc` | 存在 |

`docker volume ls` 迁移前后均为空。未检查容器内部每个业务文件，也未重新运行解析业务任务。
迁移后的活动磁盘会被运行中的引擎写入，因此不将其与迁移前备份做静态哈希相等断言。

## 项目后续

环境阻塞已解除，但固定 Bash 运行镜像和真实沙箱隔离/复用 smoke 仍待执行。
Docker 数据迁移成功不等于 `run_bash`、ExecutionGrant、Web 发布或安全门禁已完成。

参考：[Docker WSL 数据位置设置](https://docs.docker.com/desktop/features/wsl/)、
[停止 Desktop 后备份磁盘](https://docs.docker.com/desktop/settings-and-maintenance/backup-and-restore/)。
