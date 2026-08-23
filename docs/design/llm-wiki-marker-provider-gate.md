# LLM Wiki Marker Gate 与 Parser Provider Contract

> 决策日期：**2026-08-23**  
> 状态：**阶段 1 已冻结；真实 Marker Adapter 尚未接入**  
> 适用设计：[`llm-wiki.md`](llm-wiki.md)

## 1. Gate 结论

阶段 1 只允许 provider-neutral 契约和完全离线 Fake 进入主仓库。真实 Marker 运行时必须在
独立本机容器中运行，不能成为主应用依赖，也不能与主应用共享 Python 环境。

冻结结论如下：

1. 审核基线固定为 `marker-pdf==2.0.0`，不得使用浮动版本。
2. Marker 代码是 Apache-2.0，但随其分发和自动下载的模型使用修改版 AI Pubs
   OpenRAIL-M；两者必须分别审核，不能用代码许可证替代模型许可证判断。
3. Marker 模型许可证包含使用限制、输出义务和上一财年收入门槛。应用不自行给用户作法律
   资格判断；真实 Provider 在 `license_mode=unconfigured` 时必须 fail closed。
4. MVP 只允许 `fast_no_ocr`：不调用 Marker LLM/VLM，不承诺扫描件 OCR 或公式识别，只处理
   PDF 自带文本并提取 PDF 内嵌图片。
5. 解析任务期间网络必须关闭。镜像安装、依赖下载或未来模型预取属于单独的管理员维护操作，
   不能在上传文档后的任务路径中隐式发生。
6. 发布模式使用本机 OCI 容器；同机独立 venv 进程只能作为显式开发模式，不能满足 Worker
   对 Wiki 数据的强文件系统隔离验收。
7. 当前阶段不安装 Marker、Torch、Transformers 或 Surya，不实现真实 Adapter，也不接入
   `WikiStore`。

## 2. 审核证据

审核只以 Marker `v2.0.0` tag 的上游材料为准：

- [Marker v2.0.0 release](https://github.com/datalab-to/marker/releases/tag/v2.0.0)
- [v2.0.0 README](https://raw.githubusercontent.com/datalab-to/marker/v2.0.0/README.md)
- [v2.0.0 pyproject.toml](https://raw.githubusercontent.com/datalab-to/marker/v2.0.0/pyproject.toml)
- [Apache-2.0 code license](https://raw.githubusercontent.com/datalab-to/marker/v2.0.0/LICENSE)
- [Modified OpenRAIL-M model license](https://raw.githubusercontent.com/datalab-to/marker/v2.0.0/MODEL_LICENSE)
- [PyPI marker-pdf 2.0.0](https://pypi.org/project/marker-pdf/2.0.0/)

上游 `pyproject.toml` 声明 Python `>=3.10`，并直接依赖 Torch、Transformers 和 Surya OCR；
即便 MVP 选择禁用 OCR/VLM，这些依赖也不能因此进入主应用环境。上游 README 同时说明
`--disable_ocr` 会跳过扫描件和公式。内置的简单 HTTP server 不是健壮的生产服务，也没有提供
本项目需要的完整固定契约，因此不直接使用它。

## 3. 许可证 Gate

`ParserProbe.license_mode` 是强制、secret-free 的运行时证据：

| 模式 | 含义 | Provider 可用性 |
|---|---|---|
| `unconfigured` | 管理员尚未确认适用授权路径 | 必须 `available=false`，错误为 `license_not_configured` |
| `self_hosted_eligible` | 管理员显式确认其使用符合当前模型许可证 | 可继续探测运行时；不代表应用提供法律意见 |
| `commercial` | 管理员配置了适用的商业授权 | 可继续探测运行时 |
| `not_required` | 仅允许不使用 Marker 模型许可证的 Fake | Marker Provider 禁止使用 |

授权确认必须是单独的管理配置，记录版本、模式和确认时间，但不得保存合同正文、付款信息或凭证。
Marker 升级、模型许可证变化或运行模式变化会使原确认失效并重新 Gate。

## 4. 运行与分发边界

### 4.1 发布模式

首个可发布 Marker Adapter 使用本机 OCI 容器：

- 镜像固定 Marker 与全部传递依赖，使用 digest 而不是浮动 tag。
- 每个解析任务创建独立容器；输入目录只读挂载，输出目录单独读写挂载。
- 不挂载 Wiki `pages/`、`wiki.db`、Session Workspace、项目根、Keyring、Docker socket 或主应用
  配置目录。
- 使用非 root 用户、只读根文件系统、`no-new-privileges`、capability drop、PID/CPU/内存限制，
  网络模式固定为 none。
- Probe 使用独立短生命周期容器，不能复用或恢复用户解析任务。
- 任务结束、失败、取消和超时后都必须停止并移除容器；`destroy` 幂等。

默认契约上限是：30 分钟、250 MiB 原件、512 MiB 制品、2048 个制品文件、1024 张图片、
单图 64 MiB。容器初始资源档位冻结为 4 vCPU、8 GiB RAM、4 GiB 临时空间；阶段 3 的真实
基准可把它们调高，但不得绕过任务级文件与时间配额。超限返回固定 `resource_limit`，不能泄露
容器 stderr 或宿主路径。

### 4.2 开发进程模式

独立 venv/进程可以用于 Provider 开发和诊断，但不作为发布验收结果。它仍与主应用共享宿主用户
权限，无法强制保证 Worker 不能读取其他目录。开启该模式必须使用显式开发开关，UI 和日志要
标记 `isolation_degraded=true`，并且不能被默认配置静默选中。

### 4.3 安装与升级

Marker 镜像安装/升级是显式管理员动作。构建时允许联网，解析时不允许联网。构建产物必须记录：

- Marker 精确版本和 wheel hash。
- Python 版本及完整 lock/hash。
- 镜像 digest。
- 代码许可证和模型许可证版本/hash。
- 运行模式 `fast_no_ocr`。

阶段 3 接入前需要用户提供可用的 Docker/Podman-compatible 本机容器运行时，并在应用中选择
`self_hosted_eligible` 或 `commercial`；阶段 1 本身不需要任何配置。

## 5. Provider Contract v1

独立顶层包 `wiki_parser` 是主应用与未来 Sidecar Adapter 共享的唯一接口面。它不 import
`pi_agent_core_py`、Marker、Torch、Surya、Transformers、FastAPI 或容器 SDK。

### 5.1 生命周期

```text
probe()
create_job(spec, source_path) -> handle
status(handle) -> status
wait(handle, signal) -> terminal status
download_artifact(handle, local_path, expected_sha256) -> receipt
cancel(handle) -> status
destroy(handle)
```

不变量：

- `create_job` 在任务创建前核对不可变原件的大小、SHA-256 和文件身份。
- `status` 只观察，不启动、恢复或替换任务。
- `wait` 必须响应协作取消，并在 `ParserLimits.timeout_seconds` 内终止为
  `failed/request_timeout`。
- `cancel` 和 `destroy` 幂等；业务编排仍必须在 `finally` 调用 `destroy`。
- Handle/Status 可以持久化，但不得包含原件内容、本机路径、容器 stderr 或凭证。
- 原始 SDK、进程、容器和协议异常只能映射为固定 `ParserErrorCode`。

### 5.2 固定请求

Contract v1 只接受：

- `application/pdf`
- `mode=fast_no_ocr`
- `extract_embedded_images=true`
- `output_schema=llm-wiki-parser-artifact/v1`

请求模型 `extra=forbid`，没有任意 CLI 参数、Marker 配置、LLM/VLM、网络、输出路径或环境变量
透传入口。

### 5.3 固定制品

Provider 输出一个不可变确定性 tar：

```text
manifest.json
parsed.md
images/<content-addressed-name>.png|jpg|webp
```

manifest 声明来源 SHA、Provider/version、模式、页数、Markdown 和每张内嵌图片的大小、SHA、
MIME 及可用页码。相对 POSIX 路径必须规范化；拒绝绝对路径、`..`、反斜杠、控制字符、重复和
大小写冲突。主应用在阶段 3 仍须把整个 tar 视为不可信输入，独立重算并核验，而不是信任
Provider manifest。

## 6. 完全离线 Fake

`FakeParserProvider` 不打开 socket、不启动进程、不读取 Provider 环境变量，也不 import Marker。
它仍真实执行来源身份/大小/SHA 检查、确定性 tar 构建、逐文件 hash、配额、超时、取消、原子下载
和销毁语义。可注入输出、固定 ID、时钟、完成 gate 和测试超时，以覆盖编排的成功和故障路径。

Fake 不是 Marker 行为模拟器，不声称验证排版/OCR；它只验证 Provider Contract 与主应用编排。

## 7. 阶段 1 验收

- Ruff：PASS。
- strict Mypy：7 个相关文件，0 issues。
- Provider Contract/Fake：29 passed，全程离线。
- Backend 非网络全量：3897 passed、8 skipped、12 deselected，83.44% coverage。
- 包边界测试确认 `wiki_parser` 可独立 import，Marker/Torch/Surya/Transformers/FastAPI 不会被
  加载，发布 wheel 包含 `src/wiki_parser`，依赖元数据不含 Marker/Torch。
- 真实 Marker、WikiStore、上传 API 和前端均未进入本阶段。

## 8. 下一 Gate

阶段 2 只实现全新的 `WikiStore`、目录和旧库只读备份，不需要 Marker 配置。阶段 3 开始真实
Raw Ingestion 与 Marker Adapter 时，必须先完成容器运行时检测、许可证模式配置、镜像构建/拉取
和真实 PDF 配额/取消/断网 smoke；任何一项不满足都不得把 Provider 标为 available。
