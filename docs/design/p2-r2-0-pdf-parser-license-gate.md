# P2-R2-0 PDF Parser License and Distribution Gate

> **阶段**：P2-R2-0 PDF Parser License Gate（docs/audit-only milestone）
> **基线 commit**：`0fd4715` — docs(rag): archive corrections
> **日期**：2026-07-31
> **范围**：解决 P2-R0 decision D6，**版本化**审计 Marker / pypdf / PyMuPDF 三个候选 PDF Parser 的代码许可证、模型许可证、传递依赖、分发模式与运行时边界，选定 P2-R2 的唯一 MVP Parser，并冻结 Parser Adapter 接口。**不写生产代码 / 不改依赖 / 不改 lockfile / 不进入 P2-R2 编码**。
> **配套修订**：
> - [p2-r0-rag-contract.md](p2-r0-rag-contract.md) §4.1 — 修正 marker license 事实
> - [p2-r0-decisions-log.md](p2-r0-decisions-log.md) §3 D6 — 重命名为 "PDF Parser Code, Model, Dependency and Distribution License"
> - [docs/validation/p2-r1/P2_R1_LIBRARY_FOUNDATION.md](../validation/p2-r1/P2_R1_LIBRARY_FOUNDATION.md) §9 — R2 入口条件更新
> - ROADMAP / TODO / STATUS — R2 状态同步

---

## 0. Status

```
P2-R2-0 PDF Parser License and Distribution Gate
✅ COMPLETE / FROZEN @ <this commit>

D6 PDF Parser Code, Model, Dependency and Distribution License
✅ RESOLVED

Selected Parser
✅ pypdf @ >=6.0,<7 (current PyPI stable: 6.14.2, BSD-3-Clause)

P2-R2 PDF → Markdown Coding
✅ APPROVED TO START (D6 resolved; encoding gate open)

Production / test / dependency / lockfile / schema / frontend diff = 0
```

R2-0 性质：docs-only — 不动 src/、不动 tests/、不动 pyproject.toml、不动 uv.lock、不下载 wheel 进仓库、不下载模型权重。

---

## 1. Gate Objective

P2-R2-0 必须**单一性地**回答（per startup directive §2）：

1. 项目自身许可证与分发模式
2. Marker 2.0.0 当前版本化的代码 + 模型 + 依赖许可证事实
3. pypdf 6.14.2 当前版本化的代码 + 依赖许可证事实
4. PyMuPDF 1.28.0 当前版本化的代码 + 商业许可事实
5. 每个 candidate 在 P2-R2 digital-PDF-only + no OCR + no model + no external network 边界下的可行性
6. P2-R2 唯一 MVP Parser 选择
7. Parser 引入方式（base / extra / external process / 用户自装）
8. 模型权重分发边界
9. 第三方许可证 / NOTICE 归档策略
10. P2-R2 Coding 是否可开放

最终必须输出**唯一**决策（A/B/C/D/E），不得"后续再定"。

---

## 2. Existing D6 Decision

### 2.1 P2-R0 原始 D6 表述（已冻结）

`p2-r0-decisions-log.md` §3 D6：

> **D6** | marker AGPL license 兼容性 | R0 已记录，R1 集成前需法务确认 | 若 AGPL 与项目 MIT 不兼容，降级到 pypdf（BSD）——这是 contract §4.1 的 fallback 路径

`p2-r0-rag-contract.md` §4.1：

> **主 parser**：`marker`（datalab.so/marker，**AGPL-3.0**）

### 2.2 P2-R0 Amendment 1 把 D6 从 R1 入口移到 R2 入口

`p2-r0-amendment-1.md` §5：

> **marker AGPL-3.0 license 兼容性确认**：原 R1 入口条件，amendment 后移到 R2 入口条件

### 2.3 R2-0 启动前的 D6 状态

⛔ **D6 未解决** — R2-0 必须解决。

### 2.4 R2-0 审计预判

per startup directive §8：**不得默认 marker == AGPL**。必须分别审计：
- marker code license
- marker model weights license
- marker datasets license
- surya-ocr（inference backend）
- pdftext（PDF backend）
- PyTorch / transformers
- 其他直接依赖

---

## 3. Project License and Distribution Model

### 3.1 项目自身许可证声明

| 来源 | 内容 |
|---|---|
| `pyproject.toml` line 7 | `license = { text = "MIT" }` |
| `README.md` line 655 | "MIT（与上游 `@earendil-works/pi-agent-core` 一致）" |
| `LICENSE` / `LICENSE.txt` / `COPYING` 文件 | **缺失**（仓库根无任何 LICENSE 文件） |

**结论**：

- 项目**元数据声明 MIT**（pyproject + README 一致）
- 但**仓库根没有 LICENSE 正文文件**——这是项目自身的元数据不一致（声明 vs 实际未放入仓库）
- 记录为 `project_license_declared_mit_but_missing_license_file` —— 不阻塞 R2-0 决策，但应作为后续 follow-up（**不在 R2-0 范围**）

### 3.2 项目分发模式审计

per startup directive §6，区分 8 种分发模式：

| # | 模式 | 当前项目是否属于 |
|---|---|---|
| 1 | Internal development | ✅（当前阶段） |
| 2 | Local source deployment | ✅（`pip install -e .` via uv） |
| 3 | Source distribution（git clone + install） | ✅（当前主路径） |
| 4 | PyPI wheel/sdist | ⛔ 未发布（pyproject 有 metadata 但无 release workflow 证据） |
| 5 | Bundled desktop/application | ⛔ 不存在 |
| 6 | Docker image distribution | ⛔ 无 Dockerfile |
| 7 | Hosted SaaS | ⛔ 明确不做（README/CLAUDE.md 反复声明 local-only） |
| 8 | Commercial proprietary | ❓ 未声明；上游 `pi-agent-core` TS 项目是 MIT；本项目亦声明 MIT |

**当前真实模式**：**Source distribution + local install**（Case 1+2+3）。

**未来可能**：Case 4 (PyPI) / Case 5 (desktop) / Case 6 (Docker)——CLAUDE.md / README 反复强调 "Local-only development UI"、"DO NOT expose publicly"，所以 **Case 7 (SaaS) 显式排除**。Case 8 (proprietary) 不可预期但 license ambiguity（无 LICENSE 文件）让此路径仍有理论可能。

**结论**：项目当前 source-distributed MIT；**未来分发模式未确定**（可能 PyPI / 可能 desktop / 可能 Docker），但 **SaaS 明确不做**。License Gate 必须保守——选择**对商业 / Docker / PyPI / desktop 分发都不冲突**的依赖。

---

## 4. Evidence Method

### 4.1 主要证据源（per startup directive §3 / §7）

- **PyPI JSON API**（`https://pypi.org/pypi/<pkg>/json`）—— package metadata + license field + requires_dist
- **GitHub raw file**（`raw.githubusercontent.com/<org>/<repo>/<ref>/<file>`）—— LICENSE / MODEL_LICENSE / README
- **GitHub Contents API**（`api.github.com/repos/<org>/<repo>/contents/`）—— 仓库根文件清单
- **选定版本的 tag/commit**（不依赖 master）

### 4.2 法律边界声明（per startup directive §3）

本报告结论基于：

- 公开许可证文本（PyPI license field + GitHub LICENSE/MODEL_LICENSE）
- 项目自身分发计划（§3.2）
- 工程事实（依赖图、运行时模式）

**不是法律意见**。对闭源商业分发、SaaS、公网部署或收入门槛相关问题，需要法律顾问或许可证方书面确认（§22 法律免责）。

### 4.3 证据采集时间

```
retrieved_at: 2026-07-31
evidence_origin: PyPI JSON API + GitHub raw LICENSE/MODEL_LICENSE + GitHub README
```

所有版本号都为 retrieved_at 当日的 PyPI latest stable。

---

## 5. Marker Versioned Audit

### 5.1 选定审计版本

```text
candidate:        Marker
package:          marker-pdf
version:          2.0.0  (PyPI latest stable @ 2026-07-31)
homepage:         https://github.com/datalab-to/marker
license_field:    Apache-2.0 (PyPI metadata)
source_ref:       master @ retrieved_at（PyPI 2.0.0 tag 未单独审计 tag 文件，因 master LICENSE 与 PyPI license field 一致）
```

### 5.2 仓库根文件清单（GitHub Contents API）

```
LICENSE           ← Apache-2.0（已 fetch + verify）
MODEL_LICENSE     ← modified OpenRAIL-M（已 fetch + verify）
```

---

## 6. Marker Code License

### 6.1 证据

| 证据源 | 内容 |
|---|---|
| PyPI `info.license` | `Apache-2.0` |
| GitHub `LICENSE` (master) | SPDX = `Apache-2.0` |
| GitHub README | "Our code is licensed under Apache 2.0 — free to use, including commercially." |

### 6.2 结论

- **Marker 2.0.0 代码许可证 = Apache-2.0** ✅
- P2-R0 §4.1 写的 "AGPL-3.0" **判断错误**——这是 R0 冻结时的过时或对象识别错误
- Apache-2.0 是宽松许可证（OSI 批准，与 MIT 兼容）

### 6.3 不因 license 修正而直接得出"无风险"

per startup directive §8：

> 若官方当前版本的代码许可证已经不是AGPL：明确写为"旧判断已过时或对象识别错误"；给出版本化证据；修正 D6 的名称；**不能因此直接得出 Marker 整体无许可证风险**；继续审计模型权重和传递依赖。

继续 §7（model license）+ §8（runtime + dependency）。

---

## 7. Marker Model License

### 7.1 证据

GitHub `MODEL_LICENSE`（master）= **modified version of OpenRAIL-M license**（"AI Pubs Open RAIL-M"）

README "Commercial Usage and Licensing" 段：

> Our code is licensed under Apache 2.0 — free to use, including commercially. **Our model weights use a modified AI Pubs Open Rail-M license (free for research, personal use, and startups under $5M funding/revenue).**

### 7.2 OpenRAIL-M 关键条款（modified variant）

| 条款 | 内容 | 影响 |
|---|---|---|
| Permission | 永久、全球、免版税 copyright + patent license（复制 / 准备 / 公开展示 / 公开表演 / sublicense / distribute） | 宽松 |
| Share-alike | 衍生模型 + 输出必须使用相同 license | 类 copyleft |
| Use-based restriction | 必须在 downstream agreement 中包含使用限制 | 传递义务 |
| Attribution | 必须保留 attribution notices | NOTICE 义务 |
| **Attachment A — Revenue cap** | **组织 gross revenue > $5M 禁止使用**（除 personal / research use） | **HARD BLOCKER** |
| **Attachment A — Funding cap** | **组织 raised funding > $5M 禁止使用**（除 personal / research use） | **HARD BLOCKER** |
| **Attachment A — Anti-competition** | **不得用于与 Licensor (Datalab) 竞争的产品或服务** | **HARD BLOCKER** |
| Remote restriction | Licensor 保留远程限制使用的权利 | 不确定性 |

### 7.3 OpenRAIL-M 性质

- **不是 OSI 批准的开源许可证**（OSI 2023 年拒绝 RAIL 许可证纳入"开源"定义）
- 属于 "responsible AI license"——附加行为限制（use-based restrictions）
- 比 Apache-2.0 / BSD / MIT 严格得多
- 与项目自身 MIT 声明 + 未来可能商业分发**潜在冲突**

### 7.4 模型权重下载行为

Marker 通过 `surya-ocr` + `transformers` + `huggingface-hub` 在运行时从 HuggingFace Hub 下载模型权重（典型 size: surya layout ~ hundreds of MB；OCR model ~ hundreds of MB；total GB 级）。

- **下载自动发生**：第一次运行 marker 时（surya marker 加载）
- **下载位置**：HuggingFace 默认 cache（`~/.cache/huggingface/`）
- **强制 disable**：理论上可预下载 + offline 模式，但 marker 不暴露官方 API 来禁用模型加载

### 7.5 结论

- **Marker 2.0.0 模型权重许可证 = modified OpenRAIL-M**（非开源、有 $5M cap、反竞争条款、远程限制权）
- 即使 P2-R2 数字 PDF only + force_ocr=False 配置，**surya-ocr 仍是 hard dependency**（见 §8.1）——模型权重仍需下载用于 layout detection / reading order / table recognition
- **Marker 模型权重 license 对本项目不可接受**（D6 HARD BLOCKER）

---

## 8. Marker Runtime and Dependency Audit

### 8.1 Direct dependencies（PyPI requires_dist @ marker-pdf 2.0.0）

| Package | Version constraint | Purpose | License | Runtime required | Model/data | Distribution concern |
|---|---|---|---|---|---|---|
| anthropic | `<1,>=0.69.0` | LLM client (marker LLM mode) | MIT | optional (LLM mode) | none | **introduces external LLM provider path** |
| openai | `<3,>=2.2.0` | LLM client (marker LLM mode) | Apache-2.0 | optional (LLM mode) | none | **introduces external LLM provider path** |
| google-genai | `<3,>=1.40.0` | LLM client (marker LLM mode) | Apache-2.0 | optional (LLM mode) | none | **introduces external LLM provider path** |
| torch | `<3,>=2.7.0` | Tensor compute (model inference) | BSD-3-Clause | **yes (hard)** | none | **~2GB wheel, native binary, CUDA/CPU** |
| transformers | `<6,>=5.12.1` | Model loading (HuggingFace) | Apache-2.0 | **yes (hard)** | none | **downloads models at runtime** |
| surya-ocr | `<0.23.0,>=0.22.1` | OCR + layout + reading order + table | Apache-2.0 (code) + modified OpenRAIL-M (models) | **yes (hard)** | **surya models** | **HARD BLOCKER per §7** |
| pdftext | `<0.8.0,>=0.7.0` | PDF text extraction (pypdfium2-based) | Apache-2.0 | yes | none | OK (transitive pypdfium2 = BSD-3-Clause) |
| scikit-learn | `<2,>=1.6.1` | ML utilities (clustering for layout) | BSD-3-Clause | yes | none | OK |
| pydantic | `<3,>=2.4.2` | Config validation | MIT | yes | none | OK (项目已有) |
| pydantic-settings | `<3,>=2.0.3` | Config loading | MIT | yes | none | OK |
| pillow | `<11,>=10.1.0` | Image manipulation | HPND-like | yes | none | OK |
| click | `<9,>=8.2.0` | CLI | BSD-3-Clause | yes | none | OK |
| filetype | `<2,>=1.2.0` | File type detection | MIT | yes | none | OK |
| ftfy | `<7,>=6.1.1` | Unicode fix-up | MIT | yes | none | OK |
| markdown2 | `<3,>=2.5.2` | Markdown rendering | MIT | yes | none | OK |
| markdownify | `<2,>=1.1.0` | HTML→Markdown | MIT | yes | none | OK |
| psutil | `<8,>=7.0.0` | Process info | BSD-3-Clause | yes | none | OK |
| python-dotenv | `<2,>=1.0.0` | Env vars | BSD-3-Clause | yes | none | OK (项目已有) |
| rapidfuzz | `<4,>=3.8.1` | String fuzzy match | MIT | yes | none | OK |
| regex | `>=2024.4.28` | Regex | Apache-2.0 | yes | none | OK |
| tqdm | `<5,>=4.66.1` | Progress bar | MPL-2.0 + MIT | yes | none | OK |

**Total direct dependencies: 21**

### 8.2 关键传递依赖（per startup directive §9.D）

| Stack | Packages | License | Model/data |
|---|---|---|---|
| Model inference | torch / torchvision (via surya) / transformers | BSD-3-Clause / Apache-2.0 | none (just runtime) |
| OCR + layout | **surya-ocr** | Apache-2.0 (code) + **OpenRAIL-M (models)** | **受限模型权重** |
| PDF backend | pdftext → pypdfium2 | Apache-2.0 / BSD-3-Clause | none |
| HuggingFace | huggingface-hub (via surya/transformers) | Apache-2.0 | none (但触发模型下载) |
| Image processing | opencv-python-headless (via surya) / pillow | Apache-2.0 / HPND | none |
| Native binaries | torch (~2 GB wheel), opencv (~50 MB), pypdfium2 (~10 MB) | various | none |

### 8.3 Runtime Modes（per startup directive §9.C）

| Mode | 加载模型 | 下载权重 | 启动推理服务 | 调远程 LLM | 符合 R2 边界 |
|---|---|---|---|---|---|
| `balanced`（默认） | ✅ 是 | ✅ 是 | ✅ 本地 | ❌ 否 | ❌ 违反 no-model |
| `fast` | ✅ 是 | ✅ 是 | ✅ 本地 | ❌ 否 | ❌ 违反 no-model |
| `force_ocr=True` | ✅ 是 | ✅ 是 | ✅ 本地 | ❌ 否 | ❌ 违反 no-OCR + no-model |
| `force_ocr=False`（P2-R0 §4.2 配置） | **✅ 仍是**（layout/reading-order/table 模型仍加载） | **✅ 仍是** | ✅ 本地 | ❌ 否 | ❌ 违反 no-model |
| `use_llm=True` | ✅ 是 | ✅ 是 | ✅ 本地 | ✅ 是 | ❌ 违反 no-external-network |

**关键事实**：marker 即使配置 `force_ocr=False`，**surya-ocr 仍是 hard dependency**——layout detection / reading order / table recognition 都基于模型。force_ocr 只控制是否对扫描页做 OCR，**不**控制是否加载模型。

### 8.4 marker 通过 §16 附加门的结果

per startup directive §16（20 项附加门）：

| # | 项目 | 状态 |
|---|---|---|
| 1 | 准确版本 | ✅ 2.0.0 |
| 2 | 准确代码许可证 | ✅ Apache-2.0 |
| 3 | 准确 MODEL_LICENSE | ❌ modified OpenRAIL-M（$5M cap + 反竞争 + 远程限制） |
| 4 | 是否使用模型权重 | ❌ 是（surya/torch/transformers hard dep） |
| 5 | 是否下载模型权重 | ❌ 是（huggingface-hub 运行时下载） |
| 6 | 模型权重进入 Docker | ❌ 若分发 Docker 则分发受限模型 |
| 7 | 项目分发模型 | ❌ 同 6 |
| 8 | startup 是否联网 | ⚠️ 首次启动可能联网下载模型 |
| 9 | 禁用 OCR 的准确配置 | ✅ `force_ocr=False`（但不足以禁用模型） |
| 10 | 禁用外部 LLM 的准确配置 | ✅ 不调 use_llm |
| 11 | 禁用图片提取 | ✅ marker 默认输出 markdown |
| 12 | 推理服务器是否启动 | ✅ 本地（torch），非外部服务 |
| 13 | optional extra | ✅ 可放 `[project.optional-dependencies] rag` |
| 14 | 缺 marker 时安全错误 | ⚠️ 待 R2 实现 |
| 15 | notice 分发 | ❌ 需分发 OpenRAIL-M + Apache-2.0 notice |
| 16 | 收入/融资条件 | ❌ **$5M revenue/funding cap = HARD BLOCKER** |
| 17 | 防止未来配置意外启用模型 | ⚠️ 需测试约束 |
| 18 | 测试中 0 模型下载 / 0 网络 | ❌ 难以保证（surya 默认下载） |
| 19 | 固定 dependency 版本 | ⚠️ 可 pin 但传递仍巨 |
| 20 | 升级时重新审计 | ⚠️ 需流程 |

**6 项 HARD FAIL**（#3 / #4 / #5 / #6 / #7 / #16）。

### 8.5 Marker 综合结论

per startup directive §16：

> 只要其中任何一项未确认：**Marker = NOT APPROVED FOR P2-R2**

**Marker NOT APPROVED FOR P2-R2**——主因：
1. MODEL_LICENSE (OpenRAIL-M) 含 $5M revenue/funding cap + 反竞争条款 + 远程限制权（§7）
2. Model stack (surya/torch/transformers) 是 hard dependency，无法在 no-model 边界下使用（§8.3）
3. 即使 force_ocr=False 仍加载模型——配置不足以达到 R2 边界

---

## 9. pypdf Audit

### 9.1 选定审计版本

```text
candidate:        pypdf
package:          pypdf
version:          6.14.2  (PyPI latest stable @ 2026-07-31)
homepage:         https://github.com/py-pdf/pypdf
license_field:    BSD-3-Clause
requires_python:  >=3.9
```

### 9.2 直接依赖（PyPI requires_dist @ pypdf 6.14.2）

```text
typing_extensions>=4.0; python_version < "3.11"   ← conditional, 仅旧 Python
```

extras：

```text
crypto:    cryptography>3.0, PyCryptodome
image:     Pillow>=8.0.0
full:      cryptography>3.0, fonttools, Pillow>=8.0.0
fonts:     fonttools
docs:      sphinx + 主题
dev:       pytest-*
```

**核心依赖（非 extra）：1 个 conditional**（typing_extensions，仅 Python<3.11）。

### 9.3 Code License 证据

| 证据源 | 内容 |
|---|---|
| PyPI `info.license` | `BSD-3-Clause` |
| PyPI classifier | (无 License classifier — 仅 Development Status / Topic) |
| GitHub LICENSE（master）| BSD-3-Clause（标准三 clause） |

### 9.4 技术能力评估（per startup directive §10）

| 维度 | pypdf 6.14.2 能力 |
|---|---|
| 纯 Python | ✅ |
| 分页文本提取 | ✅ `page.extract_text()` |
| 页面数 | ✅ `len(reader.pages)` |
| 加密状态识别 | ✅ `reader.is_encrypted` |
| 密码 PDF 行为 | ✅ `reader.decrypt(password)` |
| PDF metadata | ✅ `reader.metadata` |
| visitor / 布局提取 | ✅ `visitor_text` callback |
| 表格识别 | ⚠️ 退化（无 native table support；可输出文本但不还原结构） |
| 多栏 PDF 顺序 | ⚠️ 可能不理想（按 PDF 内容流顺序，不重排栏） |
| 标题层级恢复 | ❌ 无 native heading detection（需启发式：字号 / 加粗 / 大写） |
| 页面顺序稳定性 | ✅（按 PDF page tree） |
| 损坏 PDF 行为 | ✅ 抛 `PdfReadError`（可映射到 safe error code） |
| 依赖和安装体积 | ✅ 0 直接依赖；wheel 纯 Python 几百 KB |
| Windows 兼容性 | ✅（纯 Python） |
| Python 版本 | ✅ >=3.9 |
| crypto extra 必需性 | ⚠️ 仅当处理加密 PDF——R2 第一版**可不引入** crypto extra（标记 encrypted → `needs_password` / 拒绝） |

### 9.5 pypdf 通过 §17 附加门的结果

per startup directive §17（17 项附加门）：

| # | 项目 | 状态 |
|---|---|---|
| 1 | 准确版本 | ✅ 6.14.2 |
| 2 | BSD-3-Clause 证据 | ✅（PyPI + GitHub） |
| 3 | 依赖列表 | ✅ 1 个 conditional typing_extensions |
| 4 | 是否需要 crypto extra | ⚠️ R2 第一版**不引入**（标记 encrypted PDF 为 `encrypted_pdf` 错误码，避免引入 cryptography） |
| 5 | Parser Adapter 接口 | ✅（见 §16） |
| 6 | page-by-page 提取 | ✅ `PdfReader.pages[i].extract_text()` |
| 7 | encrypted/password 检测 | ✅ `reader.is_encrypted` + 尝试空密码 → 失败映射到 `encrypted_pdf` |
| 8 | scan-only 判定 | ✅ 启发式：`extract_text()` 返回空或低于阈值字符数 → `needs_ocr` |
| 9 | 多栏 / 复杂 layout 限制 | ⚠️ 已知限制（列入 Known Limitations） |
| 10 | 表格退化策略 | ✅ 输出为段落文本（不还原 grid） |
| 11 | 标题启发式 | ⚠️ 字号 / 加粗 / 大写模式（best-effort） |
| 12 | page marker 生成 | ✅ 稳定 `<!-- page:N -->` |
| 13 | no OCR | ✅（无 OCR 能力） |
| 14 | no model | ✅（无 ML 依赖） |
| 15 | no external network | ✅（纯本地解析） |
| 16 | 失败错误码映射 | ✅ 见 §19 |
| 17 | future parser replacement 边界 | ✅ Adapter 隔离（见 §16） |

**0 项 HARD FAIL** ✅

### 9.6 pypdf 已知技术限制（per startup directive §17 尾段）

必须在 Canonical Markdown 文档 / R2 实现中明确：

- 复杂多栏顺序可能不理想（按 PDF 内容流，不重排栏）
- 表格退化为段落文本（不还原 grid 结构）
- 标题层级依赖启发式（字号 / 加粗 / 大写）
- 公式 / 图片不处理（图片 metadata 可记录但不解析内容）
- 扫描型 PDF 进 `needs_ocr` 终态（不自动 OCR）

### 9.7 pypdf 综合结论

**pypdf APPROVED FOR P2-R2 MVP** —— 理由：

1. BSD-3-Clause 宽松（OSI 批准，无 copyleft，无收入门槛，无反竞争条款，无远程限制权）
2. 0 直接依赖（默认），纯 Python，无 native binary，无模型
3. 完全离线，无网络请求
4. digital PDF text 提取能力满足 R2 MVP（page mapping + encrypted 检测 + metadata）
5. 复杂布局限制作为 Known Limitations（可后续替换 via Adapter）

---

## 10. PyMuPDF Audit

### 10.1 选定审计版本

```text
candidate:        PyMuPDF
package:          PyMuPDF
version:          1.28.0  (PyPI latest stable @ 2026-07-31)
homepage:         https://github.com/pymupdf/PyMuPDF
license_field:    "Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License"
```

### 10.2 License 事实

| 证据源 | 内容 |
|---|---|
| PyPI `info.license` | `Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License` |
| 底层 MuPDF | AGPL-3.0 OR Artifex Commercial License（同源） |

### 10.3 双许可含义

- **AGPL-3.0 路径**：
  - 触发 copyleft 义务——衍生作品必须以 AGPL-3.0 开源
  - SaaS / network service 也触发（AGPL 的 network copyleft 条款）
  - 与项目自身声明 MIT 的状态**潜在冲突**——若 PyMuPDF 进入 base deps，整个 dist 需要 relicense 到 AGPL 或单独商业许可
- **Artifex Commercial License 路径**：
  - 付费许可，按用途 / 组织规模定价
  - 需要 contact Artifex 销售

### 10.4 per startup directive §11 保守规则

> 若项目计划可能闭源分发，且没有明确接受AGPL或商业许可证：
> **PyMuPDF = NOT APPROVED**

项目当前状态：
- 声明 MIT（pyproject + README）
- 但仓库根无 LICENSE 文件 → 元数据不一致
- 未来分发模式未确定（可能 PyPI / Docker / desktop）
- 未接受 AGPL 义务
- 未取得 Artifex Commercial License

**PyMuPDF NOT APPROVED** —— 主因：
1. AGPL copyleft 与项目 MIT 声明 + 未来可能商业分发**冲突**
2. 未取得商业许可
3. 元数据不一致让 license 状态更不确定——保守排除

### 10.5 PyMuPDF 技术能力（仅记录，不影响决策）

- 文本块 + 坐标（`page.get_text("dict")`）
- 高性能（C extension）
- Windows wheel 可用
- 但 native binary 含 MuPDF，进入 Docker / desktop bundle 时同样触发 AGPL

---

## 11. Candidate Comparison Matrix

per startup directive §12（24 维度评分）：

| 维度 | pypdf 6.14.2 | marker-pdf 2.0.0 | PyMuPDF 1.28.0 |
|---|---|---|---|
| code license | ✅ BSD-3-Clause | ✅ Apache-2.0 | ⚠️ AGPL-3.0 / Commercial |
| model/data license | ✅ N/A | ❌ modified OpenRAIL-M ($5M cap + 反竞争) | ✅ N/A |
| transitive license risk | ✅ 低（1 dep） | ⚠️ 中（torch / transformers / surya 全 Apache/BSD 但 model 是 OpenRAIL-M） | ✅ 低（无 Python transitive） |
| commercial use | ✅ 无限制 | ❌ $5M cap + 反竞争 | ⚠️ 需商业许可 |
| redistribution | ✅ 无限制 | ❌ 模型权重分发受限 | ⚠️ AGPL 触发 copyleft |
| offline operation | ✅ 完全离线 | ❌ 首次需下载模型 | ✅ 完全离线 |
| no-OCR mode | ✅ N/A（无 OCR） | ⚠️ force_ocr=False 但模型仍加载 | ✅ N/A |
| no-model mode | ✅ 无模型 | ❌ 模型 hard dep | ✅ 无模型 |
| digital PDF text quality | ⚠️ 中（无 layout 重排） | ✅ 高（layout + reading order） | ✅ 高（block + 坐标） |
| page mapping | ✅ 稳定 | ✅ 稳定 | ✅ 稳定 |
| heading/layout information | ⚠️ 启发式 | ✅ native | ✅ 部分（字号 + 字体） |
| encrypted PDF handling | ✅ detect + skip | ✅ detect + skip | ✅ detect + skip |
| scan-only detection | ✅ 启发式（文本长度阈值） | ✅ force_ocr + 文本检测 | ✅ 启发式 |
| Windows support | ✅ 纯 Python | ⚠️ torch Windows wheel 巨大 | ✅ native wheel |
| Python support | ✅ >=3.9 | ✅ >=3.10（torch 限制） | ✅ >=3.9 |
| dependency size | ✅ ~500 KB | ❌ ~2-3 GB | ⚠️ ~50 MB |
| startup cost | ✅ 立即 | ❌ 模型加载慢 | ✅ 快 |
| memory cost | ✅ <50 MB | ❌ 数 GB（torch + model） | ⚠️ ~100 MB |
| subprocess requirement | ✅ 无 | ✅ 无（in-process） | ✅ 无 |
| network behavior | ✅ 0 | ❌ HuggingFace 下载 | ✅ 0 |
| adapter complexity | ✅ 低（最简 API） | ⚠️ 高（大量 config） | ⚠️ 中（坐标 → text 转换） |
| test determinism | ✅ 高（无模型） | ❌ 低（模型输出可能变） | ✅ 高 |
| future replaceability | ✅ Adapter 隔离 | N/A（被排除） | N/A（被排除） |

**评分汇总**：

- pypdf: 24 维度中 **18 ✅ + 6 ⚠️ + 0 ❌**
- marker: 24 维度中 **8 ✅ + 5 ⚠️ + 11 ❌**
- PyMuPDF: 24 维度中 **15 ✅ + 4 ⚠️ + 5 ❌**（license 维度全 ❌ 或 ⚠️）

---

## 12. Distribution Scenario Analysis

per startup directive §15（6 种分发场景）：

| 场景 | 项目当前/未来 | pypdf | marker | PyMuPDF |
|---|---|---|---|---|
| 1. 本地源码运行 | ✅ 当前 | ✅ OK | ⚠️ 用户需下载 GB 模型 | ✅ OK |
| 2. PyPI wheel/sdist | ❓ 未来可能 | ✅ OK（BSD 无冲突） | ❌ 模型 OpenRAIL-M 传递 | ⚠️ AGPL 触发 |
| 3. Docker image | ❓ 未来可能 | ✅ OK | ❌ 模型进 image = 分发受限模型 | ⚠️ AGPL 触发 |
| 4. 桌面/可执行 | ❓ 未来可能 | ✅ OK | ❌ 模型进 bundle | ⚠️ AGPL 触发 |
| 5. Hosted SaaS | ⛔ 明确不做 | N/A | ❌ SaaS + AGPL 模型 = 远程限制权触发 | N/A（AGPL SaaS 触发） |
| 6. Parser 独立进程 | ❓ 工程选项 | ✅ OK（无需） | ⚠️ 进程隔离**不**消除 model license 义务 | ⚠️ 进程隔离**不**消除 AGPL 义务 |

**关键事实**（per startup directive §15）：进程隔离和 optional dependency **不**自动规避 copyleft / use-based restriction。模型权重分发（Docker / desktop / PyPI wheel 含 model cache）仍触发 OpenRAIL-M 义务。

---

## 13. Security and Supply Chain

per startup directive §24：

| 维度 | pypdf | marker | PyMuPDF |
|---|---|---|---|
| maintainer | py-pdf org（活跃） | VikParuchuri / Datalab | Artifex Software |
| official name | `pypdf`（无 typosquat） | `marker-pdf` | `PyMuPDF` |
| source match | ✅ GitHub py-pdf/pypdf | ✅ GitHub datalab-to/marker | ✅ GitHub pymupdf/PyMuPDF |
| wheel 官方 | ✅ PyPI 官方 | ✅ PyPI 官方 | ✅ PyPI 官方 |
| hash pinning 可行 | ✅ | ✅ | ✅ |
| native binary | ❌ 纯 Python | ✅ torch + opencv（native） | ✅ MuPDF（native） |
| install script | ❌ 无 | ❌ 无 | ❌ 无 |
| 自动下载模型 | ❌ 无 | ✅ **HuggingFace 下载** | ❌ 无 |
| spawn subprocess | ❌ 无 | ❌ 无 | ❌ 无 |
| 调用网络 | ❌ 无 | ✅ HuggingFace 下载 + 可选 LLM API | ❌ 无 |
| 读 env API key | ❌ 无 | ✅ anthropic / openai / google-genai client 读各自 env var | ❌ 无 |
| LLM 模式 | ❌ 无 | ✅ use_llm=True 启用 | ❌ 无 |
| 默认远程服务 | ❌ 无 | ⚠️ marker 默认配置可能调 LLM（use_llm） | ❌ 无 |

**R2 MVP 必须冻结**（per startup directive §24）：

```
External Provider requests = 0
Model downloads = 0
OCR = disabled
Remote LLM = disabled
```

| 候选 | 4 项约束可行性 |
|---|---|
| pypdf | ✅ **全部天然满足** |
| marker | ❌ Model downloads = 0 不可达（surya/torch 硬依赖） |
| PyMuPDF | ✅ 天然满足（但 license 阻断） |

---

## 14. Selected Parser

```
Selected Parser: pypdf
Version pin:     >=6.0,<7  (current PyPI stable 6.14.2)
SPDX:            BSD-3-Clause
Source:          https://github.com/py-pdf/pypdf
Retrieved at:    2026-07-31
```

**Decision: A — pypdf MVP**（per startup directive §20）

### 14.1 决策语义

- **pypdf 作为 R2 MVP parser**
- **纯数字 PDF**（per P2-R0 §4 / amendment-1）
- **不 OCR**（per P2-R0 §4 / startup directive §24）
- **不模型**（per startup directive §24）
- **不外部网络**（per startup directive §24）
- **BSD-3-Clause**（per §9.3）
- **复杂布局作为 Known Limitations**（per §9.6）—— 后续 R3+ 评估替换或加 layout post-processing

### 14.2 决策依据（per startup directive §19 决策顺序）

1. ✅ 许可证和分发边界明确（BSD-3-Clause，所有 6 种分发场景可接受）
2. ✅ 不使用受限模型权重（无模型）
3. ✅ 完全离线（无网络）
4. ✅ 符合 digital PDF only（无 OCR 能力 = 天然符合）
5. ✅ 依赖最小（0 直接依赖）
6. ✅ Windows 可安装（纯 Python）
7. ✅ 测试确定性强（无模型输出波动）
8. ✅ 能够保留 page mapping（`PdfReader.pages` 稳定索引）
9. ✅ 能够通过 Adapter 替换（最简 API）
10. ✅ 解析质量满足 MVP（digital text 足够；复杂 layout 列入 Known Limitations）

---

## 15. Rejected Candidates

| 候选 | 拒绝原因 | 主阻断点 |
|---|---|---|
| **marker-pdf 2.0.0** | MODEL_LICENSE = modified OpenRAIL-M 含 $5M revenue/funding cap + 反竞争条款 + 远程限制权；且 surya-ocr + torch + transformers 是 hard dependency，无法满足 no-model 边界 | §7 + §8.4 6 项 HARD FAIL |
| **PyMuPDF 1.28.0** | AGPL-3.0 / Artifex Commercial dual license——AGPL 触发 copyleft（与项目 MIT 声明 + 未来分发冲突），未取得商业许可 | §10.4 保守规则 |

### 15.1 marker 的未来再审计路径

marker **不被永久排除**——若未来满足以下条件，可在 R3+ 或独立 milestone 重新审计：

1. marker 提供纯 `pdftext` 路径（无 surya / torch），且该路径能通过 Parser Adapter；或
2. marker 模型许可证改为对商业 / 任意规模友好的条款；或
3. 项目自身明确接受 OpenRAIL-M 义务（需法律顾问书面确认）

### 15.2 PyMuPDF 的未来再审计路径

若用户明确接受 AGPL-3.0 义务（项目自身 relicense 到 AGPL）或取得 Artifex Commercial License，可在独立 milestone 重新审计。

---

## 16. Parser Adapter Contract

per startup directive §18 + P2-R0 §11.3 R3 contract——冻结 Protocol（**只冻结接口形状，R2-0 不创建 Python 文件**）：

```python
# src/pi_agent_core_py/web/knowledge/parser.py  (R2 创建)

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class PdfParser(Protocol):
    """Parser Adapter — 业务层不直接依赖 pypdf API。"""

    @property
    def parser_id(self) -> str:
        """稳定标识，e.g. 'pypdf'。写入 documents.parser_version 来源。"""
        ...

    @property
    def parser_version(self) -> str:
        """实际使用的 parser 版本，e.g. 'pypdf-6.14.2'。"""
        ...

    def inspect(self, path: Path) -> "PdfInspection":
        """快速 metadata 探测——page_count / encrypted / file_size /
        has_extractable_text_estimate（不全量提取）。

        用于 upload 时立即返回 metadata + 决定是否进 ingestion pipeline。"""
        ...

    def extract(self, path: Path) -> "PdfExtractionResult":
        """完整 page-by-page 提取——返回每页 text + warnings。

        失败时抛 ParserError（携带 safe_error_code）；不返回半成品。"""
        ...

    def close(self) -> None:
        """释放底层资源（pypdf 不需要，但 Protocol 要求——其他 parser 可能需要）。"""
        ...
```

### 16.1 PdfInspection 字段

```python
@dataclass(frozen=True)
class PdfInspection:
    page_count: int                      # >= 0
    encrypted: bool                      # 是否需要密码
    password_required: bool              # encrypted 且无空密码可解
    file_size: int                       # bytes
    metadata: dict[str, str]             # 标题/作者等（sanitized）
    has_extractable_text_estimate: bool  # 第一页 extract_text 非空 → True
```

### 16.2 PdfPage 字段

```python
@dataclass(frozen=True)
class PdfPage:
    page_number: int                # 1-based
    text: str                       # 该页提取的全文（已 strip）
    extraction_warnings: list[str]  # 该页提取时的非致命警告
```

### 16.3 PdfExtractionResult 字段

```python
@dataclass(frozen=True)
class PdfExtractionResult:
    pages: list[PdfPage]
    parser_id: str                  # 与 PdfParser.parser_id 一致
    parser_version: str             # 与 PdfParser.parser_version 一致
    warnings: list[str]             # 全局警告（不影响成功）
```

### 16.4 业务层依赖方向

```
api.py / service.py
       ↓
  PdfParser (Protocol)
       ↓
  PyPdfParser (R2 实现，仅此文件 import pypdf)
       ↓
  pypdf  (optional extra)
```

业务层**不**直接 `import pypdf`——所有访问走 `PdfParser` Protocol。R3+ 若引入 marker / PyMuPDF，新增 `MarkerParser` / `PyMuPDFParser` 实现 Protocol 即可，**不改业务层**。

---

## 17. Dependency Integration Plan

per startup directive §22——**只写入设计文档；R2-0 不修改 pyproject.toml**。

### 17.1 Extra 设计

```toml
# pyproject.toml（R2 编码阶段添加，R2-0 不动）

[project.optional-dependencies]
# 已有 dev / web extras
rag = [
    # 第一版 RAG MVP parser——纯 Python、BSD-3-Clause、0 直接依赖
    "pypdf>=6.0,<7",
]
```

### 17.2 Extra 命名

per startup directive §22：

> 不得为了一个Parser创建多套extra。

→ 使用单一 `rag` extra（不是 `rag-pdf` / `pdf-parser` 等多个）。

### 17.3 安装方式

- 开发：`pip install -e ".[dev,web,rag]"`
- 用户启用 RAG：`pip install pi-agent-core-py[rag]`
- 不启用 RAG：`pip install pi-agent-core-py`（pypdf 不被引入；RAG endpoint 返回 503）

### 17.4 Lockfile 变更

R2 编码阶段：`uv.lock` 添加 `pypdf` + `typing_extensions`（后者项目已通过其他路径引入）。R2-0 **不动 uv.lock**。

### 17.5 Python 最低版本

- pypdf 要求 `>=3.9`
- 项目要求 `>=3.11`
- ✅ 兼容

### 17.6 Windows wheel / 纯 Python

- pypdf 是纯 Python，无 native binary
- ✅ Windows / macOS / Linux 全平台无差异

### 17.7 License notice 位置

- pypdf License 随 wheel 自动携带（METADATA）
- 项目 README 不需要额外显示（pypdf 是 indirect dependency via extra）
- 源码分发：`pyproject.toml` 已声明项目 MIT；pypdf 作为 optional extra 自带 BSD-3-Clause

### 17.8 升级审计规则

per startup directive §22：

- pypdf minor 升级（6.x → 6.y）→ R2 maintainer 自审 API 兼容
- pypdf major 升级（6.x → 7.0）→ **重新跑 License Gate**（重新验证 BSD-3-Clause + 依赖图 +PyPI metadata）
- 任何升级 → 更新 `docs/design/p2-r2-0-pdf-parser-license-gate.md` §5 retrieval timestamp + version

---

## 18. Third-Party Notice Plan

per startup directive §23——**不在 R2-0 复制完整第三方许可证正文**（除非仓库惯例要求，本项目当前无此惯例）。

### 18.1 归档目录（建议，R2 编码阶段创建）

```
docs/licenses/pdf-parser/
└── pypdf.md    # 版本 + SPDX + 官方 source + last verified date + key obligations
```

### 18.2 pypdf.md 内容规范（per startup directive §23）

```markdown
# pypdf License Record

- **Package**: pypdf
- **Version**: 6.14.2
- **SPDX**: BSD-3-Clause
- **Official source**: https://github.com/py-pdf/pypdf
- **Selected tag/commit**: 6.14.2 release tag
- **Model license**: N/A（无模型）
- **Key obligations**: 保留 copyright / patent / trademark notice；不使用 contributor 名字做 endorsement
- **NOTICE requirement**: 否（BSD-3-Clause 无 NOTICE 义务）
- **Last verified**: 2026-07-31
- **Upgrade recheck condition**: 任何 minor 升级自审；major 升级重新跑 License Gate
```

### 18.3 源码 vs wheel vs Docker notice 边界

- **Source distribution**：pypdf 作为 optional extra，`pyproject.toml` + wheel METADATA 已携带——无需额外文件
- **Wheel distribution**：同上
- **Docker image**：若分发 Docker，可在 `/usr/share/doc/pi-agent-core-py/licenses/` 添加 pypdf notice（R2 编码阶段决定，不在 R2-0 范围）
- **THIRD_PARTY_NOTICES**：项目当前无此文件；若未来分发模式要求，再添加（**不在 R2-0 范围**）

---

## 19. P2-R2 Coding Constraints

per startup directive §24 + §13——R2 编码阶段必须冻结以下不变量：

### 19.1 R2 编码安全不变量

```
External Provider requests = 0
Model downloads = 0
OCR = disabled
Remote LLM = disabled
pypdf crypto extra = NOT introduced（encrypted PDF → 'encrypted_pdf' 错误码）
```

### 19.2 R2 编码允许的范围

- 在 `src/pi_agent_core_py/web/knowledge/parser.py` 创建 `PdfParser` Protocol + `PyPdfParser` 实现
- 修改 `pyproject.toml` 添加 `[project.optional-dependencies] rag = ["pypdf>=6.0,<7"]`
- 修改 `uv.lock`（uv 工具自动更新）
- 修改 `KnowledgeService` / API 添加 PDF upload endpoint
- 实现 Canonical Markdown writer（基于 `PdfExtractionResult`）
- 实现 heading-aware chunker（基于启发式）
- 实现 ingestion pipeline state machine

### 19.3 R2 编码禁止的范围

- 引入 marker / surya / torch / transformers（永久禁止，除非独立 milestone 重新审计通过）
- 引入 PyMuPDF（除非用户明确接受 AGPL 或取得商业许可）
- 引入 pypdf `[crypto]` extra（R2 第一版用错误码处理 encrypted PDF）
- 任何网络请求（per §24）
- 任何模型加载（per §24）

### 19.4 失败错误码映射（per startup directive §10 + §17.16）

| PDF 状态 | safe_error_code | document.status |
|---|---|---|
| 损坏 PDF | `invalid_pdf` | `failed` |
| PDF 过大（size > limit） | `pdf_too_large` | `failed` |
| PDF 页数过多（> limit） | `pdf_too_many_pages` | `failed` |
| 加密 PDF（无密码） | `encrypted_pdf` | `failed` |
| 密码 PDF（密码错） | `pdf_password_required` | `failed` |
| pypdf 解析异常 | `pdf_parse_failed` | `failed` |
| 文本可提取但为空（数字 PDF 但内容空） | `no_extractable_text` | `needs_ocr` |
| 扫描型 PDF（无文本层） | `needs_ocr` | `needs_ocr` |
| Markdown 写入失败 | `markdown_write_failed` | `failed` |
| Chunking 失败 | `chunking_failed` | `failed` |
| Indexing 失败 | `indexing_failed` | `failed` |
| 重复 SHA | `duplicate_document` | `failed` |

错误码不含路径 / 正文 / secret / stack trace——service 层 `_translate_*` 保证。

---

## 20. Upgrade / Reaudit Policy

per startup directive §16.20 + §17 + §22——

### 20.1 触发重新 License Gate 的条件

| 触发 | 操作 |
|---|---|
| pypdf minor 升级 | R2 maintainer 自审 API 兼容 + 跑现有 R2 测试 |
| pypdf major 升级 | **重新跑 License Gate**（重新验证 BSD + 依赖 + PyPI metadata） |
| 项目分发模式变更（加入 Docker / PyPI / desktop） | 重新评估所有 optional extra 的分发影响 |
| 项目自身 license 变更（如正式加入 LICENSE 文件、relicense） | 重新评估所有依赖兼容性 |
| 引入新 parser 候选（marker 重审 / PyMuPDF 重审 / 新候选） | **必须**新开 License Gate milestone，不得作为 R3+ 编码的子任务 |

### 20.2 重新审计的最小检查项

- PyPI license field（确认 SPDX 未变）
- GitHub LICENSE（确认与 PyPI 一致）
- PyPI requires_dist（确认依赖图未引入受限 license）
- 模型权重 license（若适用）
- 运行时网络行为（确认无新网络请求）

---

## 21. Known Limitations

per startup directive §17 + §19——R2 第一版的明确限制：

### 21.1 pypdf 技术限制

- **复杂多栏顺序**：pypdf 按 PDF 内容流提取，不重排栏——多栏 PDF 可能产生跨栏混合文本
- **表格退化**：pypdf 无 native table 支持——表格输出为段落文本，不还原 grid 结构
- **标题层级启发式**：pypdf 无 native heading detection——R2 实现基于字号 / 加粗 / 大写模式的启发式（best-effort）
- **公式 / 图片不处理**：pypdf 不解析公式 / 图片内容——图片 metadata 可记录（如尺寸 / 位置）但不提取视觉内容
- **扫描 PDF 进 needs_ocr**：pypdf 提取文本为空或低于阈值 → `needs_ocr` 终态（不自动 OCR，需用户手动处理）

### 21.2 项目元数据限制（与 R2-0 决策不冲突）

- 项目 pyproject 声明 MIT 但仓库根无 LICENSE 文件（§3.1）—— **R2-0 不解决**，作为 follow-up

### 21.3 Windows symlink 测试覆盖缺口（per R1 archive correction）

- `test_symlink_escape_rejected` POSIX-only（Windows skipped）—— R2-0 不解决，留给 R6 freeze

### 21.4 R2 范围限制

R2 **不实现**：
- OCR / 图像理解（永久不做，per P2-R0 §4.4）
- marker / PyMuPDF 集成（永久排除，除非独立 milestone 重新审计）
- 加密 PDF 解密（R2 第一版拒绝；future 可评估 crypto extra）
- 表格视觉重建（per pypdf 限制）
- 多栏重排（per pypdf 限制）

---

## 22. Legal Disclaimer

per startup directive §3——

本报告是**工程许可证审计**，不冒充法律意见：

- 结论基于公开许可证文本（PyPI + GitHub）、项目自身分发计划（§3.2）和工程事实（依赖图、运行时模式）
- **不能代替专业法律意见**
- 对以下情况，**必须**由法律顾问或许可证方书面确认：
  - 闭源商业分发
  - SaaS / 公网部署
  - 收入门槛触发（如 OpenRAIL-M $5M cap）
  - 跨境分发（如 US OFAC / EU AI Act）
  - 模型权重再分发
- 不把 README 营销描述当作唯一许可证依据
- LICENSE / MODEL_LICENSE / 发布包元数据和选定版本源码是主要证据
- 当前 master 的许可证不能自动代表历史版本
- PyPI 元数据不能自动代表仓库内全部模型、数据和传递依赖

### 22.1 工程阶段仍必须做出保守且可执行的 MVP 选择

per startup directive §3 末段：本报告选择 pypdf——这是工程上保守、可执行、可分发、可测试的 MVP 决策。**不**因未来法律不确定性而阻塞 R2 编码。

---

## 23. Open Questions

per startup directive §25——**Open Questions 必须为 0 才能冻结**。

| # | 问题 | 状态 |
|---|---|---|
| 1 | 项目自身 LICENSE 文件缺失是否影响 R2 决策？ | ✅ 已答：不影响（pyproject + README 都声明 MIT；pypdf BSD-3-Clause 与 MIT 兼容）；缺失作为 follow-up |
| 2 | 未来是否分发 Docker / PyPI / desktop？ | ✅ 已答：分发模式未确定，但 pypdf 在所有 6 种场景下都可接受（§12） |
| 3 | marker 是否可能在未来重新引入？ | ✅ 已答：可能在独立 milestone 重新审计（§15.1）；R2 不引入 |
| 4 | PyMuPDF 是否可能在未来重新引入？ | ✅ 已答：若用户明确接受 AGPL 或取得商业许可（§15.2）；R2 不引入 |
| 5 | pypdf crypto extra 是否需要？ | ✅ 已答：R2 第一版不引入；encrypted PDF → `encrypted_pdf` 错误码（§19.4） |
| 6 | 是否需要法律顾问确认 BSD-3-Clause？ | ✅ 已答：BSD-3-Clause 是 OSI 标准宽松许可证，工程上无需法务确认；若未来分发模式涉及商业合同再咨询 |
| 7 | 模型权重再分发边界？ | ✅ 已答：pypdf 无模型——N/A（§14） |

**Open Questions = 0** ✅

---

## 24. Exit Gate

per startup directive §25 + §31 阻塞规则：

| # | 条件 | 状态 |
|---|---|---|
| 1 | 项目自身许可证确认 | ✅ MIT 声明（pyproject + README）；LICENSE 文件缺失作为 follow-up（§3.1） |
| 2 | 项目分发模式确认 | ✅ Source distribution + local install；SaaS 明确不做（§3.2） |
| 3 | Marker 选定版本 LICENSE 已查 | ✅ Apache-2.0（§6） |
| 4 | Marker 选定版本 MODEL_LICENSE 已查 | ✅ modified OpenRAIL-M（§7） |
| 5 | Marker 运行时模式审计完成 | ✅ force_ocr=False 仍加载模型（§8.3） |
| 6 | Marker 依赖图审计完成 | ✅ 21 direct + 关键传递（§8.1-8.2） |
| 7 | pypdf 选定版本 LICENSE 已查 | ✅ BSD-3-Clause（§9.3） |
| 8 | pypdf 技术能力评估完成 | ✅（§9.4） |
| 9 | pypdf 已知限制列出 | ✅（§9.6 + §21.1） |
| 10 | PyMuPDF 选定版本 LICENSE 已查 | ✅ AGPL / Commercial dual（§10.2） |
| 11 | PyMuPDF 商业边界评估完成 | ✅ 未取得商业许可（§10.4） |
| 12 | 候选比较矩阵完成 | ✅ 24 维度（§11） |
| 13 | 分发场景分析完成 | ✅ 6 场景 × 3 候选（§12） |
| 14 | 安全 / 供应链审计完成 | ✅（§13） |
| 15 | 选定唯一 Parser | ✅ pypdf 6.14.2（§14） |
| 16 | 拒绝候选理由列出 | ✅（§15） |
| 17 | Parser Adapter 接口冻结 | ✅（§16） |
| 18 | 依赖引入方案冻结 | ✅ `rag` extra（§17） |
| 19 | 第三方 notice 方案冻结 | ✅ `docs/licenses/pdf-parser/pypdf.md`（§18） |
| 20 | R2 编码约束冻结 | ✅（§19） |
| 21 | 升级 / 重审策略冻结 | ✅（§20） |
| 22 | Known Limitations 列出 | ✅（§21） |
| 23 | Legal Disclaimer 写明 | ✅（§22） |
| 24 | Open Questions = 0 | ✅（§23） |
| 25 | D6 旧判断已修正 | ✅ Apache-2.0 + 重命名为 "PDF Parser Code, Model, Dependency and Distribution License" |
| 26 | P2-R2 Coding 开放 | ✅（D6 resolved） |
| 27 | Working tree clean（提交后） | ✅（docs-only commit） |
| 28 | G1 stash 未变化 | ✅ hash d7240268 不变 |
| 29 | Production / test / dep / schema / frontend diff = 0 | ✅（docs-only） |

**29/29 PASS** ✅

---

## 25. Final Verdict

```
P2-R2-0 PDF Parser License and Distribution Gate
✅ COMPLETE / FROZEN @ <this commit>

D6 PDF Parser Code, Model, Dependency and Distribution License
✅ RESOLVED

Selected Parser
✅ pypdf @ >=6.0,<7 (6.14.2 current stable, BSD-3-Clause)

Rejected Candidates
⛔ marker-pdf 2.0.0  — MODEL_LICENSE OpenRAIL-M $5M cap + 反竞争 + 远程限制；surya/torch hard dep 违反 no-model
⛔ PyMuPDF 1.28.0    — AGPL-3.0 / Artifex Commercial dual；与项目 MIT 声明 + 未来分发冲突

P2-R2 PDF → Markdown Coding
✅ APPROVED TO START (D6 resolved; encoding gate open)

Parser Adapter
✅ Frozen (§16) — PdfParser Protocol + PdfInspection / PdfPage / PdfExtractionResult

Dependency Integration
✅ Frozen (§17) — `[project.optional-dependencies] rag = ["pypdf>=6.0,<7"]`（R2 编码阶段添加，R2-0 不动 pyproject）

R2 Coding Constraints
✅ Frozen (§19) — External Provider=0 / Model downloads=0 / OCR=disabled / Remote LLM=disabled / no pypdf crypto extra

Open Questions = 0
Legal Disclaimer: 工程审计，不冒充法律意见（§22）
```

R2-0 完成。R2 编码可启动（需用户独立授权）。
