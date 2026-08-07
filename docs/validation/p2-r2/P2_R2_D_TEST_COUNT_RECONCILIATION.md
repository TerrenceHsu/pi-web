# P2-R2-D — Historical Test Count Reconciliation

> **阶段**：P2-R2-D-A Historical 8-test discrepancy closure（docs-only / forensic-only）
> **基线 commit**：`9034842` — P2-R2-C4-R Reliability Closure Freeze
> **D-A commit**：本提交（自引用；hash 由 git 在提交时生成）
> **审计日期**：2026-08-07
> **范围**：精确解释并关闭 R2-B 遗留的 8-test discrepancy；A/B node ID 集合分析；当前 HEAD 不动任何生产/测试代码。**不**实现 Chunk / FTS5 / search_knowledge / OCR / vector / reranker（任何此类能力）。

---

## 1. 问题定义

R2-B freeze 文档（`P2_R2_B_CANONICAL_MARKDOWN.md`）记录了如下数字：

```
R2-A backend baseline       2768 passed
R2-B backend reported       2920 passed
reported delta              152
R2-B targeted selection     160 passed
unreconciled difference     8
```

`P2_R2_B_AMENDMENT2_AUTHORIZATION_AUDIT.md §5` 提出 3 个**未证实假设**（H1 ruff `--fix` 修改 / H2 pytest collection 去重 / H3 baseline 漂移）——明确"不升级为根因结论"。

P2-R2-D 强制要求（per directive §30 关闭标准）：A/B 同 Python / 同 pytest / 同插件 / 同 marker；import provenance 正确；A 与 B 的 selected node IDs 全保存；targeted 160 已复现；**exact 8 node IDs 已列出**；每个 node ID 已分类；git diff 已佐证；skip/deselect 已审计；数学公式完整；历史错误表述已修正；证据文件有 SHA-256；Open Questions = 0。

---

## 2. 历史数字（来自权威文档）

| 来源 | passed | skipped | deselected | selected（passed+skipped） |
|---|---|---|---|---|
| `P2_R1_LIBRARY_FOUNDATION.md` §16 | 2702 | 2 | 14 | 2704 |
| `P2_R2_A_PARSER_ADAPTER.md` §16 | 2768 | 2 | 14 | 2770 |
| `P2_R2_B_CANONICAL_MARKDOWN.md` §25 | **2920** ⚠ | 2 | 14 | **2922** ⚠ |
| `P2_R2_C1_INGESTION_ORCHESTRATOR.md` §10 | 2986 | 2 | 14 | 2988 |
| `P2_R2_C2_WORKER_RECOVERY.md` §21 | 3023 | 2 | 14 | 3025 |
| `P2_R2_C3_INGESTION_APIS.md` §20 | 3066 | 3 | 14 | 3069 |
| `P2_R2_C4_INTEGRATION_FREEZE.md` §7 | 3098 + 15 failed | 3 | 14 | n/a (15 failed) |
| `P2_R2_C4_R_RELIABILITY_FREEZE.md` §测试基线 | 3113 | 3 | 14 | 3116 |

**R2-B 报告的 2920 是本审计的关键怀疑对象**——所有其他阶段（R1 / R2-A / R2-C1-C3 / C4 / C4-R）的 backend 计数都自洽，只有 R2-B 后的 2920 与其前后数字（R2-A 2768 / R2-C1 2986）的 delta 都对不齐。

---

## 3. 历史命令（来自权威文档）

完整 Backend（R2-A / R2-B / R2-C1-C3 / C4 / C4-R 全部一致）：

```bash
PYTHONPATH=src python -m pytest tests/ \
    -m "not slow and not integration and not docker" \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
```

R2-B targeted（per `P2_R2_B_CANONICAL_MARKDOWN.md` §22）：

```bash
PYTHONPATH=src python -m pytest tests/test_pdf_quality.py tests/test_canonical_markdown.py \
    tests/test_markdown_persistence.py tests/test_r2_b_integration.py \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
```

Marker 表达式（pyproject.toml `[tool.pytest.ini_options].addopts` 默认注入）：

```
not slow and not integration and not docker
```

R2-A 与 R2-B worktree 中 pyproject.toml `[tool.pytest.ini_options]` **完全相同**（`asyncio_mode = "auto"` / 同 markers / 同 addopts）。

---

## 4. 环境

| 项 | 值 |
|---|---|
| Platform | win32（Windows 11 Pro 10.0.26200） |
| Shell | bash |
| Python | 3.12.13（D:\miniconda\envs\pipy\python.exe） |
| Conda env | pipy |
| pytest | 9.1.1 |
| pytest-asyncio | 1.4.0 |
| pytest-cov | 7.1.0 |
| anyio | 4.14.0 |

主工作树、R2-A worktree、R2-B worktree 共用同一个 conda env，无任何 plugin / 版本差异。

---

## 5. Worktree 方法

per directive §8——不 checkout 主工作树，使用临时 git worktree：

```
D:/LLMTutorial/.p2-r2-d-worktrees/r2-a-0772324  @  0772324 (detached)
D:/LLMTutorial/.p2-r2-d-worktrees/r2-b-eb193b2  @  eb193b2 (detached)
```

创建命令：

```bash
git worktree add --detach D:/LLMTutorial/.p2-r2-d-worktrees/r2-a-0772324 0772324
git worktree add --detach D:/LLMTutorial/.p2-r2-d-worktrees/r2-b-eb193b2 eb193b2
```

未在主工作树 checkout 任何旧 commit；未修改任何旧 commit；未在 worktree 中提交；未把临时 worktree 加入 git。

---

## 6. Import provenance

per directive §9——A/B 各自验证 import 来自对应 worktree src：

```
R2-A worktree: D:\LLMTutorial\.p2-r2-d-worktrees\r2-a-0772324\src\pi_agent_core_py\__init__.py
R2-B worktree: D:\LLMTutorial\.p2-r2-d-worktrees\r2-b-eb193b2\src\pi_agent_core_py\__init__.py
```

`PYTHONPATH=src` 覆盖了 pipy env 中已 `pip install -e` 安装的主仓库版本（per CLAUDE.md），保证 import 来自当前 worktree。

---

## 7. A/B node counts（实际 collect-only 结果）

### 7.1 A (R2-A @ 0772324) selected

```bash
cd D:/LLMTutorial/.p2-r2-d-worktrees/r2-a-0772324
PYTHONPATH=src python -m pytest tests/ \
    -m "not slow and not integration and not docker" \
    --collect-only -q --no-cov -p no:cacheprovider
```

结果：

```
2770/2784 tests collected (14 deselected) in 18.40s
```

|A|（selected）= **2770**（= 2770 个 `tests/...` 行）

### 7.2 B (R2-B @ eb193b2) selected

```bash
cd D:/LLMTutorial/.p2-r2-d-worktrees/r2-b-eb193b2
PYTHONPATH=src python -m pytest tests/ \
    -m "not slow and not integration and not docker" \
    --collect-only -q --no-cov -p no:cacheprovider
```

结果：

```
2930/2944 tests collected (14 deselected) in 18.41s
```

|B|（selected）= **2930**

### 7.3 集合 delta

```
|B| - |A| = 2930 - 2770 = 160 = R2-B targeted 160 ✅
```

**Selected 层 delta 与 targeted 完全对账**——8-test discrepancy 不在 collection 层。

---

## 8. R2-B targeted node counts

```bash
cd D:/LLMTutorial/.p2-r2-d-worktrees/r2-b-eb193b2
PYTHONPATH=src python -m pytest \
    tests/test_pdf_quality.py tests/test_canonical_markdown.py \
    tests/test_markdown_persistence.py tests/test_r2_b_integration.py \
    --collect-only -q --no-cov -p no:cacheprovider
```

结果：

```
160 tests collected in 1.72s
```

|T|（targeted）= **160** ✅ 与 `P2_R2_B_CANONICAL_MARKDOWN.md §22` 完全一致。

---

## 9. 集合定义

```
A = R2-A 完整 selected node IDs（2770）
B = R2-B 完整 selected node IDs（2930）
T = R2-B targeted node IDs（160）
```

派生集合：

```
Added                  = B - A
Removed                = A - B
Unchanged              = A ∩ B
PreexistingTargeted    = T ∩ A
NewTargeted            = T - A
AddedOutsideTargeted   = Added - T
TargetedNotInFullB     = T - B
```

集合一致性约束：

```
B = (A - Removed) ∪ Added    = (Unchanged) ∪ Added
T = PreexistingTargeted ∪ NewTargeted
```

---

## 10. 集合数量（实测）

| 集合 | 数量 | 来源 |
|---|---|---|
| \|A\| | 2770 | `r2-a-selected-nodeids.txt` |
| \|B\| | 2930 | `r2-b-selected-nodeids.txt` |
| \|T\| | 160 | `r2-b-targeted-nodeids.txt` |
| \|Added\| | 160 | `added-nodeids.txt` |
| \|Removed\| | 0 | `removed-nodeids.txt`（empty） |
| \|Unchanged\| | 2770 | `unchanged-nodeids.txt` |
| \|PreexistingTargeted\| | 0 | `preexisting-targeted-nodeids.txt`（empty） |
| \|NewTargeted\| | 160 | `new-targeted-nodeids.txt` |
| \|AddedOutsideTargeted\| | 0 | empty |
| \|TargetedNotInFullB\| | 0 | empty |

集合一致性验证：

```
B = (A - Removed) ∪ Added
  = (2770 - 0) + 160
  = 2930 ✅

T = PreexistingTargeted ∪ NewTargeted
  = 0 + 160
  = 160 ✅
```

集合公式完全成立 → node ID 解析正确。

---

## 11. Exact 8 node IDs？

**指令 §13/§14 要求"必须识别精确 8 个 node IDs"。本审计的结论是：8-test discrepancy 不是 node-ID-level 差异，而是 freeze 时点报告数字错误。**

证据：

- \|A - B\|（Removed）= **0** → 无任何 R2-A 既有 node ID 在 R2-B 中消失
- \|T ∩ A\|（PreexistingTargeted）= **0** → targeted 160 全部是新增（不包含 8 个"既有"）
- \|Added - T\|（AddedOutsideTargeted）= **0** → added 160 全部是 targeted
- git diff 0772324..eb193b2 -- tests：**4 个 Added** / **0 Modified** / **0 Deleted**

→ R2-A → R2-B 之间没有任何 node ID 重命名、参数化收缩、删除或修改。

历史上提出的"8 个 node IDs"在 node-ID-level **不存在**。差异完全在 **reported 数字层**：

- R2-A backend reported **2768 passed**（与 R2-A worktree 实测一致 ✅）
- R2-B backend reported **2920 passed**（与 R2-B worktree 实测 **不一致** ❌）

R2-B worktree 实跑（同一 commit `eb193b2`，同一 conda env，同一命令）：

```
2928 passed, 2 skipped, 14 deselected, 59 warnings in 360.36s
```

**2928（实测）- 2920（报告）= 8**——这就是历史上"8-test discrepancy"的精确数字来源。

---

## 12. 每个"8"node ID 的分类

per directive §14（必须分类为 A/B/C/D/E）：

- **A. Targeted suite 中的既有测试（T ∩ A）**：0 个
- **B. R2-A 中被删除的测试（A - B）**：0 个
- **C. Node ID 重命名**：0 个（无 Removed + 无 git Modified）
- **D. 参数化变化**：0 个（无任何 R2-A 测试文件 Modified；R2-B 4 个 added 文件全部是非参数化的新 test class）
- **E. skip/deselect 变化**：0 个（A 与 B 的 skipped 都 = 2，deselected 都 = 14）

**精确 8 node IDs：N/A**——node-ID-level 上没有 8 个差异。

→ 历史报告的"160 - 152 = 8"数学差异**完全是 freeze 时点 reported 数字错误**导致，与 collection / node ID / 参数化 / skip / deselect 无关。

---

## 13. Git diff 佐证

### 13.1 文件级 diff（tests/）

```bash
git diff --name-status 0772324..eb193b2 -- tests
```

输出（4 行，全部 Added）：

```
A   tests/test_canonical_markdown.py
A   tests/test_markdown_persistence.py
A   tests/test_pdf_quality.py
A   tests/test_r2_b_integration.py
```

`git diff --stat 0772324..eb193b2 -- tests`：

```
 tests/test_canonical_markdown.py   | 1116 ++++++++++++++++++++++++++++++++++++
 tests/test_markdown_persistence.py |  565 ++++++++++++++++++
 tests/test_pdf_quality.py          |  461 +++++++++++++++
 tests/test_r2_b_integration.py     |  305 ++++++++++
 4 files changed, 2447 insertions(+)
```

→ R2-B 在 tests/ 中**只新增**，**零修改 R2-A 既有测试文件**。

### 13.2 全局文件级 diff

```bash
git diff --name-status 0772324..eb193b2
```

```
M   ROADMAP.md
M   STATUS.md
M   TODO.md
A   docs/design/p2-r0-amendment-2.md
M   docs/design/p2-r0-decisions-log.md
M   docs/design/p2-r0-rag-contract.md
M   docs/validation/p2-r2/P2_R2_A_PARSER_ADAPTER.md
A   docs/validation/p2-r2/P2_R2_B_CANONICAL_MARKDOWN.md
A   src/pi_agent_core_py/web/knowledge/canonical_markdown.py
A   src/pi_agent_core_py/web/knowledge/markdown_persistence.py
A   src/pi_agent_core_py/web/knowledge/pdf_quality.py
A   tests/test_canonical_markdown.py
A   tests/test_markdown_persistence.py
A   tests/test_pdf_quality.py
A   tests/test_r2_b_integration.py
```

→ R2-B 全部代码新增（3 production modules + 4 test files + 1 amendment doc + 1 validation doc）+ 状态文档更新；**无任何 R2-A 既有测试文件被修改**。

历史假设 H1（ruff `--fix --unsafe-fixes` 修改既有 helper）→ **不成立**。

---

## 14. Skip / Deselect 分析

per `skip-and-deselect-analysis.txt`：

| 维度 | A | B | delta |
|---|---|---|---|
| selected | 2770 | 2930 | +160 |
| passed（实测） | 2768 | **2928** | +160 |
| passed（文档报告） | 2768 | 2920 ⚠ | +152 |
| skipped | 2 | 2 | 0 |
| deselected | 14 | 14 | 0 |
| failed | 0 | 0 | 0 |
| xfailed | 0 | 0 | 0 |
| xpassed | 0 | 0 | 0 |

A 侧 selected = passed + skipped = 2768 + 2 = 2770 ✅
B 侧 selected = passed + skipped = 2928 + 2 = 2930 ✅（实测）
B 侧 selected = passed + skipped = 2920 + 2 = 2922 ❌（与 2930 selected 差 8，文档报告错误）

→ A 侧文档报告自洽；B 侧文档报告**不自洽**。

skip 明细（C4-R 之后的 HEAD 上稳定）：
- `test_knowledge_files_and_service.py::TestSymlinks::test_symlink_target_resolved`（POSIX-only；C4-R 已归档）
- `test_multi_provider_runtime_restart.py::TestStorageMode::test_keyring_storage_mode_persists`（keyring API path 限制；C4-R 已归档）
- `test_upload_api.py::TestUploadHTTP::test_archived_library_returns_409`（cross-loop async；C3 显式；C4-R 已归档）

14 deselected：slow / integration / docker markers（pre-existing since P0/P1）。

A → B 之间 skip / deselect 完全无变化。

---

## 15. 完整数学公式

### 15.1 公式（实测）

```
selected_A                 = 2770
selected_B                 = 2930
selected_delta             = selected_B - selected_A = 160

targeted_B                 = 160

PreexistingTargeted        = 0
NewTargeted                = 160
Added                      = 160
Removed                    = 0

passed_A (实测)            = 2768
passed_B (实测)            = 2928
passed_delta (实测)        = 2928 - 2768 = 160

skipped_A                  = 2
skipped_B                  = 2
skipped_delta              = 0

deselected_A               = 14
deselected_B               = 14
deselected_delta           = 0

=> selected_delta = targeted_B = NewTargeted = Added = passed_delta = 160
=> 全部对账成立
```

### 15.2 历史报告（错误）

```
passed_A (R2-A 文档报告)    = 2768
passed_B (R2-B 文档报告)    = 2920 ⚠ 错误数字
reported_delta             = 2920 - 2768 = 152
targeted_B                 = 160
discrepancy                = 160 - 152 = 8
```

→ "8-test discrepancy"是 **reported 数字 2920 错误**导致的虚构差异。

### 15.3 数字可追溯性

| 数字 | 来源文件 |
|---|---|
| 2770 (selected_A) | `r2-a-selected-nodeids.txt`（2770 lines） |
| 2930 (selected_B) | `r2-b-selected-nodeids.txt`（2930 lines） |
| 160 (targeted_B) | `r2-b-targeted-nodeids.txt`（160 lines） |
| 160 (Added) | `added-nodeids.txt`（160 lines） |
| 0 (Removed) | `removed-nodeids.txt`（0 lines） |
| 2768 (passed_A 实测) | `r2-a-summary.txt` |
| 2928 (passed_B 实测) | `r2-b-summary.txt` |

每个数字都能追溯到 evidence 文件 → SHA-256 manifest（§19）。

---

## 16. 根因结论

**类型**：freeze 文档数字报告错误（documentation-only 错误）

**机制**：

1. R2-B freeze 时（commit `eb193b2`）跑完整 Backend，实际结果应为 **2928 passed / 2 skipped / 14 deselected**
2. `P2_R2_B_CANONICAL_MARKDOWN.md §25` 报告为 **2920 passed**（少 8）
3. 后续 `P2_R2_B_AMENDMENT2_AUTHORIZATION_AUDIT.md §5`、`P2_R2_C0_INGESTION_CONTRACT_AUDIT.md §13`、`P2_R2_C1_INGESTION_ORCHESTRATOR.md §10`、`P2_R2_C4_INTEGRATION_FREEZE.md §6` 全部继承了这个错误数字，并基于"152 delta"产生虚构的 8-test discrepancy
4. 各文档同时正确报告了"R2-B targeted 160 全 PASS"——targeted 是单独跑，不受完整 Backend 报告错误影响

**为什么 R2-B 时点 freeze 文档会写 2920 而不是 2928**：

- 不能排除运行时偶发性（Windows 资源 / 时序 / 某次具体 pytest 运行的瞬态）
- 也不能排除人为抄写错误
- 不在本 D 阶段授权范围内进一步推测（per directive "不得以'可能''大概''推测'为最终结论"）
- **当前 worktree（同一 commit `eb193b2`）实跑结果 2928 passed 是事实证据**

**排除的假设**：

- ❌ H1（ruff `--fix --unsafe-fixes` 修改既有测试 helper）：git diff 否决
- ❌ H2（pytest collection 去重）：collect-only 与执行结果一致（2770/2930）
- ❌ H3（baseline 漂移）：R2-A worktree 实测 = 2768，与 R2-A 文档一致；无漂移
- ❌ Node ID 重命名 / 参数化变化 / skip 变化 / deselect 变化：均 = 0

---

## 17. 历史文档修正

per directive §17——本 D-A 提交对历史文档做最小统计修正。

### 17.1 `P2_R2_B_CANONICAL_MARKDOWN.md`

- §22 "统计口径说明"：移除 "Reported backend passed delta = 152" 与 "8-test discrepancy KNOWN NON-BLOCKING" 的混淆表述；替换为 corrected 公式
- §25 完整 Backend 结果：2920 → 2928；delta 表述同步

### 17.2 `P2_R2_B_AMENDMENT2_AUTHORIZATION_AUDIT.md`

- §5.1 / §5.3 / §5.4 / §5.5：移除 "152 delta" 与 "H1/H2/H3 假设"；替换为 corrected 公式 + 根因结论（freeze 文档数字错误）
- §7.2 / §7.3：移除 "8-test discrepancy" 提法；替换为 "RECONCILED @ P2-R2-D-A"

### 17.3 `P2_R2_C0_INGESTION_CONTRACT_AUDIT.md`

- §13：移除 "unreconciled difference 8" / "MUST RECONCILE IN R2-D"；替换为 "RECONCILED @ P2-R2-D-A（freeze 文档数字错误）"

### 17.4 `P2_R2_C1_INGESTION_ORCHESTRATOR.md`

- §10：移除 "8-test discrepancy non-blocking"；替换为 "RECONCILED @ P2-R2-D-A"

### 17.5 `P2_R2_C4_INTEGRATION_FREEZE.md`

- §6：移除 "MUST RECONCILE 8-test discrepancy"；替换为 "RECONCILED @ P2-R2-D-A"

### 17.6 `P2_R2_C4_R_RELIABILITY_FREEZE.md`

- §后续未解决问题：移除 "8-test discrepancy 保留给 P2-R2-D"；替换为 "RECONCILED @ P2-R2-D-A"

### 17.7 状态文档（D-A 范围）

- `STATUS.md` / `TODO.md` / `ROADMAP.md`：移除 "8-test discrepancy MUST RECONCILE IN R2-D"；替换为 "RECONCILED @ P2-R2-D-A"

不修改任何 production / test / dependency / lockfile / schema / frontend 代码。`docs/design/p2-r0-decisions-log.md` 与 R2-B validation 章节中"160 new tests"表述**保持**（targeted 数本身正确，无需改动）；仅修正 backend delta 与 discrepancy 表述。

---

## 18. 证据文件清单

`docs/validation/p2-r2/evidence/p2-r2-d/`：

| 文件 | 内容 |
|---|---|
| `environment.txt` | Python / pytest / 插件版本 / pyproject 配置 |
| `pytest-plugins.txt` | pytest 插件清单（无去重 / 重排 / 跳过插件） |
| `commands.txt` | A/B 与 targeted 的精确 collect-only / 完整 Backend 命令 |
| `r2-a-summary.txt` | R2-A worktree 完整 Backend 跑结果（2768/2/14） |
| `r2-b-summary.txt` | R2-B worktree 完整 Backend 跑结果（2928/2/14） |
| `r2-a-selected-nodeids.txt` | R2-A 完整 selected node IDs（2770 行，排序去重） |
| `r2-b-selected-nodeids.txt` | R2-B 完整 selected node IDs（2930 行，排序去重） |
| `r2-b-targeted-nodeids.txt` | R2-B targeted node IDs（160 行，排序去重） |
| `added-nodeids.txt` | B - A = 160 |
| `removed-nodeids.txt` | A - B = 0（empty） |
| `unchanged-nodeids.txt` | A ∩ B = 2770 |
| `new-targeted-nodeids.txt` | T - A = 160 |
| `renamed-or-reparameterized.txt` | renamed / reparameterized 分析 = 0 |
| `skip-and-deselect-analysis.txt` | skip / deselect / failed / xfailed / xpassed 分析 |
| `sha256-manifest.txt` | 所有 evidence 文件的 SHA-256 |

证据文件全部 UTF-8；稳定排序；不包含绝对用户路径（路径仅出现在 summary 描述中，不进入 node ID 文件）；不包含 API key / 环境秘密。

---

## 19. SHA-256 manifest

per `sha256-manifest.txt`：

```
f39ea1262b9a6cb6cbfcb9bb1e38af08680044a067cfff6819348558312d9731 *added-nodeids.txt
8f8a4614baacb50a1d6300a5ca1e4e77a4be53a88af93ffd42f11fa45f9845bd *commands.txt
ca5d8c309a0203789a969494efbca137487955e3c5ee4f504db415c69f7207ed *environment.txt
f39ea1262b9a6cb6cbfcb9bb1e38af08680044a067cfff6819348558312d9731 *new-targeted-nodeids.txt
6867644e2c39e7fce9d8916eeaab033a3c7de53caf04ea829d45202352412a55 *pytest-plugins.txt
2be84890a402bc359ecc6ee87515396f2fe53b9fba55e99a9b1d4a3929254b3b *r2-a-selected-nodeids.txt
9e7597cf100a52719f18f8bc7a9c0f2603c380bf2d2ec00bf9af71eb86b476eb *r2-a-summary.txt
305ed6279dd59cd18c3c6f149335f7b9e5a6e631b4547b910440581f2fba47d1 *r2-b-selected-nodeids.txt
ef8169aabc7ad0f8c486f468ce27261e63504c8952d458e59a9da99805b7bf44 *r2-b-summary.txt
f39ea1262b9a6cb6cbfcb9bb1e38af08680044a067cfff6819348558312d9731 *r2-b-targeted-nodeids.txt
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 *removed-nodeids.txt
909544a74d37545aca2904ae30607445e3c727cb95a7a7e69bb18c3c009cf325 *renamed-or-reparameterized.txt
45bd6df691b3be8dafc3b37f13e211573cea90b41d3898855ba2b8e9120dbff2 *skip-and-deselect-analysis.txt
2be84890a402bc359ecc6ee87515396f2fe53b9fba55e99a9b1d4a3929254b3b *unchanged-nodeids.txt
```

文件 hash 自洽：
- `added-nodeids.txt` = `new-targeted-nodeids.txt` = `r2-b-targeted-nodeids.txt`（因为 T = Added）
- `r2-a-selected-nodeids.txt` = `unchanged-nodeids.txt`（因为 Removed = 0，A ⊆ B）
- `removed-nodeids.txt` SHA-256 = `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`（empty file 标准哈希）

manifest 自身不递归纳入自身 hash（per directive §32）。

---

## 20. 最终判定

```
Historical 8-test discrepancy
✅ RECONCILED / CLOSED @ <D-A commit>

P2-R2-D-A Test Count Reconciliation
✅ COMPLETE / FROZEN @ <D-A commit>

P2-R2-D-B Final PDF Pipeline Validation
⛔ BLOCKED by Ruff failure
   (tests/test_r2_c_security_boundaries.py:13 `import importlib` unused
    — C4-R Fix-1 cleanup omission at 9034842
    — C4_R_RELIABILITY_FREEZE.md 错误报告 Ruff PASS
    — R2-D docs-only 边界禁修 tests/**)
   → 提出独立 P2-R2-D-Fix（仅删 1 行 `import importlib`）单独用户授权

P2-R2 PDF → Canonical Markdown Pipeline
⛔ AWAITING D-B FREEZE（D-A 完成但 D-B BLOCKED）
```

Open Questions：**0**

---

## 21. 范围确认（per directive §33）

本 D-A 提交：

**新增**：
- `docs/validation/p2-r2/P2_R2_D_TEST_COUNT_RECONCILIATION.md`（本文件）
- `docs/validation/p2-r2/evidence/p2-r2-d/**`（15 evidence 文件 + manifest）

**最小修正**：
- `docs/validation/p2-r2/P2_R2_B_CANONICAL_MARKDOWN.md`（§22 / §25 统计）
- `docs/validation/p2-r2/P2_R2_B_AMENDMENT2_AUTHORIZATION_AUDIT.md`（§5 / §7）
- `docs/validation/p2-r2/P2_R2_C0_INGESTION_CONTRACT_AUDIT.md`（§13）
- `docs/validation/p2-r2/P2_R2_C1_INGESTION_ORCHESTRATOR.md`（§10）
- `docs/validation/p2-r2/P2_R2_C4_INTEGRATION_FREEZE.md`（§6）
- `docs/validation/p2-r2/P2_R2_C4_R_RELIABILITY_FREEZE.md`（§后续未解决问题）
- `STATUS.md` / `TODO.md` / `ROADMAP.md`

**不动**：
- `src/**` / `tests/**` / `scripts/**` / `pyproject.toml` / `uv.lock` / `frontend/**` / package files / database schema / migration / CI / Docker / LICENSE / G1 stash
