# Project Status

> 当前事实快照：2026-09-10。未完成事项只维护于 [TODO](TODO.md)；
> 历史数字见 [验证报告](docs/validation/) 与 [CHANGELOG](CHANGELOG.md)，不将不同批次累加为全量结果。

## 基线与范围

- Python、Web API、前端、Worker 与最新 release tag 均为 `0.0.29`；当前工作分支 `master`。
- localhost-only 本机 Web Agent，不提供公网 SaaS、独立 client/protocol/server/tui。
- 声明 Python >=3.11，本机用 conda `pipy` Python 3.12.13。
- Git origin 为 `https://github.com/TerrenceHsu/pi-web.git`，已按用户要求公开；公开源码不等于部署 Web 服务，不新增 release tag。
- 主工程 MIT；Worker 另附 MinerU 第三方许可、Notice、SBOM 与对应源码入口。

## 本轮工程化修复

2026-09-10 浏览器停帧恢复：连续错误时有界重建帧源，耗尽预算后明确报错；只有真正解码了
当前视口的画面才显示 Live，显式 Reconnect 可重试。截图/配置失败、旧尺寸帧与静默帧源均有回归。
退出登录/登录过期会终止已有 WebSocket；代理改为 180 秒共享空闲期限与独立 6 小时硬上限。
前端 312 项、相关后端 84 项、真实 Chromium 4 项及 Web E2E 11 项通过（不同层级，不算全仓门禁）。
用户确认“下一步”后，单标签媒体已接通：项目专用扩展精确绑定 Session/page/tab，
VP8/Opus 通过有界认证 WebSocket/MSE 播放；固定 1080p、目标 30 fps，支持用户播放解锁、
静音/音量、停止恢复图片，隐藏/切换/关闭/删除/登录撤销回收，单账号一路，失败不提前释放配额。
修复媒体头分片、启动时间线间隙与跨 Session 旧导航竞态；无日常浏览器扩展安装或远程 CDP 端口。
完整 Chromium 149、151 的页面音视频验收均通过：连续 30 秒约 29.49 fps、音频非零、
最大缓冲约 4.6 秒；headless shell 路径未通过，不以该测试壳作为普通浏览器媒体验收环境。
当前相关后端 191 项、真实 Chromium 5 项、前端 336 项和最终 Web E2E 13 项通过，类型/静态检查与正式构建通过，
仍非全仓门禁；媒体代码需随生产后端重启加载，前端刷新生效。60 fps、多路媒体与后台音频留待阶段 3。见
[设计](docs/design/workspace-browser-media.md) 与 [验证记录](docs/validation/workspace-browser-media-2026-09-10.md)。

2026-09-10 栏宽调节：加强 Workspace 与聊天之间的可拖动分隔线，移除中栏 760px 固定上限，
按窗口空间联动调宽并保护中栏 280px / 聊天 320px 最小宽度；支持双击复位、键盘操作和本地记忆。
临时收窄窗口不覆盖桌面偏好；窄聊天栏的输入框与顶部状态按栏宽换行。
验证与操作说明见 [栏宽调节记录](docs/validation/panel-resize-2026-09-10.md)。

2026-09-10 公式显示：公共 Markdown 渲染器接入本地 KaTeX，支持行内/块级公式及 LaTeX 括号分隔符，
助手回复、Context Summary、Workspace Markdown 和 Wiki 共用；代码与用户原文保持字面显示。
修复旧 `.row > *` 样式阻止消息收缩导致的长公式裁切，公式内部可横向滚动；
无效/未完成公式安全回退为原文，禁止可信 HTML/URL 命令，不改写历史公式的转义字符。
本轮验证与输入边界见 [公式显示修复记录](docs/validation/markdown-math-2026-09-10.md)。

2026-09-10 浏览器比例跳变修复：视口重绘完成后才启动动态帧；前后端拒绝尺寸不匹配的帧，
帧携带采集时坐标，前端以固定 CSS 比例显示。新增合成色块的像素位置回归，覆盖动态/静态切换、
HD、滚动、分屏尺寸和跨页面导航；不操作 Google 验证码。见 [补充验证](docs/validation/workspace-browser-performance-2026-09-09.md#画面比例跳变修复2026-09-10)。

2026-09-09 浏览器优化：移除前端 450ms 截图轮询，改为有背压的二进制 WebSocket 推送，
动态 JPEG 88 + 静态无损 PNG、最高 2× HD 与 CSS 坐标/视口版本校验；拆分页面导航/输入锁，
合并连续输入/滚轮，补已校验公网 IP 连接回退。Google 全新未登录上下文与高清像素已验证；
这是传输和渲染优化，不宣称 Google 外网速度固定或已恢复 HTTP 缓存。
详见 [性能验证](docs/validation/workspace-browser-performance-2026-09-09.md)。

2026-09-09 补充：Workspace 分隔线下已实现浏览器式 Markdown/网页多标签、左右/上下分屏、
独立关闭与草稿保留；New Markdown 去重，末尾“＋”打开真实本机 Chromium。
按账号/Session 隔离临时网页登录状态，公网校验代理、配额、15 分钟空闲回收与 Session 删除联动。
不提供 Agent 浏览器工具，不复用日常浏览器；普通网页与合成登录/弹出页已实测，
完整桌面 Chrome 的扩展/文件选择器/音视频能力不属于已验收范围。
操作见 [Workspace 浏览器说明](docs/design/workspace-browser.md)，本轮分批结果见
[多标签与浏览器验证](docs/validation/workspace-browser-2026-09-09.md)。

2026-09-09 补充：侧栏 Session 删除已接通绑定同事务清理、完整 Workspace/分析结果删除、
执行资源回收确认、请求/事件缓存清理与前端迟到响应隔离；失败保留会话供重试。
本机 25 条历史孤儿 Provider 绑定已清为 0，唯一会话及 4 条消息、共享 Profile/凭证均保留。
删除专项 50 passed / 3 skipped，前端 277 passed，新增 Chromium 删除链路通过；
并修复 Provider DELETE 的 204 非空正文错误。前后端已加载，详见
[Session 删除闭环记录](docs/validation/session-deletion-closure-2026-09-09.md)。
共享知识与独立执行审计不随聊天误删；最后会话删除后仍按原策略新建空白 default（新 ID）。

2026-09-09 补充：Provider 设置统一为名称、API 协议（OpenAI / Anthropic 兼容）、
Base URL、Model ID 与凭证；不再按厂商分区，不再显示两项 Token 限额输入。
旧 Profile / Keyring / Session binding 保留，协议与地址在每次请求开始时冻结；
后端模型预算仍保留，自定义端点按独立作用域解析，未知窗口不推测百分比。
本轮独立验证见 [Provider 统一配置记录](docs/validation/provider-neutral-settings-2026-09-09.md)，
下方旧 CI/工程化批次不代表本轮全量验收。

同日修复 OpenAI-compatible 带工具请求在 SDK strict 自动解析校验处失败的问题：
改用原始流，保留工具 schema/取消释放；本地协议错误不再误报连接失败或自动重试。
相关后端 505 passed / 1 skipped，最终适配器专项 95 passed（重叠批次不累加），
双平台 Mypy/Ruff 通过；已加载本机后端，真实模型响应待重新登录后重试。
见 [流式工具修复记录](docs/validation/openai-compatible-stream-fix-2026-09-09.md)。

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
上述 2026-09-07 维护演练和密码测试均使用临时数据；该批次没有改业务密码、迁业务库、替换业务解析容器或自动开启 Bash。

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
| Provider | 厂商无关的协议/Base URL/Profile/Model/Session binding；OS Keyring、session-only 或显式环境变量，不在 SQLite 存明文 Key |
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
