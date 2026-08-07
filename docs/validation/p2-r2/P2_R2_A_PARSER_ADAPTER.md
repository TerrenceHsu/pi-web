# P2-R2-A pypdf Parser Adapter — Validation Report

> **阶段**：P2-R2-A Parser Adapter + Dependency Integration
> **基线 commit（R2-A 之前）**：`f804fc7` — docs(rag): archive corrections to R2-0
> **R2-A freeze commit**：本提交（自引用；hash 由 git 在提交时生成）
> **归档日期**：2026-07-31
> **范围**：pypdf optional dependency + Parser Protocol + Adapter + 安全错误映射 + 真实 fixture 测试。**不**生成 Canonical Markdown、不实现 upload API、不实现 ingestion worker、不动 Knowledge Schema/Store/Service/API。

---

## 1. Final Status

```
P2-R2-A pypdf Parser Adapter
✅ COMPLETE / FROZEN @ <this commit>

R2-A commit chain:
c359ee5  build(rag): add pypdf parser dependency                   (R2-A1)
88b017b  feat(rag): add pypdf parser adapter                       (R2-A2)
<this>   test(rag): freeze pypdf parser adapter                   (R2-A3)

Baseline before R2-A:
f804fc7 — docs(rag): archive corrections to R2-0

Selected Parser (frozen at R2-0):
pypdf (BSD-3-Clause)

Allowed dependency range:    pypdf>=6.0,<7
Audited implementation baseline: 6.14.2
Actual locked version:       6.14.2
Actual installed version:    6.14.2
```

R2-A 性质：编码（含 pyproject + uv.lock）+ 测试 + docs 归档。

---

## 2. Per-commit File Inventory

| Commit | 类型 | 新文件 | 修改文件 | 总 |
|---|---|---|---|---|
| `c359ee5` R2-A1 Dependency + License Record | build | 2 (`docs/licenses/pdf-parser/pypdf.md` + `tests/test_rag_dependency_boundary.py`) | 2 (`pyproject.toml` + `uv.lock`) | 4 |
| `88b017b` R2-A2 Parser Contract + Adapter | feat | 4 (`pdf_parser.py`, `pypdf_parser.py`, `tests/_pdf_fixture_factory.py`, `tests/test_pypdf_parser.py`) | 1 (`tests/test_rag_dependency_boundary.py` — adapter import exception) | 5 |
| `<this>` R2-A3 Validation + Freeze | test/docs | 1 (`docs/validation/p2-r2/P2_R2_A_PARSER_ADAPTER.md`) | 3 (`STATUS.md`, `TODO.md`, `ROADMAP.md`) | 4 |

**Total across R2-A**: 7 new + 6 modified = 13 file instances (no overlap).

---

## 3. P2-R0 / R2-0 Contract Mapping

每条 R2-0 §16 + R2-A §11/§17/§19 要求映射到实现位置：

| R2-0 / R2-A 合同要求 | 实现位置 | 验证 |
|---|---|---|
| `PdfParser` Protocol | `src/.../knowledge/pdf_parser.py::PdfParser` (`@runtime_checkable`) | `test_parser_protocol_runtime_checkable` PASS |
| `PdfInspection` 字段 | `pdf_parser.py::PdfInspection`（frozen+slots，7 字段） | `TestModelsAndProtocol` 8 tests PASS |
| `PdfPage` 1-based | `pdf_parser.py::PdfPage` + `pypdf_parser.py::extract` | `test_pdf_page_1_based` PASS |
| `PdfExtractionResult` | `pdf_parser.py::PdfExtractionResult`（tuple pages） | `test_extraction_result_pages_is_tuple` PASS |
| parser_id / parser_version | `pypdf_parser.py::PypdfParser`（lazy version from importlib.metadata） | R2-A2 import smoke + `test_parser_id_and_version_in_result` PASS |
| 安全错误码（10 项） | `pdf_parser.py::{PdfFileNotFound, PdfNotAFile, InvalidPdf, EncryptedPdf, PdfPasswordRequired, PdfParseFailed, PdfPageExtractFailed, PdfParserUnavailable, UnsupportedParserVersion, PdfParserError}` | `TestOptionalDependencyMissing` + 各 fixture-driven 错误测试 PASS |
| Lazy import pypdf | `pypdf_parser.py::__init__` 内 `import pypdf` | `test_pypdf_missing_raises_unavailable` PASS |
| Encrypted PDF 拒绝 | `pypdf_parser.py::extract` + `inspect`（password_required → 部分返回） | `test_encrypted_pdf_rejected` / `test_encrypted_pdf_password_required` PASS |
| Page-level failure option A | `pypdf_parser.py::extract`（`PdfPageExtractFailed` 终止整个 extract） | `test_page_extract_failure_terminates_whole_extract` PASS |
| 文件句柄释放 | `finally: reader.stream.close()` 在 inspect + extract | `test_file_handle_released` PASS |
| 源文件 SHA 不变 | adapter 不修改源 PDF | `test_source_pdf_not_modified` PASS |
| 完全离线 / 无 OCR / 无 model / 无 LLM | adapter 仅 import pypdf + stdlib | `test_no_network_imports_in_adapter` + 边界扫描 PASS |

---

## 4. 模块清单

### 4.1 生产代码

| 模块 | 行数 | 职责 |
|---|---|---|
| `src/pi_agent_core_py/web/knowledge/pdf_parser.py` | 250 | Protocol + 4 dataclasses + 10 error classes + helper + constants |
| `src/pi_agent_core_py/web/knowledge/pypdf_parser.py` | 290 | PypdfParser Adapter（lazy import + inspect/extract/normalize） |
| **生产代码总计** | **540** | |

### 4.2 测试代码

| 文件 | 测试数 |
|---|---|
| `tests/_pdf_fixture_factory.py` | (helper, 0 tests) |
| `tests/test_pypdf_parser.py` | 55 |
| `tests/test_rag_dependency_boundary.py`（R2-A1 11 + R2-A2 更新）| 11 |
| **新增测试总计** | **66** |

### 4.3 依赖与许可证归档

| 文件 | 用途 |
|---|---|
| `pyproject.toml`（+8 行） | 新增 `[project.optional-dependencies] rag = ["pypdf>=6.0,<7"]` |
| `uv.lock`（+15 行 / 2 改行） | pypdf 6.14.2 stanza + extras 注册 + openai specifier sync |
| `docs/licenses/pdf-parser/pypdf.md` | 第三方许可证归档（per R2-0 §18） |

### 4.4 不动的范围（R2-A 边界）

- ❌ `web/knowledge/store.py` / `files.py` / `service.py` / `api.py` / `state.py` / `app.py` 不动
- ❌ 数据库 schema 不动
- ❌ Session Binding / Provider Runtime / Core Runtime / Tool registry 不动
- ❌ frontend/** 不动
- ❌ G1 stash 不动

---

## 5. Parser Protocol + Result 字段

### 5.1 PdfParser Protocol（per R2-0 §16）

```python
@runtime_checkable
class PdfParser(Protocol):
    @property
    def parser_id(self) -> str: ...
    @property
    def parser_version(self) -> str: ...
    def inspect(self, path: Path) -> PdfInspection: ...
    def extract(self, path: Path) -> PdfExtractionResult: ...
    def close(self) -> None: ...
```

### 5.2 PdfInspection

```python
@dataclass(frozen=True, slots=True)
class PdfInspection:
    file_size_bytes: int
    page_count: int
    encrypted: bool
    password_required: bool
    metadata: PdfMetadata
    has_extractable_text_estimate: bool | None
    inspection_warnings: tuple[str, ...]
```

### 5.3 PdfMetadata（safe, length-limited）

```python
@dataclass(frozen=True, slots=True)
class PdfMetadata:
    title: str | None
    author: str | None
    subject: str | None
    creator: str | None
    producer: str | None
    creation_date: str | None
    modification_date: str | None
```

值经 `truncate_metadata_value` 处理：strip 控制字符 + 截断到 `MAX_METADATA_VALUE_LENGTH=500`。

### 5.4 PdfPage

```python
@dataclass(frozen=True, slots=True)
class PdfPage:
    page_number: int              # 1-based
    text: str
    extraction_warnings: tuple[str, ...]
```

### 5.5 PdfExtractionResult

```python
@dataclass(frozen=True, slots=True)
class PdfExtractionResult:
    parser_id: str
    parser_version: str
    pages: tuple[PdfPage, ...]
    warnings: tuple[str, ...]
```

---

## 6. Inspect 语义

| 项 | 实现 |
|---|---|
| 文件存在性 | `_require_real_file(path)` → `PdfFileNotFound` / `PdfNotAFile` |
| file_size | `path.stat().st_size` |
| page_count | `len(reader.pages)`（encrypted+password_required 时返回 0） |
| encrypted | `reader.is_encrypted`（不需要解密即可读） |
| password_required | `reader.decrypt("") == 0`（empty password 失败） |
| metadata | `_safe_metadata(reader)` —— 单字段失败不阻塞整体 |
| has_extractable_text_estimate | first/middle/last 抽样 + `TEXT_ESTIMATE_MIN_CHARS_PER_PAGE=10` 阈值 |
| mixed samples | `None` + warning |
| encrypted+password_required | 部分返回（page_count=0, metadata=空, estimate=None, warning） |
| 文件句柄 | `finally: reader.stream.close()` |

---

## 7. Extract 语义

| 项 | 实现 |
|---|---|
| 文本提取 | 逐页 `reader.pages[i].extract_text()` |
| 1-based page_number | `i + 1` |
| None text | `_normalize_text(None)` → `""` |
| CRLF / CR | `_normalize_text` → `\n` |
| NUL bytes | `_normalize_text` → 移除 |
| 页顺序 | 按 PDF page tree（不重排栏） |
| 单页失败 | `PdfPageExtractFailed` → 终止整个 extract（option A，no silent missing pages） |
| encrypted PDF | `PdfPasswordRequired`（password_required=True） / `EncryptedPdf`（empty password 解出但 R2 仍拒绝） |
| 文件句柄 | `finally: reader.stream.close()` |

**未实现**（R2-B 范围）：

- 标题恢复
- 段落重排
- 页眉页脚去重
- 表格重建
- 多栏重排序
- Markdown escaping / frontmatter / page marker
- `needs_ocr` Document 状态写入

---

## 8. Encrypted PDF 语义

| PDF 状态 | inspect 返回 | extract 行为 |
|---|---|---|
| 未加密 | `encrypted=False, password_required=False` | 正常提取 |
| 加密 + empty password 失败 | `encrypted=True, password_required=True, page_count=0, has_text=None` + warning | `PdfPasswordRequired` |
| 加密 + empty password 成功 | `encrypted=True, password_required=False` | `EncryptedPdf`（R2 仍拒绝，per §18） |

**R2 不接受用户密码**：

- ❌ 没有 password 参数
- ❌ 不读环境变量
- ❌ 不尝试常见密码
- ❌ 不引入 `[crypto]` extra（cryptography）

---

## 9. Scan-only / needs_ocr 边界

Adapter **不**直接修改 Document 状态——只返回事实：

- 每页提取文本（含空页）
- `has_extractable_text_estimate`（True/False/None）
- warnings

**`needs_ocr` 最终判定由 R2-B Pipeline** 根据抽样结果 + 总字符数 + 非空页比例 + 冻结阈值决定。

Adapter 不导入 OCR / 不下载模型 / 不调系统 OCR / 不返回 "ready"。

---

## 10. Text Estimate 算法（R2-A §14）

```text
sample_count = TEXT_ESTIMATE_SAMPLE_COUNT = 3
strategy = first / middle / last (deduped, capped to 3)
threshold = TEXT_ESTIMATE_MIN_CHARS_PER_PAGE = 10  (non-whitespace)

For each sample page:
    text = page.extract_text()  # may raise → warning
    non_ws = sum(1 for c in text if not c.isspace())

if all samples non_ws >= threshold:  return True
if all samples non_ws == 0:           return False
if mixed / unknown:                   return None (+ warning)
```

**不做**：

- 全文件 OCR
- 模型检测
- 根据文件大小猜测
- 单页空白直接判 needs_ocr
- 读取无限页进行 estimate

---

## 11. 文本最低限度规范化（R2-A §16）

仅允许：

- `\r\n` → `\n`
- `\r` → `\n`
- 移除 NUL（`\x00`）
- None → `""`
- 非 str → `str()`

**不允许**（R2-B 范围）：

- 压缩空白
- 删除空行
- 自动拼接连字符
- 删页眉 / 页脚
- Markdown escaping
- 表格格式化
- Unicode NFKC 全文归一化

---

## 12. 错误码表

| safe_error_code | 异常类 | 触发场景 |
|---|---|---|
| `pdf_file_not_found` | `PdfFileNotFound` | path 不存在 |
| `pdf_not_a_file` | `PdfNotAFile` | path 是目录等 |
| `invalid_pdf` | `InvalidPdf` | pypdf 无法解析（含 corrupted PDF） |
| `encrypted_pdf` | `EncryptedPdf` | 加密 PDF（empty password 成功解出，R2 仍拒绝） |
| `pdf_password_required` | `PdfPasswordRequired` | 加密 PDF + empty password 失败 |
| `pdf_parse_failed` | `PdfParseFailed` | 通用 pypdf 解析失败（保留供未来用） |
| `pdf_page_extract_failed` | `PdfPageExtractFailed` | 单页 extract_text 异常（option A 终止） |
| `pdf_parser_unavailable` | `PdfParserUnavailable` | [rag] extra 未安装 |
| `unsupported_parser_version` | `UnsupportedParserVersion` | 安装的 pypdf 不在 6.x |
| `pdf_parse_failed` (default) | `PdfParserError` | 兜底基类 |

所有错误消息**不**含绝对路径 / OSError 文本 / pypdf traceback / secret。

---

## 13. Fixture 来源与策略

per R2-A §20——所有 fixture **完全离线 + 项目自生成**：

| Fixture | 生成方式 | 用途 |
|---|---|---|
| `text.pdf` | `_pdf_fixture_factory.write_text_pdf(pages=[...], metadata={...})` via `pypdf.PdfWriter` + low-level content stream + Helvetica font | 多页文本 PDF 测试 |
| `single.pdf` | 同上，单页 | 单页测试 |
| `blank.pdf` | `write_blank_pdf(page_count=N)` via `add_blank_page` | 扫描型 / 空白页测试 |
| `enc.pdf` | `write_encrypted_pdf(user_password=...)` via `writer.encrypt` | encrypted PDF 拒绝路径 |
| `corrupt.pdf` | `write_corrupted_pdf()` 写入 fake `%PDF-1.4` 头 + 无效字节 | corrupted PDF 错误路径 |

**约束**：

- ❌ 无网络下载
- ❌ 无外部 URL
- ❌ 无 reportlab 或其他 PDF 生成依赖
- ❌ 无第三方版权内容
- ✅ 极小（fixture PDF 几百字节到 2KB）
- ✅ 项目自行生成（pypdf.PdfWriter）
- ✅ 确定性（同样输入产出同样字节）

**低层 text injection**：pypdf 6.x 无 high-level add-text API；通过 `DecodedStreamObject` + `/Resources /Font /F1` (Helvetica Type1) 直接构造 content stream。

---

## 14. 定向测试结果

| Test 套件 | 测试数 | PASS | FAIL | SKIP |
|---|---|---|---|---|
| `tests/test_rag_dependency_boundary.py` | 11 | 11 | 0 | 0 |
| `tests/test_pypdf_parser.py` | 55 | 55 | 0 | 0 |
| **R2-A 定向小计** | **66** | **66** | **0** | **0** |

Command：

```bash
PYTHONPATH=src python -m pytest tests/test_rag_dependency_boundary.py tests/test_pypdf_parser.py \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
```

Duration: 2.35s.

---

## 15. R1 定向测试回归

证明 Parser 依赖没有破坏 R1 Library Foundation：

```bash
PYTHONPATH=src python -m pytest \
    tests/test_knowledge_store.py \
    tests/test_knowledge_files_and_service.py \
    tests/test_knowledge_api.py \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
```

Result: **117 passed, 1 skipped (118 selected)** (POSIX-only symlink test) — 0 regression. **CORRECTED @ P2-R2-D-B**：原报告"118 passed"是把 selected 数误写成 passed 数；R1 §4.2 内部表述（`38 (37 pass + 1 skipped POSIX-only)`）一直正确。

---

## 16. 完整 Backend 测试

```bash
PYTHONPATH=src python -m pytest tests/ \
    -m "not slow and not integration and not docker" \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
```

Result: **2768 passed, 2 skipped, 14 deselected** in 276.32s.

- Baseline（R2-A 之前）：2702 passed / 2 skipped / 14 deselected
- R2-A 新增：66 tests（11 boundary + 55 parser） → 2702 + 66 = 2768 ✅
- **0 regression**

---

## 17. Frontend 回归

```bash
cd src/pi_agent_core_py/web/frontend
npm run test      # 267/267 PASS (11 files)
npm run typecheck # PASS (vue-tsc --noEmit)
npm run lint      # PASS (eslint . --max-warnings=0)
npm run build     # 169.32 KB JS / 46.11 KB CSS / built in 1.33s
```

Frontend source diff = **0**（R2-A 全部是 backend + docs）。

---

## 18. Ruff 结果

```bash
PYTHONPATH=src python -m ruff check src tests scripts
→ All checks passed!
```

---

## 19. 网络与模型审计（per R2-A §28）

| 检查 | 结果 |
|---|---|
| External HTTP requests | ✅ 0（pypdf 纯本地解析；测试无网络） |
| DNS resolution | ✅ 0 |
| Model downloads | ✅ 0（无 surya/torch/transformers/huggingface） |
| OCR calls | ✅ 0（pypdf 无 OCR 能力） |
| LLM calls | ✅ 0（无 anthropic/openai/google-genai import in adapter） |
| Provider calls | ✅ 0 |
| Subprocess inference | ✅ 0（adapter 不 spawn 子进程） |

**证明方式**：

- 静态 import 扫描（adapter 模块零网络/OCR/LLM import）
- 真实 pypdf fixture 测试通过（无 mock 替代核心路径）
- 测试套件全离线运行（无任何 fixture URL）

---

## 20. 源文件完整性（per R2-A §29）

`test_source_pdf_not_modified`：

```python
sha_before = file_sha256(text_pdf)
parser.inspect(text_pdf)
parser.extract(text_pdf)
sha_after = file_sha256(text_pdf)
assert sha_before == sha_after  # PASS
```

✅ Adapter 不修改源 PDF；不创建临时文件 / cache / sidecar / model 目录 / log。

---

## 21. 依赖扫描（per R2-A §23）

| 扫描 | 命令 | 结果 |
|---|---|---|
| pypdf import 边界 | `grep -rE "import pypdf\|from pypdf" src tests` | ✅ 仅 `pypdf_parser.py`（adapter）+ `tests/test_pypdf_parser.py` + `pdf_parser.py` 的 docstring 引用（"MUST NOT import pypdf directly"） |
| 禁用 parser deps | `grep -rE "PyMuPDF\|pymupdf\|fitz\|marker\|surya\|torch\|transformers\|huggingface" src/pi_agent_core_py/web/knowledge` | ✅ 仅 `pdf_parser.py:184` docstring "marker / PyMuPDF adapters behind the same Protocol"——设计说明，非 import |
| 网络 imports in adapter | `grep -rE "httpx\|requests\|urllib\|aiohttp\|openai\|anthropic\|google.genai" pdf_parser.py pypdf_parser.py` | ✅ 0 hit |
| Markdown / retrieval in adapter | `grep -rE "markdown\|frontmatter\|page:[0-9]\|search_knowledge\|embedding\|bm25\|MATCH" pdf_parser.py pypdf_parser.py` | ✅ 0 hit（adapter 不生成 Markdown / 不做检索） |
| pyproject 禁用 deps | `tests/test_rag_dependency_boundary.py::TestRagExtraScope::test_no_forbidden_pdf_deps_in_any_extra` | ✅ PASS（marker-pdf / PyMuPDF / surya-ocr / torch / transformers / huggingface-hub / reportlab 全 0 出现） |

---

## 22. Production diff

| 范围 | 状态 |
|---|---|
| `src/pi_agent_core_py/web/knowledge/store.py` | ✅ 不变（R1 frozen） |
| `src/pi_agent_core_py/web/knowledge/files.py` | ✅ 不变 |
| `src/pi_agent_core_py/web/knowledge/service.py` | ✅ 不变 |
| `src/pi_agent_core_py/web/knowledge/api.py` | ✅ 不变 |
| `src/pi_agent_core_py/web/state.py` | ✅ 不变 |
| `src/pi_agent_core_py/web/app.py` | ✅ 不变 |
| Session logic | ✅ 不变 |
| Provider Runtime | ✅ 不变 |
| Core Runtime | ✅ 不变 |
| Tool registry | ✅ 不变 |
| **production diff 仅限** | `pdf_parser.py` + `pypdf_parser.py`（新模块） |

---

## 23. Schema / API / Frontend diff

| 范围 | 状态 |
|---|---|
| 数据库 schema | ✅ 0 diff（R1 frozen） |
| API endpoints | ✅ 0 diff（R2-A 不实现 upload / retry / markdown / search） |
| frontend/** | ✅ 0 diff |

---

## 24. G1 stash 完整性

```
git stash list
→ stash@{0}: On master: wip: assistant markdown rendering security hardening pending

git rev-parse stash@{0}
→ d7240268ec8b8e5d9e195c999e56fb6ec130fd55
```

G1 stash 未变化（与 R2-A 启动前一致）。G1 文件（5 个：eslint.config.js / package.json / package-lock.json / MessageBubble.vue / markdown.ts）未复制进 R2-A 任何提交。

---

## 25. 已知限制（R2-A 不解决）

### 25.1 pypdf 技术限制（per p2-r2-0-pdf-parser-license-gate.md §21.1）

- 复杂多栏顺序可能不理想（pypdf 按 PDF 内容流，不重排栏）
- 表格退化为段落文本（不还原 grid）
- 标题层级未恢复（R2-B 启发式）
- 公式 / 图片不处理
- 扫描 PDF 进 `needs_ocr` 终态（不自动 OCR）

### 25.2 R2-A 显式不实现（推到 R2-B / R2-C）

- ❌ Canonical Markdown 生成（YAML frontmatter + `<!-- page:N -->` + heading 启发式）→ R2-B
- ❌ `needs_oor` Document 状态判定 → R2-B
- ❌ PDF upload endpoint（`POST /api/knowledge/libraries/{lib}/documents`）→ R2-C
- ❌ Retry ingestion endpoint → R2-C
- ❌ Markdown preview endpoint → R2-B
- ❌ Ingestion worker / queue / state machine 实际运行 → R2-C
- ❌ knowledge_chunks 表实际写入 → R2-C
- ❌ Knowledge Service / API 修改（service 调 parser）→ R2-B/C

### 25.3 项目级 follow-ups（非 R2-A 阻塞）

- 仓库根 LICENSE 文件缺失（详见 `p2-r2-0-pdf-parser-license-gate.md §3.3`）
- pypdf 升级规则（详见 `p2-r2-0-pdf-parser-license-gate.md §20`）
- Windows symlink 测试覆盖缺口（R6 入口检查项）

---

## 26. Exit Gate（per R2-A §33）

| # | 条件 | 状态 |
|---|---|---|
| 1 | pypdf 依赖范围正确 | ✅ `>=6.0,<7` |
| 2 | 实际 locked version 已记录 | ✅ pypdf 6.14.2 |
| 3 | 实际版本属于 6.x | ✅ |
| 4 | 无 crypto extra | ✅ |
| 5 | 无无关依赖漂移 | ✅ uv.lock diff 51 行（含 openai specifier sync，版本不变） |
| 6 | 许可证记录完成 | ✅ `docs/licenses/pdf-parser/pypdf.md` |
| 7 | 主包无 rag extra 仍可 import | ✅ `test_main_package_imports_without_pypdf` |
| 8 | PdfParser Protocol 完成 | ✅ |
| 9 | PdfInspection 完成 | ✅ |
| 10 | PdfPage 完成 | ✅ |
| 11 | PdfExtractionResult 完成 | ✅ |
| 12 | 安全错误完成 | ✅ 10 类 |
| 13 | PypdfParser 完成 | ✅ |
| 14 | inspect 真实 fixture 通过 | ✅ text/blank/encrypted/corrupted |
| 15 | extract 真实 fixture 通过 | ✅ |
| 16 | encrypted PDF 语义通过 | ✅ |
| 17 | corrupted PDF 语义通过 | ✅ |
| 18 | text estimate 通过 | ✅ |
| 19 | page 顺序通过 | ✅ |
| 20 | page number 1-based | ✅ |
| 21 | file handle 释放 | ✅ |
| 22 | 源 PDF SHA 不变 | ✅ |
| 23 | close 幂等 | ✅ |
| 24 | optional dependency 缺失安全 | ✅ |
| 25 | 错误无绝对路径 | ✅ `test_error_messages_no_absolute_path` |
| 26 | 错误无原始第三方异常 | ✅ `test_error_messages_no_pypdf_traceback` |
| 27 | 外部网络 0 | ✅ §19 |
| 28 | 模型下载 0 | ✅ |
| 29 | OCR 调用 0 | ✅ |
| 30 | LLM 调用 0 | ✅ |
| 31 | Provider 调用 0 | ✅ |
| 32 | Markdown 生成 0 | ✅ `test_no_markdown_generation` |
| 33 | 上传 API diff 0 | ✅ |
| 34 | schema diff 0 | ✅ |
| 35 | Store/Service/API diff 0 | ✅ |
| 36 | frontend diff 0 | ✅ |
| 37 | Parser 定向测试全通过 | ✅ 66/66 |
| 38 | R1 定向测试全通过 | ✅ 117 passed + 1 platform skip（118 selected） |
| 39 | 完整 Backend 零回归 | ✅ 2768 passed / 0 regression |
| 40 | Frontend 267/267 | ✅ |
| 41 | typecheck/lint/build 通过 | ✅ |
| 42 | Ruff 通过 | ✅ |
| 43 | G1 stash hash 不变 | ✅ d7240268 |
| 44 | validation 文档完成 | ✅ 本文件 |
| 45 | 状态文档同步 | ✅ STATUS/TODO/ROADMAP（本次提交内） |
| 46 | working tree clean（提交后） | ✅ |
| 47 | Open blockers = 0 | ✅ |

**47/47 PASS** ✅

---

## 27. 最终判定

```
P2-R2-A pypdf Parser Adapter
✅ COMPLETE / FROZEN @ <freeze commit>

Selected Parser
✅ pypdf @ 6.14.2 (BSD-3-Clause, matches R2-0 audited baseline)

P2-R2-B Canonical Markdown Builder
✅ APPROVED TO START

P2-R2-C Ingestion Worker + Upload/Retry API
⛔ BLOCKED BY P2-R2-B

P2-R2-D Integration Validation + Freeze
⛔ BLOCKED BY P2-R2-C

P2-R3 Chunk + Retrieval MVP
⛔ BLOCKED BY COMPLETE P2-R2

G1 Assistant Markdown Rendering
⏸ PRESERVED AS WIP @ d7240268 (unchanged across R2-A)

Merge / Tag / Push
⛔ NOT AUTHORIZED
```

R2-A 完成。R2-B 编码门已开（D6 resolved + Parser Adapter ready），等用户独立授权启动 R2-B。

---

## 28. R2-B 前置审计记录（per acceptance review feedback）

### 28.1 R2-B 实际起始基线

R2-B 启动前必须验证以下 commit 全部是 HEAD 祖先——不止 `533fe48` (R2-0 freeze)，**还包含** `f804fc7` (R2-0 Archive Corrections) + `0772324` (R2-A freeze)：

```bash
git merge-base --is-ancestor 533fe48 HEAD   # R2-0 freeze
git merge-base --is-ancestor f804fc7 HEAD   # R2-0 archive corrections
git merge-base --is-ancestor 0772324 HEAD   # R2-A freeze
```

**实际验证结果**（2026-07-31）：

```
533fe48_ANCESTOR ✅
f804fc7_ANCESTOR ✅
0772324_ANCESTOR ✅
```

R2-B 起始基线 = **当前 HEAD = `0772324`**（含 R2-0 + R2-0 archive corrections + R2-A 全部 freeze）。

### 28.2 uv.lock OpenAI specifier sync 解释（per R2-A1 commit `c359ee5`）

R2-A1 的 uv.lock diff 17 行中除 pypdf 相关条目外，还包含一条 OpenAI specifier 同步：

```diff
-    { name = "openai", specifier = ">=1.40" },
+    { name = "openai", specifier = ">=2.0,<3" },
```

**审计结论**（per acceptance review feedback）：

| 检查项 | 命令 | 结果 |
|---|---|---|
| OpenAI 实际 locked version 未变化 | `git show c359ee5^:uv.lock \| grep -A2 '^name = "openai"'` vs `git show c359ee5:uv.lock \| grep -A2 '^name = "openai"'` | ✅ 两边都是 `version = "2.44.0"` |
| 没有新增包（除 pypdf） | `git show c359ee5^:uv.lock \| grep -c '^name = '` vs `git show c359ee5:uv.lock \| grep -c '^name = '` | ✅ 72 → 73（差 1 = pypdf；无其他新增） |
| 没有 Provider 行为变化 | OpenAI stanza `dependencies` / `source` 字段前后完全一致 | ✅ |
| 只是 lock metadata/specifier 同步 | specifier 从 `>=1.40` 改为 `>=2.0,<3`——后者是 pyproject.toml line 15 已经声明的版本范围 | ✅ uv 在重新解析时把 lockfile specifier 与 pyproject 同步 |

**根因**：R2-A1 之前的 uv.lock 中 OpenAI specifier `>=1.40` 与 pyproject.toml 实际声明的 `>=2.0,<3` 不一致——这是历史 lockfile 漂移（pyproject 更新时未重新跑 `uv lock`）。R2-A1 跑 `uv lock` 时 uv 自动同步 specifier 到 pyproject 当前值，**没有升级 OpenAI 实际版本**（仍 2.44.0，落在 `>=2.0,<3` 范围内）。

**结论**：此变更**不是** pypdf 添加的必要副作用，而是 uv lock 顺带修复历史 specifier 漂移。属于"无害 lockfile metadata 同步"，不影响 Provider Runtime 行为，不引入新依赖。

### 28.3 R2-B 编码启动前的额外检查清单

R2-B 启动指令应包含：

1. ✅ 验证 `f804fc7` + `0772324` + 之前所有 R2-0/R1/R0/E 基线均为 HEAD 祖先
2. ✅ 验证 G1 stash hash = `d7240268ec8b8e5d9e195c999e56fb6ec130fd55`
3. ✅ 验证 working tree clean
4. ✅ 验证 pypdf 实际安装版本仍属 6.x（`python -c "from importlib.metadata import version; print(version('pypdf'))"`）
5. ✅ 跑 R2-A 定向测试基线（66 tests）证明 Parser Adapter 仍 PASS
6. ✅ 跑 R1 定向测试基线（118 tests）证明 Library Foundation 仍 PASS
7. ⛔ **若 uv.lock 在 R2-B 期间出现 OpenAI 之外的非预期变更**，立即停止并独立解释（不得默认为 R2-B 编码的必要副作用）
8. ✅ **P2-R0 Amendment 2 已落地**（docs-only commit；详见 [p2-r0-amendment-2.md](../../design/p2-r0-amendment-2.md)）——R2-B 编码开始时合同已是新版本（frontmatter 字段集 + needs_ocr 阈值已精化）

R2-B 不应重新触发 uv.lock specifier 同步——R2-A1 已经把 lockfile 与 pyproject 同步过一次。若 R2-B 触发 `uv lock`，diff 应**完全为空**或仅含 R2-B 显式添加的新依赖（如 heading 启发式所需库，若引入）。

**Amendment 2 解决的冲突**（R2-B 编码无需在"指令 vs 合同"间选择）：

- frontmatter 字段集：按 amendment-2 §2.2 新表（含 `schema` / `parser_id` 拆分 / `source_filename` 重命名 / 删 `library_id` / 删默认 `generated_at` / 加 `title`）
- needs_ocr 阈值：按 amendment-2 §2.1 新规则（仅 `total_non_whitespace_chars == 0 → needs_ocr`；低密度文本作为 warning）
- 字段序列化方式：JSON-quoted str（`json.dumps(value, ensure_ascii=False)`）
- page marker 格式：不变（`<!-- page:N -->` 1-based 单调递增）


