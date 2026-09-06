# 本地 Python 分析验证记录

日期：2026-09-05。用户确认采用“独立本地 Python 环境＋执行前确认”；不使用 Docker。

## 本机环境

- Web/测试 Python：`D:\miniconda\envs\pipy\python.exe`，所有项目测试设置 `PYTHONPATH=src`。
- 专用环境：`D:\LLMTutorial\test\.venv-analysis\Scripts\python.exe`，Python 3.12.13。
- 通过项目 Python 运行 `scripts/setup_analysis_python.py` 创建并安装，`--check` 真实探针通过。
- 已安装 pandas 3.0.5、NumPy 2.5.2、Matplotlib 3.11.1、openpyxl 3.1.5、PyArrow 25.0.1、Pydantic 2.13.5。
- 依赖下载经用户授权；没有更新 Web 主环境依赖、安装 Docker、启动远程代码服务或调用真实模型。

## 验证

- Python 专项：`pytest tests/test_python_data_analysis.py --no-cov -q`，**18 passed**。
- 既有固定分析/工具契约/Web 审批：**30 passed**。
- Ruff `src tests scripts evals`：PASS；strict Mypy `src evals`：**274 source files PASS**。
- 前端 Vitest：**200/200，30 个文件**；ESLint、vue-tsc：PASS。
- 真实 Chromium Python 专项：**2/2 PASS**，包含批准/拒绝、完整代码、刷新恢复、真实计算/PNG/保存。
- 本机 Evals：5 suites / 10 observations，candidate gate PASS；产物 `.eval/python-analysis-2026-09-05/`。
- `git diff --check`：PASS。
- 主项目 `uv lock --check --offline`：PASS，115 packages；专用环境独立安装，实际版本记录在每次结果 manifest 中。
- 完整 Chromium：**24/24 PASS，11 个规格，1.7 分钟，0 retry/0 flaky**；posttest 生产构建 PASS。
- 完整后端：`pytest tests/ --tb=short -q`，**2231 passed、7 skipped、9 deselected**，
  **542.38 秒，coverage 76.43% ≥ 75%**。使用原有 `.pytest-tmp`；此前 Wiki 路径失败全部消失。

专用解释器未安装 coverage 插件，worker 通过真实子进程的输入/输出黑盒测试验证；覆盖率不会把
子进程执行行算作主进程命中，也没有为过门禁而排除这些新增源码。

覆盖重点：模型不能设置 `approved`/解释器；环境不可用不回退；确认前不启动代码；默认策略、
无策略、AllowAll 均只有一次真实确认；浏览器额外 code 字段不能替换待执行代码；来源改变/关闭工具
会阻止执行；等待确认时取消或超时不执行；真实超时清理；代码异常行号/stdout；重试重新确认；
重启 interrupted 不重放；账号/Session API 归属；保存不再次执行且含代码/来源证据；Telemetry 无代码正文。

## 迭代中遇到的环境问题

1. 初次安装 uv 默认缓存受沙盒限制；改为项目内缓存并授权下载，独立环境成功安装。
2. 未授权启动 Chromium 时 `spawn EPERM`，不是功能失败；授权后两项专项浏览器测试通过。
3. 首次全量后端主动选择了较长的 `.test-tmp/python-analysis-full` 路径，Wiki 的深层临时文件触及
   Windows 路径长度限制，结果 **2224 passed、7 failed、7 skipped、9 deselected**（510.89 秒）。
   失败均为 Wiki 文件 I/O，Python 分析均通过。使用原有短目录复验 `test_wiki_api.py` +
   `test_wiki_worker.py`，**18/18 PASS**；不修改 Wiki 代码来掩盖环境问题。
4. 已核对并停止未授权浏览器尝试遗留的 8057 端口测试后端；没有停止或重启用户开发后端。

此功能不是恶意代码安全沙箱：同系统用户权限仍可访问文件/网络。测试证明的是确认门禁、
受信轻量代码流程和资源限制，不是 OS 级权限隔离或公网多租户安全性。
