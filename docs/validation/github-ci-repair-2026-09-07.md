# GitHub CI 失败修复

日期：2026-09-07。范围：公开仓库 `TerrenceHsu/pi-web` 的远端门禁失败，
不变更业务配置、部署服务、放宽执行权限或创建 release tag。

## 原因与修复

首次运行 [34095561462](https://github.com/TerrenceHsu/pi-web/actions/runs/34095561462)
及文档提交后的 [34098378014](https://github.com/TerrenceHsu/pi-web/actions/runs/34098378014)
均失败：前端通过，Mypy 和 Linux/Windows 平台前置检查失败，后续后端/浏览器阶段被跳过。

1. **平台类型分支**：`os.name` 分支未让 Linux Mypy 排除 Windows 专属的
   `ctypes.WinDLL` 和 `msvcrt.locking`。改用类型检查器能识别的 `sys.platform == "win32"`；
   原 Windows Job Object 限额及 Windows/POSIX 文件锁实现不变。
2. **PyArrow 类型环境漂移**：开发机 PyArrow 24 与 frozen 环境的 25 不同，CI wheel
   缺少可用的类型标记，同时旧的 `no-untyped-call` 忽略变为冗余。将 PyArrow 纳入已有的
   计算库类型边界，并去掉四处版本敏感的行级忽略；产品代码仍启用 strict Mypy。
3. **测试导入路径**：CI 裸 `pytest` 入口没有仓库根目录，导致跨测试辅助模块及 `scripts`
   无法导入。所有 CI pytest 命令统一为本机已使用的 `python -m pytest`。
4. **遗漏的测试依赖**：干净环境全量收集暴露 `test_configure_e2b_sandbox.py` 依赖 E2B SDK，
   原 CI 未安装。门禁安装增加已锁定的 `sandbox-e2b` extra，中英文测试准备说明同步更新。
   这仅供离线 SDK 安全测试，普通本机 Web 运行仍不要求 E2B，也不会自动调用云服务。

本机和 CI 均新增显式 Linux/Windows 双平台 Mypy；两个门禁回归测试保护平台参数、
模块测试入口及 CI 测试依赖，防止重新依赖开发机偶然存在的环境。
没有关闭 strict、降低 75% 分支统计覆盖率门槛、删除测试或增加 Chromium 重试。

## 本机验证

新建 `.test-tmp/ci-repair-20260907/venv`，使用 Python 3.12.13 和 `uv sync --frozen`，
不覆盖现有 conda、`.venv`、`.venv-analysis` 或业务数据。锁文件版本未改变。

| 检查 | 结果 |
|---|---|
| 开发机两平台 Mypy | 每个平台 308 source files，PASS |
| frozen 环境两平台 Mypy | 每个平台 308 source files，PASS |
| frozen Worker Mypy / Ruff | 16 source files / 全项目检查，PASS |
| frozen 锁文件核验 | `uv lock --check --offline`，PASS |
| frozen 全量收集 | 2680 / 2701 tests collected，21 deselected，无收集错误 |
| frozen 定向回归 | 153 passed / 1 skipped；覆盖持久化、发布锁、Parquet、独立 Python 分析 |
| frozen E2B 配置安全 / 门禁回归 | 16 passed，无真实 SDK 网络调用 |
| frozen Worker 单元 | 11 passed |

定向回归中的独立 Python 分析使用既有 `.venv-analysis`，未重新安装业务分析环境。
本机一个 symlink 测试因账号缺少创建符号链接权限跳过；应以 Linux CI 的执行补充验证。
本轮定向结果不与之前 2674 项本机全量数字累加。

## 远端验收

第一轮修复提交 `e2ab6de` 的 [34100427303](https://github.com/TerrenceHsu/pi-web/actions/runs/34100427303)
已通过 strict Mypy、两平台前置检查、前端和 Windows 完整浏览器门禁，但仍失败：

- Linux 后端：2677 passed / 2 failed / 1 skipped / 21 deselected，550.82 秒。
  失败为上传文件名在 Linux 不识别 Windows 路径，以及 Wiki 并发首次打开时 WAL 模式切换报 BUSY。
- Linux 浏览器：Wiki 整行中心点击误中 Parse 按钮；新建聊天仅匹配 `/chat/:id` 读到旧会话 ID。
- `.test-tmp` 下门禁日志因上传器默认排除隐藏文件而未归档；失败上下文和 trace 可用。

第二轮修复：

1. 文件名使用平台无关的 `PureWindowsPath` 提取两种分隔符和盘符的基名；补充 UNC/混合路径回归。
2. WAL 切换仅对 SQLITE_BUSY 做有界异步重试并关闭 PRAGMA cursor；其它 I/O 错误、超时、
   静默退回 DELETE 模式均失败，不降低事务或持久化保证。并发打开测试重复三次并关闭成功的 peer。
3. Wiki E2E 精确点击文件名；聊天 E2E 等待创建响应中的 Session ID 对应的 URL 和 Store 状态。
   不补一个假的 Parse 响应、不放宽断言、不增加超时或重试次数。
4. 仅对白名单 `.coverage.xml`、`.test-tmp/gate-*/`、`tests/e2e/test-results/` 启用隐藏文件归档；
   不扩大到整个临时目录或 `.auth`。新增门禁测试保护这个范围。

本机补充：frozen Wiki/Workspace/API/门禁 **109 passed / 1 skipped**；强化后的并发/WAL 测试
**5 passed / 29 deselected**；两平台 strict Mypy 各 308 files 再次通过。
本次曾用过深的 `.test-tmp/ci-repair-20260907/frozen-full` 作为全量 basetemp，触发现有 Wiki
Windows MAX_PATH 限制（2525 passed / 1 failed / 3 skipped，提前停止）；改用预创建的短目录
`.t/ci-fix` 后相关用例通过。这不是新的本机全量通过记录，深路径限制仍保留在 TODO。
本机浏览器首次尝试被宿主权限限制拒绝启动（spawn EPERM），需正常授权的浏览器进程复验。

第二轮提交后继续远端完整复验；在实际完成前，不将此前跳过的阶段标记为通过。
