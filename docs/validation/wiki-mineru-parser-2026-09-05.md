# LLM Wiki MinerU-only Parser 验证记录

> 日期：2026-09-05
> 结论：源码、Web 产品面、离线契约与发布包闭环通过；真实 OCI CPU/GPU 运行仍保持 `runtime_ready=false`

## 实现结果

- PDF 解析器唯一化为 MinerU 3.4.5，仓库已无旧解析器的实现、依赖、模式或文档引用。
- Web 只公开 `pipeline`、`gpu-medium`、`gpu-high` 三个固定档位，不接受任意 MinerU CLI 参数。
- `pipeline` 映射 MinerU `pipeline`；两个 GPU 档映射 `hybrid-engine` 和 `medium/high` effort，CUDA 不可用时 fail closed。
- 每个 Job 恰好一个 MinerU attempt；取消、超时、质量拒绝与依赖不可用都不会触发隐式回退。
- Adapter 消费 `middle.json` 与 `content_list.json`，生成连续逐页 Markdown/plain text；PNG/JPEG/WebP 按内容 SHA-256 命名并在配额计算前去重。
- Wiki schema v8 固定新档位，v7 按现有策略显式要求重建，避免旧数据被静默压成某一新档。
- Worker 自身采用 MIT，同时携带 MinerU 3.4.5 官方双语许可正文、Notice、SPDX SBOM、精确源码清单和可重现 wheel 校验。

## 供应链与运行边界

| 项目 | 已固定事实 |
|---|---|
| MinerU | `mineru[core]==3.4.5` |
| 源码标签 | `mineru-3.4.5-released` |
| Worker Python/Platform | CPython 3.12 / linux-amd64 |
| `uv.lock` | 132 packages；SHA-256 `102dc3d30199f2c87fa46956160f551387c6f13f6d422a62ec8aa51ba4b45209` |
| hash requirements | 131 packages；SHA-256 `8dd4d08e73e148c93f38110507a8cc24eb3131a119b0bd13f3ce4d1564d981eb` |
| 模型 | OCI 构建期通过 MinerU downloader 预取，Job 期间固定 local/offline |
| 隔离 | file queue、非 root、只读 rootfs、no capabilities、no-new-privileges、无对外端口 |

## 实际门禁

| 门禁 | 结果 |
|---|---|
| Ruff | PASS，0 errors |
| 主项目 strict Mypy | PASS，263 source files / 0 issues |
| Worker strict Mypy | PASS，18 source files / 0 issues |
| Backend 全量离线 | PASS，2182 passed / 7 skipped / 9 deselected，coverage 76.62% |
| MinerU 邻接与合规 | PASS，60 passed；路由、Adapter、去重、Contract、Queue、Ingestion、API、源码归档均覆盖 |
| Worker 离线单测 | PASS，10 passed |
| Frontend | PASS，29 files / 188 tests；ESLint、vue-tsc、production build 通过 |
| Chromium E2E | PASS，20/20，包含 Knowledge source 主旅程 |
| Local Evals | PASS，5 suites / 10 observations，candidate gate PASS |
| 主 wheel | PASS，410 entries；9 个 `wiki_parser` 文件；0 个 MinerU/Torch/Transformers 运行文件 |
| Worker wheel | PASS，30 entries；compliance verifier PASS |
| 旧解析器残留扫描 | PASS，全部旧名称零命中 |

## 未通过项

当前 Windows 主机没有可调用的 Docker CLI，因此未构建新 Worker 镜像，也未伪造
CPU pipeline、CUDA medium/high、断网运行和代表性 PDF 通过记录。这些门禁全部完成前，
`component-manifest.json` 与 `runtime-manifest.json` 继续保持 `runtime_ready=false`。

## 官方对照

- [MinerU 仓库](https://github.com/opendatalab/MinerU)
- [MinerU CLI 与 backend 用法](https://github.com/opendatalab/MinerU/blob/master/docs/en/usage/cli_tools.md)
- [MinerU 输出文件说明](https://github.com/opendatalab/MinerU/blob/master/docs/en/reference/output_files.md)
- [MinerU 官方许可](https://github.com/opendatalab/MinerU/blob/mineru-3.4.5-released/LICENSE.md)
