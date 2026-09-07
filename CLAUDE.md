# 项目协作与运行约定

本目录 `D:\LLMTutorial\test` 是本机 Web Agent 项目，不再按早期 Step 1–21 精简副本的进度维护。
当前功能、验收和剩余边界以 [STATUS.md](STATUS.md)、[TODO.md](TODO.md) 为准；旧计划保留于 `docs/archive/`。
上游 `D:\LLMTutorial\pi\pi-main` 仅供参考，不修改其源码。

## 运行与验证

- 本机 Python 使用 `D:\miniconda\envs\pipy\python.exe`；运行项目前设置 `PYTHONPATH=src`，确保导入本目录源码。
- `.venv-analysis` 是用户批准代码分析的独立运行环境，不是冗余缓存，也不等于安全沙箱。
- 常规门禁：`python scripts/run_local_gates.py`；具体环境准备、Docker 显式验收见 [本地门禁指南](docs/guides/local-gates.md)。
- 不在测试收集阶段自动加载根 `.env`；真实 Provider 测试由 `scripts/run_live_integration_tests.py` 显式开启。
- Web 启动使用当前 Windows 交互用户，让 Credential Manager 可写。`scripts/dev_web_app.py` 的
  Keyring 探针失败时应修正启动上下文，不绕过探针；只有明确接受不持久化密钥时才使用 memory backend。

## 数据与权限

- 不提交 `.env`、登录 Cookie、业务 SQLite、上传文件或运行时凭证。
- `.pi-agent-data`、`dev_data`、根 `dev_sessions.db*` 和 `uploads` 可能包含用户数据，不能仅因日期旧而删除。
- 清理临时目录前核对进程和容器挂载；当前业务 Worker 使用 `.test-tmp/wiki-parser-exchange-final`，
  禁止整批清空 `.test-tmp`。保留最新门禁和未完成质量验收所需的证据。
- 备份恢复/Wiki 升级须离线、使用全新目标；见 [数据维护指南](docs/guides/data-maintenance.md)。
- 不修改上游参考仓库；不擅自启用 Bash、放宽执行/发布审批，或替换业务解析容器。
- 保持不可变消息/模型契约；Provider 异常按既有流式错误协议传递，不向前端暴露密钥或内部异常正文。
