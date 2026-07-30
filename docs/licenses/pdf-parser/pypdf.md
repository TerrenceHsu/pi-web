# pypdf License Record

> **Scope**：R2 MVP PDF Parser 第三方许可证归档（per `p2-r2-0-pdf-parser-license-gate.md §18`）。
> 本文件**不**复制完整第三方 license 正文——项目当前无统一第三方 license archive 惯例。
> 完整许可证文本见 [pypdf GitHub LICENSE](https://github.com/py-pdf/pypdf/blob/main/LICENSE）。

## Package metadata

| 字段 | 值 |
|---|---|
| Package | pypdf |
| Integration range（pyproject `[rag]` extra） | `>=6.0,<7` |
| Audited implementation baseline | 6.14.2 |
| Actual locked version（uv.lock） | 6.14.2 |
| Actual installed version（pipy env） | 6.14.2 |
| SPDX | BSD-3-Clause |
| Source organization | py-pdf（GitHub: py-pdf/pypdf） |
| PyPI URL | https://pypi.org/project/pypdf/ |
| Retrieved at | 2026-07-31 |

## Selection rationale（per `p2-r2-0-pdf-parser-license-gate.md §14`）

- BSD-3-Clause 宽松（OSI 批准，无 copyleft，无收入门槛，无反竞争条款）
- 0 直接依赖（默认；`typing_extensions` 仅 Python<3.11 conditional）
- 纯 Python（无 native binary）
- 完全离线（无 HuggingFace / 无模型下载 / 无 LLM client）
- 符合 R2 编码不变量（External Provider=0 / Model downloads=0 / OCR=disabled / Remote LLM=disabled）

## Key license obligations（BSD-3-Clause）

1. 保留 copyright notice（pypdf wheel/sdist METADATA 已携带）
2. 保留 binary form 中的 notice
3. 不使用 contributor 名字做 endorsement

**无 NOTICE 文件义务**（BSD-3-Clause 三 clause 之一仅约束 endorsement）。

## 排除的 extras（per `p2-r2-0-pdf-parser-license-gate.md §17.1`）

- ❌ `[crypto]` extra（cryptography）—— R2 第一版不处理加密 PDF；encrypted PDF → `encrypted_pdf` 错误码
- ❌ `[image]` extra（Pillow）—— R2 第一版不处理 PDF 内图片
- ❌ `[full]` extra
- ❌ `[cryptodome]` extra

## Upgrade policy（per `p2-r2-0-pdf-parser-license-gate.md §20`）

| 升级类型 | 操作 |
|---|---|
| 6.x patch / minor | 重跑 parser contract + fixture regression；更新本文件 last_verified date |
| 7.x major | 重新执行 License Gate + API compatibility audit |

## Last verified

- 日期：2026-07-31
- 验证人：R2-0 License Gate（自动审计）
- 审计来源：[p2-r2-0-pdf-parser-license-gate.md](../../design/p2-r2-0-pdf-parser-license-gate.md) §5-§9
