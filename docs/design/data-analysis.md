# Web 可选数据分析工具

日期：2026-09-05。定位为本机 Web Agent 的固定表格运算，不是任意代码解释器。

后续新增独立可选 `run_python_analysis`：专用本地 Python 环境、每次执行前完整代码确认。
不改变本页固定 `analyze_data` 的无代码契约，具体见 [Python 分析设计](python-data-analysis.md)。

## 使用闭环

1. 拖拽上传文件，原件位于当前 Workspace 的只读 `upload/`。
2. 在 Workspace → extensions → Tools 勾选 Data Analysis；默认关闭，选择按 Workspace 持久化。
3. 在聊天中要求分析文件，Agent 使用 `list_files` 获取 file ID，再调用 `analyze_data`；也可直接在右栏 analysis 页运行，不经过模型。
4. 查看真实计算的表格、统计和 PNG 图表。界面明确区分输入行数、过滤后行数、结果行数、导出行数和预览行数。
5. 点击 Save results to Workspace，单事务追加 `artifacts/analysis/<run-id>/report.md`、`result.csv`、`manifest.json` 及图表任务的 `chart.png`。

计算不会改原件，不会自动写 Workspace；计算任务只产生私有临时结果。删除分析历史会清除私有
预览，不会删除已保存的 Workspace 文件。重启后可恢复已完成任务；未完成任务变为 interrupted，
由用户显式 Retry，绝不自动重放。

## 实现边界

| 层 | 责任 |
| --- | --- |
| `agent_workspace/analysis.py` | 严格参数模型、动作和资源边界，不导入计算库 |
| `pi_agent_core_py/tools/data_analysis.py` | 原生工具 schema、请求 Session 绑定、进度与模型可读结果；通过接口注入服务 |
| `coding_agent_app/data_analysis/engine.py` | 固定读取/过滤/计算/绘图程序；重依赖仅在计算时导入 |
| `coding_agent_app/data_analysis/service.py` | 独立工作进程、Session 归属、队列、取消、SQLite 历史、来源 hash 校验、显式保存 |
| `agent_workspace/store.py` | 有界分析产物追加事务；不放宽 Sandbox 路径/UTF-8 策略；禁止覆盖原件及现有产物 |
| `web/data_analysis.py` 与前端 | 账号/Session 鉴权、异步 API、Tools 选择、分析表单、历史、表格和图表卡片 |

Tools 与 MCP/Skills 同属 Workspace 资源快照，但不是 MCP server。extension schema 4 增加独立
工具选择表；旧客户端不发送 `tool_names` 时保持已有选择。服务层在开始任务和实际执行时再次
检查启用状态；关闭后不可新算或保存，历史仍可查看、取消和删除。

## 运算和数据规则

- 输入：CSV、TSV、XLSX、Parquet。第一版不支持 XLS、JSON、数据库、PDF、图片、视频链接或远程数据 URL。
- `inspect`：schema、工作表、有限行预览；CSV/TSV 按字符串读取，保留 ID 前导零。
- `profile`：缺失/非空/去重计数；严格推断为数值的列提供 min/max/mean/四分位数。
- `aggregate`：最多四列分组、十个指标，支持 sum/count/mean/min/max/median；count 为非空计数。
- `timeseries`：显式日期列、日期格式，UTC 的 day/week/month/year；默认 ISO8601，含空/错误日期则失败，不静默丢弃。
- `chart`：bar、line、histogram、scatter；bar/line 按 X 分组求 Y 和，histogram 十个等宽桶；限制点数会显示警告。
- 白名单过滤 eq/ne/gt/gte/lt/lte/is_null/not_null，以及结果列排序和行数上限；不接受表达式、Python、SQL、eval 或依赖安装。
- 所有统计先处理边界内的完整输入，再限制结果；不是拿头几十行估算全量统计。
- 空单元格为 null，数值计算拒绝混杂文本/无穷；采用 Pandas 数值类型，不承诺 Decimal 财务精度。
- Excel 只读缓存公式，不执行宏、不重算公式。涉及缺失缓存的计算拒绝执行；inspect 提示警告。
- CSV 输出对以 `= + - @` 开头的字符串转义，避免表格公式注入；原始数值预览不作为 HTML 执行。
- 来源 SHA-256、请求参数、引擎/依赖版本、产物 hash 进入 manifest。保存前再次核对来源和产物，重复保存幂等，不覆盖用户改动。

界面表单提供常用动作、单指标、单分组和按月汇总；多指标、多分组、过滤、排序和其他周期可由
聊天工具或结构化 API 使用。图表字体取决于本机可用字体；原表格不受字体影响。

## 限制与隔离

- 输入最大 25 MiB、20 万行、256 列、100 万单元格；XLSX/Parquet 解压数据预检最大 256 MiB。
- 工作进程最大内存 1 GiB、CPU 60 秒；排队加执行总超时 90 秒，取消会终止工作进程。
- 每 Workspace 最多一个活跃任务、20 条历史；达到历史上限需删除旧预览。同一应用事件循环最多两个计算并发，不是跨多个服务器进程的全局调度器。
- 最多导出 5,000 结果行；Web 预览最多 50 行、单元格显示最多 300 字符，图表最多 50 行。限制不隐瞒；模型上下文过大时另降为有标记的紧凑预览。
- 工作进程 JSON 最大 2 MiB，单个保存文件最大 5 MiB；导出另受 Workspace 原有配额约束。
- 每次复制并核验当前 Session 的一个文件。工作进程不继承凭证环境，仅接收运行所需固定变量；临时输入任务结束即清理。
- Windows 使用 Job Object；POSIX 使用资源限制。限制不可用即失败，不自动退化为无界运行。
- 这是可信固定程序的进程隔离与资源限制，**不是任意恶意 Python 的安全沙箱**；不声称 OS 层全文件系统/网络隔离。功能本身不提供网络请求或任意文件路径参数。
- 私有历史位于账号 uploads 目录旁的 `uploads-analysis/analysis.sqlite` 和 run 目录；不再建立另一套 Workspace 文件事实源。

聊天工具结果（含有限表格预览）会发送给当前 Provider；“本机计算”不等于“聊天数据永不发送模型”。
Telemetry 只记录 Session、工具、动作、状态、处理行数与耗时，不记录文件名、列名、过滤值或单元格。

## 安装与验证

可选 extra 为 `.[data-analysis]`；未安装时目录标记不可用，不在请求中自动下载。主 CI 显式安装
该 extra，以运行真实 Pandas、Parquet、Excel、图表和工作进程回归；无依赖环境仍运行工具契约测试。
本轮证据见[验证记录](../validation/data-analysis-2026-09-05.md)。
