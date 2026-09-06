# 可选 Data Analysis 验证

日期：2026-09-05；Python：conda pipy 3.12.13，`PYTHONPATH=src`。

## 范围

新增可选原生工具、Workspace 工具选择（extension schema v4）、固定本机分析进程、
SQLite 任务恢复、结果事务保存、Web API、analysis 页与聊天结果卡片。使用可选
`data-analysis` extra，已更新 uv.lock，CI 显式安装分析依赖。
未改动 MinerU 解析链；图片/视频方案仍未实施。保留原有 upload 工作区改动。

## 行为证据

- 不安装可选依赖的情况下，参数契约、工具过滤和能力探测测试仍可运行。
- 默认关闭、按 Workspace 选择；旧客户端省略 tool_names 不会清空选择。
- v3→v4 真实 SQLite 迁移保留 MCP/Skills；注入事务异常后 schema 版本和 DDL 均回滚。
- CSV/TSV 保留 ID 前导零；全输入统计、过滤、排序、Top-N 标记；Excel sheet 选择和缺失公式缓存拒绝；Parquet 与四类真实 PNG 图表。
- 两个 Workspace 的真实工作进程并发，数据不串；源/结果 hash 校验；并发重复保存幂等；清除历史不删除已保存文件。
- 真实工作进程超时、取消、重启状态恢复；Telemetry 只包含动作/状态/计数/耗时，无列名与单元格。
- 完整 Web Agent + Fake Provider 调用链实际执行 analyze_data，返回正确数值，绑定请求 Session，且不自动导出。
- 前端卡片 HTML 转义、跨 Workspace 禁止保存/加载图表、保存竞态、live/persisted ToolResult 两种形态。
- Chromium 实际启用工具→选择 CSV→生成图表→验证 PNG 加载→保存四份产物→刷新恢复→另一 Workspace 默认关闭。

## 门禁

| 检查 | 最终结果 |
| --- | --- |
| Ruff（src/tests/scripts/evals） | PASS |
| strict Mypy（src/evals） | PASS，271 files |
| 依赖锁一致性 | PASS，`uv lock --check --offline`，115 packages |
| 后端全量离线与 coverage | 2212 passed / 7 skipped / 9 deselected，445.65s；coverage 76.60%，达到 75% 门禁 |
| 最终数据分析专项 | 27/27 passed，15.76s；包含最后补充的数字排序回归 |
| Frontend Vitest | 198/198，30 files |
| Frontend typecheck / ESLint / production build | PASS |
| 完整 Chromium E2E | 22/22，无重试；posttest 已恢复生产构建 |
| 本机 Evals | 5 suites / 10 observations，candidate gate PASS |

首轮后端全量为 2207 passed / 1 failed：新增服务测试直接上传后没有先初始化 Workspace，
未把首次发布补建的 AGENT.md/Memory.md 计入基线；已将测试初始化对齐真实 Web 流程，相关
46 项通过，再执行最终全量，得到上表 2212 passed。额外 Agent 调用链专项也通过。
收尾还修正 inspect 数值列按数值排序而非字符串排序、同时保留原始单元格与前导零；最后单独
复跑数据分析 27 项全部通过。这一条新增排序用例未计入先前全量采集的 2212 项，不将两轮数字相加。
迁移故障注入测试同时修正为断言真实 sqlite3.IntegrityError，继续检查版本与 DDL 回滚，未放宽行为检查。

浏览器首次因沙箱 spawn EPERM 无法启动；授权后专项的计算/图表/保存成功，刷新处暴露测试
定位歧义（analysis 页签与同名目录按钮），改用独立 test ID 后完整 22 项通过。
本轮没有使用真实模型、外部 MCP、E2B、Docker 或 MinerU 实机服务；这些不能从本轮离线结果推断为通过。

Evals 产物：`.eval/data-analysis-2026-09-05/`（不入版本库）。
