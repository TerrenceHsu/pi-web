# LLM Wiki MinerU PDF Parser

> 状态：源码、Contract、Web 装配与离线测试已完成；新 OCI 镜像和代表性真实语料 Gate 待执行
>
> 校准日期：2026-09-05

## 产品配置

Web 只公开三个固定档位，不接受任意 MinerU 参数：

| Web 档位 | MinerU backend | effort | 运行要求 |
|---|---|---|---|
| `pipeline` | `pipeline` | 无 | CPU 或 GPU |
| `gpu-medium` | `hybrid-engine` | `medium` | CUDA GPU |
| `gpu-high` | `hybrid-engine` | `high` | CUDA GPU；启用图片/图表分析 |

默认档位为 `pipeline`。GPU 档位在 CUDA 不可用时返回固定
`provider_unavailable`，不静默降级到 CPU，也不改用另一个档位。

## 主链路

```text
Web Source
  -> 不可变 PDF size/SHA 校验
  -> PersistentOciParserProvider file queue
  -> Worker 安全预检
  -> 固定档位映射
  -> 单次 MinerU 3.4.5 解析
  -> 逐页/图片规范化
  -> 质量门禁
  -> Contract v2 tar
  -> 主应用不可信制品复核
  -> ParseAttempt + ParseRevision + Artifact 原子发布
```

每个任务只允许一个 `mineru` attempt，不存在解析器回退。预检只拒绝加密、主动内容、嵌入文件、
页数/来源身份异常，并生成可审计元数据；不会根据文档内容偷偷改变用户选择的档位。

## 输出适配

Adapter 调用 MinerU 的 `do_parse`，要求 `source_middle.json` 和
`source_content_list.json` 各恰好一份。`pdf_info` 必须与输入页数一致；content list 按
`page_idx` 归并为连续的一基页码。文本、标题、公式、代码、列表及图片引用被转换为规范
Markdown，同时生成 plain text。PNG/JPEG/WebP 经 MIME 魔数、单图/总大小/数量配额校验后，
按内容 SHA-256 命名并去重。

主应用不导入 MinerU、Torch 或模型 SDK，只消费 provider-neutral Contract v2。所有运行时模型在
镜像构建阶段下载，Job 运行环境固定离线。

## 持久化与版本

- Wiki schema v8 保存 `pipeline | gpu-medium | gpu-high`，旧 schema v7 按项目既有策略要求显式重建。
- 记录 MinerU 版本、preset、路由配置 revision/SHA、质量报告、attempt 与 artifact SHA。
- 重解析保留旧 ParseRevision；失败不会切换 selected revision。
- Web Session/Workspace 并行不改变 Parser Worker 的单任务隔离与单并发 supervisor 边界。

## 供应链与许可

Worker 自身采用 MIT；MinerU 3.4.5 使用其独立的
`LicenseRef-MinerU-Open-Source-License`。`NOTICE.md`、`MINERU_LICENSE.md`、SPDX SBOM、完整
`uv.lock` 和 hash requirements 随源码归档提供。Web 的档位名称明确展示 MinerU。

当前 `runtime_ready=false`：只有新镜像完成模型预取、断网运行、CPU pipeline、两个 GPU 档位、
取消/超时/恢复、制品篡改和代表性 PDF smoke 后，才能改为 `true`。
