# Workspace upload 与媒体设计验证

日期：2026-09-05。

## 本次范围

- 实现聊天区与 Workspace 面板拖拽上传，所有新上传原件归入逻辑 `upload/**`；首份成功上传时出现目录。
- 保留私有 file-ID 存储，Sandbox 从同一事实源物化真实 `upload/`；无历史数据移动或复制。
- PDF/DOCX/XLSX 自动转换结果独立放在 `documents/<stem>-<source-id>/**`；旧原件继续支持重解析。
- 上传代码只作为输入，工作副本使用 `scripts/**`；不允许覆盖原件或伪造工作代码摘要。
- 图片、视频链接仅提交[设计方案](../design/workspace-upload-media.md)，未新增 OCR、视觉请求、下载器或 ASR 运行时。

## 行为覆盖

1. 空 Session 没有占位 `upload/`；上传 PDF、图片、代码、DOCX、XLSX 后全部归入该目录。
2. 同名文件编号避让并保持字节，路径带括号时仍可正常 Sandbox 物化；不同 Session 文件隔离。
3. 子目录始终落在 `upload/` 内，路径穿越被拒绝；上传不能覆盖根指令。
4. 文档原件和转换产物分离；同名文档不混用输出或缓存；旧 `documents/<id>/original.*` 可解析及复用。
5. 转换失败保留原件、无半成品；固定转换器不能写回原件路径。
6. 工作代码摘要、重启恢复、stale/failed 状态继续有效；测试以真实工作副本替代旧的代码上传即工作代码假设。
7. Chromium 实际使用 DataTransfer 拖拽：聊天自动附加、Workspace 只归档；Excel 产物显示及同名文件刷新恢复。

## 门禁结果

| 检查 | 本轮结果 |
| --- | --- |
| Ruff（src/tests/scripts/evals） | PASS |
| strict Mypy（src/evals） | PASS，263 source files |
| 后端全量离线 | 2186 passed / 7 skipped / 9 deselected，453.74s；coverage 76.57%，达到 75% 门禁 |
| 前端 Vitest | 193/193，29 files |
| 前端 ESLint / typecheck / production build | PASS |
| Workspace Chromium 专项 | 5/5，包括两条实际拖拽链路 |
| 完整 Chromium E2E | 21/21，无重试；生产构建由 posttest 恢复 |
| 本机 Evals | 5 suites / 10 observations，candidate gate PASS |

后端使用 `D:\miniconda\envs\pipy\python.exe` 和 `PYTHONPATH=src`，默认 marker 排除真实
LLM、外部 integration、Docker。本轮不把离线 Gate 当作 MinerU、视觉模型或视频平台的真实验证。
首轮全量发现两项 Checkpointer 测试仍把上传代码等同于工作代码；更新测试准备后，定向 11/11
通过，再次完整运行得到上表最终结果，未删除或跳过这两项恢复检查。
浏览器首次因沙箱进程启动限制 EPERM 失败，授权后沙箱外重跑成功。全套运行时出现一次 Windows
连接关闭回调 `WinError 10054`，未影响 21 项结果；不将其当作功能测试失败或伪报无运行日志警告。

本机 Evals 产物位于 `.eval/workspace-upload-media-2026-09-05/`（不入版本库）。
