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

本次修复提交推送后重新运行完整 CI；在实际完成前，不将此前跳过的阶段标记为通过。
后续远端结果在这里补充，当前状态以根目录 `STATUS.md` / `TODO.md` 为准。
