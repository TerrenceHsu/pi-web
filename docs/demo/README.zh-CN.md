# pi-web 使用演示

[中文](README.zh-CN.md) | [English](README.en.md) · [返回首页](../../README.md)

本演示使用仓库内完全合成的资料，不需要上传私人文件。前两段无需真实模型；
后两段需要显式配置 Provider 或 Docker，不能用模拟回复冒充实机智能效果。
这是可复现的操作与录屏脚本，不是已经录制的视频。

## 准备

按 [README 快速开始](../../README.md#快速开始)安装依赖并构建前端。
建议单独使用一个新的演示数据目录和空闲端口，避免展示真实会话、凭证或后台任务：

```powershell
$env:PYTHONPATH = "src"
$env:HOST = "127.0.0.1"
$env:PORT = "8135"
$env:EXTRA_UI_ORIGINS = "http://127.0.0.1:8135"
$env:PI_AGENT_DATA_DIR = Join-Path (Get-Location) ".test-tmp/pi-web-demo"
$env:PI_AGENT_SECRET_BACKEND = "memory"
$env:PI_LOCAL_DOCKER_ENABLED = "0"
python scripts/dev_web_app.py
```

从仓库根目录运行，确认上述目录尚未使用；已有演示时换一个新的目录名，不清空旧目录。
打开 `http://127.0.0.1:8135`。自定义端口必须设置匹配的 `EXTRA_UI_ORIGINS`，否则登录会因 Origin 校验而拒绝。
环境变量只作用于该终端及其子进程；演示结束关闭终端，
不要用它继续启动业务服务。演示数据不会自动删除。Memory 模式下不保存 API Key。
首次使用 `admin / 123456` 登录，并在不录制密码的情况下先通过 Password 改密。
离线片段不要创建真实 Provider 或启用外部 MCP。

## 演示 A：聊天、原件与 Workspace（约 2 分钟）

1. 点击 **+ New chat**，命名为 `Demo: sales review`。
2. 将 [project-notes.md](../../examples/demo/project-notes.md) 和
   [sales.csv](../../examples/demo/sales.csv) 拖入聊天区或 Workspace。
3. 在文件树中确认 `upload/project-notes.md`、`upload/sales.csv`；打开 CSV 预览。
4. 输入：`你好，这是离线界面演示。`，观察模拟文本逐段出现。
5. 刷新页面，确认仍是同一 Session，消息与文件保留。
6. 新建第二个 Session，确认它没有前一个 Session 的上传原件或工具选择，再返回。

**预期：**模拟答复为 `Hello from delayed fake backend`。它不理解资料，也不会自主调用工具。
此段证明的是界面、持久化和 Workspace 隔离，不是模型推理能力。

## 演示 B：无需 LLM 的真实固定数据分析（约 2 分钟）

1. 回到 `Demo: sales review`，打开 Workspace → **extensions** → Tools。
2. 启用 **Data Analysis**（`analyze_data`），切到 **analysis** 页。
3. 选择 `sales.csv`，操作选择 `chart`，图表类型选 `bar`，X 填 `region`，Y 填 `sales`；柱图固定按 X 对 Y 求和。
4. 点击运行，检查表格和柱状图，然后点击 **Save**。
5. 检查结果保存到 `artifacts/analysis/<run-id>/`；刷新后仍能打开历史结果。
6. 再查看 `upload/sales.csv`，确认上传原件没有被改写。

这一步在本机工作进程做真实计算，不执行任意 Python，也不需要模型或 Docker。

| 合成数据检查项 | 预期值 |
| --- | ---: |
| 源数据行数 | 6 |
| North 销售额 | 2600 |
| South 销售额 | 3400 |
| 总销售额 | 6000 |
| 一月 / 二月（另做月份汇总时） | 2700 / 3300 |

下图是本项目在独立演示 Workspace 中实际生成的图表，不是模型或绘图工具合成的界面截图：

![合成销售数据的真实本机分析结果](assets/sales-by-region.png)

本次已执行的验证与未执行片段见 [演示验收记录](VALIDATION.md)。

## 演示 C：真实模型、Wiki 与观测（约 3 分钟）

本段需要真实 Provider，可能产生费用；请只用合成资料，不在录屏中展示 API Key。

1. 在 Providers 创建并绑定真实 Profile，再发送：

   > 阅读本 Workspace 的 project-notes.md，列出已确认需求与待定事项。不要修改文件，不要运行代码；为每项注明来源。

2. 打开 **Knowledge**，新建 `Demo Studio` Space，上传
   [wiki-source.html](../../examples/demo/wiki-source.html)。这是零网络 HTML 解析，不需要 MinerU。
3. 在该 Space 的对话中请求：

   > 根据当前来源整理项目入口页。把“已确认需求”和“待定事项”分开。先生成提案，等待我审核发布。

4. 核对 Change Set 的内容和来源，只有正确时才批准；随后检查 Pages 与来源关系。
5. 打开管理员 **Telemetry**，查看本次请求的状态、耗时、token 与工具记录。
   无内容字段是隐私设计，不等于该请求未把资料发送给模型 Provider。

**不要宣称：**HTML 演示验证了 PDF/OCR 质量。MinerU 三档需要独立 Worker 和实机验收；
CPU 表格仍有已知问题，`runtime_ready=false`。图片/视频链接的 Workspace 解析也未实现。

## 演示 D：可选的代码执行审批（约 3 分钟）

只有管理员按 [Bash runtime 指南](../../docker/bash-runtime/README.md)准备并验证了固定 Docker
镜像，且在专用演示服务和 Workspace 显式启用后，才演示本段。不要将这些设置应用到业务服务。

示例提示词：

> 在本 Workspace 的受管副本里，用 Python 标准库读取 upload/sales.csv，生成按地区汇总的 JSON。请先展示任务范围和需要执行的代码，等待我批准。不联网、不安装依赖、不修改原件；结果写回另行确认。

展示顺序：任务/脚本审批 → 执行结果 → 冻结制品及差异 → 单独批准发布 → Workspace 中查看结果。
可先演示 **Deny**，证明没有授予执行权限，再发起新的可审批任务。
不要录制“自动同意所有执行”的演示。命令退出码为 0 或输出完整，不代表需求已被验证。

若改用 `run_python_analysis`，它是本地独立 Python 环境、逐次代码确认，**不是 Docker 沙箱**。
不要把两种执行方式混称为同一个安全边界。

## 录屏脚本

| 时间 | 画面 | 中文旁白 |
| --- | --- | --- |
| 00:00–00:20 | 新会话 | “pi-web 是本机 Web Agent，演示只使用合成资料。” |
| 00:20–01:20 | 拖拽、预览、刷新 | “每个会话有独立 Workspace；原件统一进入 upload，刷新后仍可恢复。” |
| 01:20–02:00 | 模拟流式回复 | “这是模拟 Provider，只演示流式界面，不代表真实模型效果。” |
| 02:00–03:30 | 固定分析、图表、保存 | “本机实际计算：North 2600、South 3400；保存结果不改原件。” |
| 03:30–04:30 | 已配置真实模型后的提案 | “模型生成的是待审提案；只有批准的页面进入当前知识库。” |
| 04:30–05:00 | Telemetry | “管理员查看运行元数据，面板不收集消息正文。” |

C/D 的先决条件未满足时，省略该片段并注明“未演示”，不要用模拟画面替代实机验收。
停止录制前检查浏览器地址栏、下载列表、对话内容与终端，避免包含真实文件路径或凭证。

## 如何验证演示

固定分析预期值可用 Python 标准库独立核对；浏览器覆盖对应
[分析测试](../../tests/e2e/data-analysis.spec.ts)、
[Workspace 测试](../../tests/e2e/workspace-results.spec.ts)和
[Telemetry 测试](../../tests/e2e/telemetry-admin.spec.ts)。
这些浏览器回归使用 Fake Provider。完整门禁与真实 Docker/MinerU 的证据层次见
[本地门禁指南](../guides/local-gates.md)，不要将已编写的演示脚本等同于通过了所有演示实机验收。
