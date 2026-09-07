# Project Status

> 当前事实快照：2026-09-07。未完成事项只维护于 [TODO](TODO.md)；
> 历史数字见 [验证报告](docs/validation/) 与 [CHANGELOG](CHANGELOG.md)，不将不同批次累加为全量结果。

## 基线与范围

- Python、Web API、前端、Worker 与最新 release tag 均为 `0.0.29`；当前工作分支 `master`。
- localhost-only 本机 Web Agent，不提供公网 SaaS、独立 client/protocol/server/tui。
- 声明 Python >=3.11，本机用 conda `pipy` Python 3.12.13。
- Git origin 为 `https://github.com/TerrenceHsu/pi-web.git`，已按用户要求公开；公开源码不等于部署 Web 服务，不新增 release tag。
- 主工程 MIT；Worker 另附 MinerU 第三方许可、Notice、SBOM 与对应源码入口。

## 本轮工程化修复

本次 GitHub 发布补充中英文 README、双语演示与合成输入；具体新增验证和未演示边界见
[发布准备记录](docs/demo/VALIDATION.md)。下方工程化测试数字保留原批次，不将演示测试累加进去。

实施顺序见 [工程化收敛计划](docs/design/engineering-closure.md)。

| 项目 | 当前状态与边界 |
|---|---|
| Telemetry | 周期裁剪终态 span，保护 running；安装锁内启动对账崩溃遗留记录；失败固定错误码/重试，Admin UI 显示策略/故障；关闭回收维护任务 |
| MinerU 探针 | Worker 每 5 秒更新心跳；客户端与容器探针拒绝超过 30 秒或来自未来的状态，Worker 关闭后不可用 |
| 执行对象释放 | 已确认清理的终态任务释放内存对象和锁；维护查询只取活跃许可/清理债务，授权记录与冻结制品仍保留 |
| 备份恢复 | 离线 SQLite 快照 + 文件 SHA 清单 + integrity/FK 检查；目标必须全新，不自动切换业务配置；网关与维护共用 OS 锁 |
| Wiki 升级 | v7 严格兼容迁移，或显式 v1–v7 原件重建；保留完整 legacy 归档，新 ID/旧历史不迁入的边界写入报告 |
| 本地门禁 | `scripts/run_local_gates.py` 覆盖锁文件、主工程/Worker、独立 Python 环境探针、Evals、前端和 Chromium；Docker 显式固定镜像 |
| CI | 修复提交 `32b3d53` 的第三轮远端完整通过：后端 2684 passed / 78.50%、Worker 11、Evals 6 suites / 10 pairs、前端 241、Linux/Windows Chromium 各 27（零重试）；两平台 strict Mypy 与生产构建恢复通过 |
| 改密 | 侧栏 Password 入口；当前密码验证，新密码 12–128 字符；原子撤销该账号旧登录，阻断并发旧密码登录并断开旧 WebSocket |
| 新 MinerU 实机 | 三档队列解析/制品 SHA/销毁、GPU 运行中取消与重启恢复通过；GPU 合成表格/OCR 正确，CPU 表格单元格错误，`runtime_ready=false` |

操作见 [数据维护指南](docs/guides/data-maintenance.md) 与 [本地门禁指南](docs/guides/local-gates.md)。
最终修复验收见 [GitHub Actions 34108827664](https://github.com/TerrenceHsu/pi-web/actions/runs/34108827664)；
首次失败、三轮修复及本机/远端分批结果见 [CI 修复记录](docs/validation/github-ci-repair-2026-09-07.md)。
下方本地测试结果仍为原批次，不与远端结果累加。公开仓库及离线 CI 通过不改变 localhost-only 和 MinerU 未就绪边界。
所有维护演练和密码测试均使用临时数据；没有改业务密码、迁业务库、替换业务解析容器或自动开启 Bash。

## 工程化本地验证（CI 修复前批次）

- 本地一键 `all`：15 个检查/恢复步骤全部通过，日志 `.test-tmp/gate-all-20260907T034352Z/`。
- 后端：2674 passed / 1 skipped / 21 deselected，coverage **78.36%**（含分支统计，门槛 75%）。
- Worker：11 passed；Evals：6 suites / 10 pairs，PASS。
- 前端：236 passed；Chromium：27 passed（零重试），production bundle 已恢复。
- 交付补充回归：维护/Auth 25 passed；协议/探针/Worker/门禁 23 passed。不同批次不与全量相加。
- Docker：25 项底层检查通过；12 passed / 62 deselected，434.48 秒。
- 主工程 Ruff、strict Mypy 308 files、Worker 静态检查通过。
- MinerU：CPU / GPU medium / GPU high 均完成单次真实解析与清理；取消/强制重启安全终态化及清理通过。
  详细失败迭代与证据见 [工程化验收报告](docs/validation/engineering-closure-2026-09-07.md)。

上轮 Bash 阶段 5 的 2650 passed / 78.41%、前端 232、Chromium 26 属于
[2026-09-06 基线](docs/validation/workspace-bash-stage5-2026-09-06.md)，不是本轮结果。

## 已实现的产品链路

| 模块 | 当前能力 |
|---|---|
| ai / agent | Provider-aware 消息/图片/Thinking/usage、首事件前重试、流生命周期、工具批次、协作取消、steering/follow-up、hook 与状态同步 |
| coding-agent | 每个 Web Session 独立 Runtime/Agent/Harness；同 Session 串行，不同 Workspace 可并行；统一请求级 Provider/Skill/MCP/Workspace/Prompt 快照 |
| Session / Memory | append-only 消息树、lane/branch、writer fence、SQLite FTS；只读历史工具与结构化 Memory 增量，来源/纠正/固定/分支失效和失败恢复 |
| 压缩 | 70% 预警、80% 自动 LLM 摘要、60% 目标、逐调用 hard stop；持久工作视图和大工具结果外置，失败不改原始消息或 Memory |
| Workspace | 拖拽原件统一 `upload/**`，工作代码 `scripts/**`，Agent 成果 `artifacts/**`；revision/SHA 冲突检查、预览、下载和固定 PDF/DOCX/XLSX 转换 |
| 连续性 | `AGENT.md`、`Memory.md`、revision-bound 代码摘要与统一上下文装配；stale 摘要、待审批制品不会冒充当前代码 |
| MCP / Skills | 账号级全局目录 + Workspace 选择；stdio、Streamable HTTP；DDGS 为不可删除内置选项；HTTP 认证只持久化环境变量引用 |
| Provider | Web Profile/Model/Session binding；OS Keyring、session-only 或显式环境变量，不在 SQLite 存明文 Key |
| Coding / Plan | Planner–Executor–Verifier；任务范围许可、固定验证、签名冻结、独立审阅批准和事务发布；Local Docker 与 Managed E2B 支持 |
| Bash | 五阶段代码与验收链路已实现；独立脚本逐次确认，Coding/Plan Executor 任务内复用；Planner/Verifier/read-only/Knowledge 不获得 Bash |
| 执行维护 | 账号/全局共享配额、Workspace 互斥、后台心跳/撤销/孤儿清理/缓存 TTL；未知清理保留债务，管理员可观测并重试 |
| Data Analysis | 可选固定统计工具及逐次确认的 Agent Python；独立本机 Python 环境，预览、历史和显式保存；不是 Docker 安全沙箱 |
| Wiki | MinerU Contract v2 三档与 HTML 零网络；Source→Raw→Proposal→Change Set 审批→Page/FTS5/Graph；每 Space 多 Knowledge 对话、来源和归档生命周期 |
| Telemetry / Evals | Admin-only 无正文观测；本机 Fake Provider 配对 Evals 验证编排/隔离/持久化，不代表真实模型智能评测 |

`/checkpointer` 仍是显式“摘要写 Memory 后清空当前 lane”的独立操作，不等于保留原消息的工作视图压缩。
旧 Chunk-RAG/Docling 运行时已经移除，不作为当前 Knowledge 主链路。

## 安全与恢复边界

- Bash 默认关闭；需管理员配置固定 Docker CLI/镜像并由 Workspace 选择，不因 Python 分析自动获 Bash 权限。
- Docker 使用离线受限副本，不挂载业务 Workspace；任务结束回收，批准签名制品后才事务写回。
- 独立 Bash 的 `bash-output-integrity/v1` 只证明输出完整性，不冒充 Coding/Plan 固定功能验证。
- 默认执行配额：每账号 2 / 全局 4；CPU 总额 8、内存 8192 MiB、每账号缓存 2 GiB。
- 运行许可不跨重启恢复；只对账清理/恢复可审阅制品，不重放模型、命令、批准或发布。
- 普通 active request/pending approval 不跨后端重启恢复；刷新只能恢复仍在当前进程中的请求。
- 初始 `admin / 123456` 仍仅用于首次本机登录，请立即通过 Password 改密；没有强制改密、Users 管理、OAuth 或公网加固。
- 改密不删除聊天或配置，也不回滚已接受的在途工作；后续请求与旧 WebSocket 失去登录权限。
- 备份不含 OS Keyring、Docker 镜像、外置数据目录和 Python 环境；恢复旧签名制品依赖原 Keyring，不恢复旧执行许可。
- Wiki 原件重建不是原 ID/页面/审批/对话的无损迁移；必须重新解析、选择新 Space 并审批。
- Context Budget 仍是有余量的估算器；没有官方 tokenizer、原生 deferred tool loading、OTLP 或外部告警。
- Telemetry 裁剪保护 running；产品网关启动前在安装锁内对账崩溃遗留 span，通用 SDK 不擅自对账其它写入者；终态数量上限不是数据库硬磁盘配额。
- 图片可上传/预览；Workspace OCR/视觉理解和视频链接解析尚未实现。Wiki PDF OCR 不等于 Workspace 全媒体解析。

## 实机环境与剩余事项

Docker CLI 已可用，数据位于 `D:\DockerData\DockerDesktopWSL`。
本机 GPU 为 RTX 4060 Laptop，8 GiB 显存。最终新版验收镜像 ID 为
`sha256:11f1ed9a4fd97423962820889aba0f5827dc025a4474acaa921febd1ff134822`。
新版镜像与合成 PDF 仅用于验收；测试容器已回收，原 `0.0.28` 业务 Worker 保持健康，未被替换。
源码/Fake 测试、镜像构建、真实分档解析和代表性质量验收是不同证据层次。
CPU 合成表格仍丢失/错配数据，不能用结构质量分数 1.0 掩盖；代表性中文/复杂文档质量仍待验收。

本轮代码、门禁和验证报告整理为本地交付，不自动部署；未完成清单以 [TODO](TODO.md) 为准。
