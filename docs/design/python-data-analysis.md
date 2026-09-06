# 本地 Python 数据分析：独立环境与逐次确认

日期：2026-09-05。按用户确认，采用独立本地 Python 环境，不依赖 Docker/E2B，
不将依赖隔离称为安全沙箱。固定 `analyze_data` 保留；新增可选 `run_python_analysis`。

## 使用

1. 在项目根目录用项目 Python 运行 `scripts/setup_analysis_python.py`。
   脚本创建 `.venv-analysis`，安装 pandas、NumPy、Matplotlib、openpyxl、PyArrow、Pydantic。
   不安装完整 Web/Provider SDK，不改 `pipy` 的计算依赖；已存在的环境可以再次检查/补装。
2. 重启 Web 后端，在 Workspace → extensions → Tools 启用 Python Data Analysis。
   默认关闭，按 Workspace 选择；缺少环境或探针失败时显示不可用，没有自动安装或主环境回退。
3. 上传 CSV/TSV/XLSX/Parquet，在聊天中要求用 Python 分析。
4. 确认卡展示完整代码、输入文件/逻辑路径、输入 SHA-256 和代码 SHA-256，以及本机执行风险。
   选择 **Approve once** 或 **Deny**。代码不会因选中工具或允许所有工具而自动运行。
5. 查看真实 stdout、表格、图表和执行代码。点击 Save results to Workspace 才保存标准报告包。
   报告/manifest 包含执行代码，可供复核；不自动向 `scripts/` 写入可执行文件。

默认解释器是当前源码项目的 `.venv-analysis/Scripts/python.exe`（Windows）或
`.venv-analysis/bin/python`（POSIX），与 Web 启动 cwd 无关。已安装发行包可从 cwd 查找，
或由本机部署者设置绝对路径环境变量 `PI_AGENT_ANALYSIS_PYTHON`。模型/API 参数不能指定解释器。
探针检查依赖能实际导入、资源限制可用及 `sys.prefix` 与 Web 进程不同，结果缓存 15 秒。

```powershell
$env:PYTHONPATH = 'src'
# 安装脚本使用 uv；若项目 Python 尚未安装 uv，先执行此行
D:\miniconda\envs\pipy\python.exe -m pip install uv
D:\miniconda\envs\pipy\python.exe scripts/setup_analysis_python.py
# 只验证、不安装
D:\miniconda\envs\pipy\python.exe scripts/setup_analysis_python.py --check
```

## Agent 接口

工具参数为 `file_id`、`code`，以及可选 `sheet`、`encoding`、`header_row`、`limit`。
`action` 固定为 `python`，默认无需发送。拒绝 `approved`、`executable`、宿主路径等额外字段。

代码得到预加载的 `df`、`pd`、`np`、`plt`。CSV/TSV 保留字符串和 ID 前导零，数值转换应显式完成。
将 DataFrame 或 Series 赋给 `result` 返回表格；`print` 返回文字；最后一个 pyplot Figure 作为 PNG。
每次启动新进程，不继承上一轮 Python 变量。轻量示例：

```python
df['sales'] = pd.to_numeric(df['sales'], errors='raise')
result = df.groupby('region', as_index=False)['sales'].sum()
print('总计', int(df['sales'].sum()))
plt.bar(result['region'], result['sales'])
```

Python 异常返回异常类型、有限错误文字及代码行号，Agent 可以据此修改代码再发起新的确认。
只有 `print` 而未设置 `result` 也可成功，表格为空；不支持任意对象序列化、pickle 或持久 Notebook。
XLSX 仍使用保存的公式缓存，缺失缓存时拒绝执行；不运行宏或重算公式。

## 确认与生命周期

- `DataAnalysisService` 是不可绕过的执行前门禁：没有确认回调、当前 Workspace 未选择或环境不可用，
  都不能启动 Python。每个新 run 的回调必须明确返回 `True`。
- Web 回调只认请求 ContextVar 中的活跃 request/session，直接调用本账号的 `ToolApprovalManager`；
  不使用可能串 Session 的 current-request fallback，也不使用可能自动批准的宿主通用 handler。
- `DefaultToolPermissionPolicy` 对具体 Python 工具交给服务执行确认，避免两次审批；显式 deny 仍优先。
  None/AllowAll/allowlist 均不能越过服务内的真实确认。
- 状态为 queued → awaiting_approval → running → succeeded/failed；拒绝为 failed +
  `python_execution_denied`，停止为 cancelled。等待确认最多 10 分钟，过期取消对应确认。
- 等待确认不占计算并发槽、不创建输入副本、不启动代码进程；确认后重新核对选择与输入 hash，
  副本字节不一致则失败。浏览器只能批准对应 ID，不能替换代码、输入或解释器。
- 每 Workspace 的活跃任务配额包含等待确认任务。取消/关服取消确认等待与计算；重启将所有未完成状态
  标为 interrupted，不自动重放。页面刷新可恢复仍在运行的 Web 请求确认卡。
- 固定分析 HTTP 创建 API 不接收 Python 代码。Python 仅由聊天发起；历史 Retry API 对 Python 返回
  `python_retry_requires_new_chat_approval`，要求重新从聊天发起确认，不能复用上次批准。

## 资源限制与信任边界

- 解释器以 `-I -B` 启动，专用 cwd、临时输入副本，不继承 Provider/MCP 凭证环境。
  初始化只加载计算模块，不加载 Web/Agent 包初始化逻辑。
- 使用既有 worker 限制：1 GiB 内存、60 秒 CPU、排队加执行 90 秒总超时。
  Windows Job Object 禁止子进程；POSIX 使用资源限制与进程组清理。
- 输入/结果限额沿用固定分析：25 MiB 输入、20 万行/256 列/100 万单元格、最多导出 5,000 行，
  预览 50 行、单文件 5 MiB、结果 JSON 2 MiB。代码最多 16,000 字符，stdout/stderr 合并保留约 16 KiB。
- 只发布固定四类产物（report.md、result.csv、manifest.json、可选 chart.png），保存前校验来源/hash，
  拒绝输出链接与非普通文件。普通输入副本与分析结果按账号/Session 管理。
- **这不是 OS 文件系统或网络隔离。** 用户批准的任意代码以 Web 后端运行用户的系统权限执行，
  技术上可访问本机其他文件、网络、密钥存储，也可能规避进程内防护。资源限制不能把恶意代码变安全。
  “仅提供当前文件副本”不等于“只能访问当前文件”；不承诺绝对保护原件或其他账号数据。
- 因而仅用于 localhost 的受信用户和审阅过的轻量代码，不能作为不受信多租户/公网任意代码执行服务。
  不自动安装依赖，不把网络任务或访问无关文件作为工具推荐用法。
- Python 代码、stdout、结果在私有分析历史/聊天中可见，保存时进入报告。聊天结果会发送给所选模型；
  本机运算不等于数据从不发给 Provider。Telemetry 只收安全状态/计数/耗时，不记录代码与数据正文。

测试和本机安装记录见 [验证记录](../validation/python-data-analysis-2026-09-05.md)。
