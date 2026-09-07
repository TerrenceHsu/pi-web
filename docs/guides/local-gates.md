# 本机一键门禁与 MinerU 实机验收

从仓库根目录、同一个 Python 环境执行。门禁不会安装依赖、拉镜像、读取 Provider Key、
替换业务服务或开启 Workspace Bash。首次环境准备需自行完成主项目 dev/web/data-analysis/sandbox-e2b
extras、前端与 E2E 的 `npm ci`、Chromium，以及 `scripts/setup_analysis_python.py`。
CI 使用 `uv sync --frozen`；独立分析环境的新安装从 `uv.lock` 导出约束，不漂移解析依赖。
`sandbox-e2b` 在门禁中用于离线 SDK 配置安全测试，不发起云调用；普通本机 Web 运行无需安装它。

```powershell
$env:PYTHONPATH = "src"
D:\miniconda\envs\pipy\python.exe scripts/run_local_gates.py
```

默认 `all` 顺序执行锁文件、Ruff、Mypy（显式检查 Linux 和 Windows 两套平台类型）、Worker 静态检查、分析环境只读探针、
后端（保留 75% 总覆盖率门槛，含分支统计）、Worker 单元、离线 Evals、前端 lint/type/unit/build、
Chromium（零重试）。退出码非零即不通过，不把失败前的子项通过当作整套通过。
`--group offline` 不启动浏览器；`--group browser --port 8121` 单独验浏览器；
`--dry-run` 只列命令。端口需空闲，CI 模式不会复用既有业务服务。

本机与 CI 统一使用 `python -m pytest`，让仓库根目录可用于 `tests`/`scripts` 的导入；
不要改成裸 `pytest` 后依赖开发机偶然存在的导入路径。复现 CI 时应在独立环境中按
`uv.lock` 安装，已有 conda 环境检查通过不能替代 frozen 依赖验证。

日志和逐项耗时/退出码位于 `.test-tmp/gate-<group>-<UTC>/`；Evals 每次使用
`.eval/local-gate/<unique-id>/` 保存独立证据，不覆盖上一次运行。
OS 锁阻止两个门禁共享 `.coverage`、basetemp 或静态构建；不要手动同时跑另一个测试进程。
每项最长两小时，超时终止该子进程树；浏览器开始后无论成功失败均重建 production bundle。
使用工作区 uv cache，不改宿主全局缓存。环境缺依赖/权限时失败，不自动降低覆盖率或跳过子项。

## Docker 专项

使用已经构建且确认过身份的 Bash runtime **完整 image ID**：

```powershell
python scripts/run_local_gates.py --group docker --docker-executable C:/YourDocker/docker.exe --image-id sha256:YOUR_64_HEX_IMAGE_ID
```

示例必须换成真实绝对 CLI 和 64 位 SHA。执行底层配置/隔离检查及标记 `docker` 的真实 Web 用例，
只建立与清理测试任务；不自动 pull/prune、不覆盖生产配置。Docker 专项不属于默认离线覆盖率。

## MinerU 三档

MinerU 的大模型和 CUDA 环境不属于默认门禁。新 Worker 镜像需要单独构建并核验 image ID；
旧版健康容器不能代替新源码。使用 `workers/wiki_parser_worker/compose.yaml` 的受限配置，
独立测试 exchange 和容器名称，绝不能指向业务队列；GPU 档额外使用 `compose.gpu.yaml`。

可选合成语料生成器 `scripts/create_mineru_fixture.py` 需要独立的 ReportLab/Pillow 作者环境；
它生成文字/表格页和纯扫描页。应先渲染并目视检查，再运行解析。合成样本不是真实质量基准。
真实语料须明确授权，包含中文、扫描、富表格/图片等有代表性的文档，并按 SHA 标识。

运行中 Worker 和脚本必须使用相同 exchange、路由版本及配置 hash：

```powershell
python scripts/smoke_wiki_parser_oci.py --exchange-root D:/YourTest/exchange --output-root D:/YourTest/results --routing-config workers/wiki_parser_worker/config/routing-quality-v1.json --timeout-seconds 1800 --case cpu-pipeline pipeline D:/YourTest/corpus.pdf
python scripts/smoke_wiki_parser_oci.py --exchange-root D:/YourTest/exchange --output-root D:/YourTest/results --routing-config workers/wiki_parser_worker/config/routing-quality-v1.json --timeout-seconds 1800 --case gpu-medium gpu-medium D:/YourTest/corpus.pdf
python scripts/smoke_wiki_parser_oci.py --exchange-root D:/YourTest/exchange --output-root D:/YourTest/results --routing-config workers/wiki_parser_worker/config/routing-quality-v1.json --timeout-seconds 1800 --case gpu-high gpu-high D:/YourTest/corpus.pdf
```

CPU 试验必须不暴露 GPU，GPU 两档分别记录。每档必须得到一个真实 MinerU succeeded attempt、
正确来源 SHA、制品 SHA/大小/页数；还需检查正文、表格与 OCR 预期，不以非空 tar 代替质量验收。
记录 image ID、GPU/驱动、路由配置 hash、隔离参数与资源上限。运行中取消、清理、重启恢复、
错误归因和无网络模型可用性需单独检查；缺项不能将整个 runtime manifest 改为 ready。
检查真实本机效果后才人工决定部署，门禁本身不激活业务 Worker。

固定 PyTorch 2.14 的 Worker 在加载 MinerU 前禁用可选 Triton native overrides，使用内置 CUDA
算子；否则首次 VLM 推理会尝试在只读 HOME 创建 JIT 缓存。此处理不等于禁用 GPU，也不更改
`read_only` 或 `/tmp:noexec`。接口见 [PyTorch python_native 文档](https://docs.pytorch.org/docs/stable/backends#module-torch.backends.python_native)。

## 证据分层

- Offline/Fake：验证契约和业务编排，不证明模型质量、CUDA 或网络环境。
- Browser：真实本地 Web/Chromium，但 LLM 是 Fake。
- Docker：固定真实镜像的隔离和 Web 执行链路。
- MinerU：真实版本、模型、三档解析和质量/资源证据；构建成功不等于验收成功。
- 远端 CI：配置已提交不等于 CI 实际通过；没有 remote 时只记录本机结果。
