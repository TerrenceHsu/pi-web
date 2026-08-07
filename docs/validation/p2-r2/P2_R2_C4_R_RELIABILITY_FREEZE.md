# P2-R2-C4-R — Reliability Closure FREEZE Report

> **HEAD after Fix-1**: 待 commit  
> **作者**: Hsu  
> **日期**: 2026-08-06  
> **基线**: P2-R2-C4 attempt @ `f211bd2`（NOT FROZEN）

## 阶段定位

P2-R2-C4 Integration Validation 阶段在 `f211bd2` 提交时声称 freeze，但完整 backend 套件 deterministic 出现 15 failures。C4-R Reliability Closure 启动，目标：定位 minimal polluter + 修复 + 完整套件连续 0 fail。

C4-R 完成后，**P2-R2-C 才能正式冻结**（`f211bd2` 仍保留为 C4 测试证据提交，C4-R 修复作为新 commit 补充）。

## Root Cause

**类型**：duplicate exception class identity（类型身份污染）

`tests/test_r2_c_security_boundaries.py::TestImportSideEffects` 三个测试使用 `importlib.reload()`：

| Polluter | reload target | 影响 |
|---|---|---|
| `test_importing_upload_service_does_not_start_worker` | `pi_agent_core_py.web.knowledge.upload_service` | 13 failures in `test_upload_api.py` |
| `test_importing_app_does_not_start_lifespan` | `pi_agent_core_py.web.app` | 2 failures in `test_web_prompt_execution_split.py` |
| `test_importing_worker_does_not_create_tasks` | `pi_agent_core_py.web.knowledge.ingestion_worker` | 潜在（未观察到 victim） |

**机制**：
1. `test_upload_api.py` 顶部 `from ... import InvalidUploadError` 在 suite collection 阶段绑定**原始 class 对象**（id=X）
2. `test_r2_c_security_boundaries.py` alphabetical 早于 `test_upload_api.py`，运行时 `importlib.reload()` 重新执行模块代码，创建**新 class 对象**（id=Y）并替换 `sys.modules`
3. 生产代码 `raise InvalidUploadError(...)` 通过模块 namespace 引用 id=Y；测试代码 `with pytest.raises(InvalidUploadError):` 仍持有 id=X
4. `isinstance(NEW_instance, OLD_class) == False` → `pytest.raises` 不捕获 → 异常传播出 `with` 块 → pytest 标记 FAILED

完整诊断证据见 `docs/validation/p2-r2/C4_R_ROOT_CAUSE.md`。

## Fix-1：subprocess 隔离

`tests/test_r2_c_security_boundaries.py::TestImportSideEffects` 三个测试不再 `importlib.reload()`，改为通过 `subprocess.run([sys.executable, "-c", "import X as m; assert ..."])` 在独立子进程验证 import side-effects。子进程 import 完成后退出，不影响主测试进程的 `sys.modules`，从根源消除类型身份污染。

## A/B/A 三态对照证据

| 阶段 | 状态 | 完整 backend 套件结果 | 证据 |
|---|---|---|---|
| **A baseline** | HEAD `f211bd2`（含 reload） | **15 failed** deterministic（2 次连续） | `/tmp/c4r/full_suite.log` + `/tmp/c4r/full_suite_2.log` |
| **B fix applied** | Fix-1（subprocess 隔离） | **3113 passed, 0 failed**（2 次连续） | `/tmp/c4r/fix_b1.log` + `/tmp/c4r/fix_b2.log` |
| **A revert** | Fix-1 stash（reload 恢复） | **15 failed** 复现（同 15 node IDs，1 次） | `/tmp/c4r/fix_a_revert.log` |

15 个失败 node ID 在 A baseline 与 A revert 中完全一致，确定性复现。

## C4-R Exit Gate 验收

| 准则 | 状态 |
|---|---|
| 完整 Backend 零回归 | ✅ **3113 passed, 0 failed, 3 skipped, 14 deselected** ×2 |
| C4 targeted 全部通过 | ✅ 47/47 PASS（保留自基线） |
| Open blockers = 0 | ✅ |
| Root cause PROVEN（minimal polluter + A/B/A） | ✅ |
| G1 stash 等价性审计 | ✅ CONTENT EQUIVALENCE CONFIRMED（patch-id + blob hash 全一致） |
| 第 3 个 skip 归档 | ✅ 3 个 skip 全部预期，文档化 |

## 测试基线（C4-R closure）

| 维度 | 结果 |
|---|---|
| 完整 backend | **3113 passed + 3 skipped + 14 deselected** ×2 deterministic |
| C4 targeted（5 文件） | 47/47 PASS（保留自基线） |
| C3 targeted | 43 PASS + 1 SKIP（保留） |
| C2 targeted | 37/37 PASS（保留） |
| C1 targeted | 58/58 PASS（保留） |
| R2-B regression | 160/160 PASS（保留） |
| R2-A regression | 66/66 PASS（保留） |
| R1 regression | 117 PASS + 1 platform SKIP（118 selected；**CORRECTED @ P2-R2-D-B**） |
| Frontend | 267/267 PASS（保留） |
| Typecheck / lint / build / Ruff | Typecheck / lint / build：PASS（保留）；**Ruff：⚠ archived claim INCORRECT — corrected by `a27494c`**（详见 §"后续未解决问题"） |
| 生产 diff | 仅 `tests/test_r2_c_security_boundaries.py` 一个文件，无生产代码改动 |
| Schema / dependency / frontend diff | 0 |

### C4-R Ruff archival correction（per P2-R2-D-B）

> **Functional reliability closure @ `9034842`：✅ VALID**（A/B/A 三态成立；subprocess isolation root cause proven；15 deterministic failures resolved）。
>
> **Archived Ruff PASS claim @ `9034842`：⚠ INCORRECT**——本 freeze 文档原始版本（line 72 et al.）记录"Ruff PASS（保留）"是不准确的。C4-R Fix-1 把 `importlib.reload()` 调用替换为 subprocess 隔离时，遗漏了清理顶部 `import importlib`；该 import 在 Fix-1 之后变为 unused，触发 Ruff F401。
>
> Stale import 影响**仅限 lint**：
> - 不影响 subprocess isolation 行为；
> - 不影响 A/B/A 因果证据；
> - 不影响生产代码（src/）；
> - 不影响完整 Backend 套件行为（3113 passed ×2 仍成立）。
>
> **Corrected by**: `a27494c` — `fix(test): remove stale import from C4-R isolation tests`（P2-R2-D-Fix；1 file / 1 line deletion；行为零变化）。
>
> **Post-fix**：Ruff PASS；Full Backend fresh process #1 + #2 = 3113 passed / 0 failed / 3 skipped / 14 deselected。
>
> **时间线（不改写历史 commit）**：
> - `9034842` ✅ functional reliability closure / ⚠ archived Ruff claim inaccurate
> - `a27494c` ✅ stale import cleanup / ✅ Ruff restored

## 3 个 skip（预期，全等价覆盖）

| # | node ID | reason | 等价覆盖 |
|---|---|---|---|
| 1 | `tests/test_knowledge_files_and_service.py::TestSymlinks::test_symlink_target_resolved` | POSIX symlink on Windows requires admin | Linux/Mac CI 自动覆盖 |
| 2 | `tests/test_multi_provider_runtime_restart.py::TestStorageMode::test_keyring_storage_mode_persists` | keyring storage_mode not supported via this API path | P1-E M1 functional test |
| 3 | `tests/test_upload_api.py::TestUploadHTTP::test_archived_library_returns_409` | HTTP-level archive test requires cross-loop async | `TestLibraryState::test_archived_library_rejected`（service-level） |

完整 skip 审计见 `docs/validation/p2-r2/C4_R_SKIP_AUDIT.md`。

## G1 stash 等价性审计结果

- 旧对象 `d7240268ec8b8e5d9e195c999e56fb6ec130fd55`（2026-07-30 22:12:17 +0800）
- 新对象 `5731ab7d...`（2026-08-04 10:44:50 +0800）
- 4 个 Modified 文件 patch-id **完全一致**：`6a0d747f7444c10df643c56f33ff0ebda68f5cad`
- `markdown.ts` blob hash **完全一致**：`195597587882a94341209ddad03009946d3787d5`
- C4 commits (f99caf5..f211bd2 + 169cb7e + f211bd2) **无 G1 文件泄露**
- 工作树清洁
- 结构差异（cosmetic）：OLD = 4 unstaged M + 1 untracked；NEW = 5 全 staged；`git stash pop` 工作树结果**完全一致**
- **CONTENT EQUIVALENCE CONFIRMED**

## 提交计划

C4-R closure 单一 commit（待用户授权）：

```
fix(rag): subprocess-isolate import side-effect tests (C4-R Fix-1)

Replace importlib.reload() in TestImportSideEffects with subprocess
isolation. The reload approach re-executed module code in the test
process, replacing class objects in sys.modules; any test that bound
exception classes via `from X import Y` at file load time kept stale
references, causing isinstance(NEW, OLD) == False and breaking
pytest.raises across 15 deterministic full-suite failures.

A/B/A verification:
  A baseline (HEAD f211bd2):  15 failed (×2 deterministic)
  B fix applied (this commit): 3113 passed, 0 failed (×2)
  A revert (stash this commit): 15 failed reproduced (×1, same node IDs)

Closes P2-R2-C4-R. Unblocks P2-R2-D. No production code changes.
```

变更内容：
- `tests/test_r2_c_security_boundaries.py`（modified）— Fix-1
- `docs/validation/p2-r2/P2_R2_C4_R_RELIABILITY_FREEZE.md`（new）— 本 freeze 报告
- `docs/validation/p2-r2/C4_R_ROOT_CAUSE.md`（new）— 根因证明
- `docs/validation/p2-r2/C4_R_SKIP_AUDIT.md`（new）— 3 个 skip 审计
- `docs/validation/p2-r2/C4_R_RESOURCE_AUDIT.md`（new）— 资源释放审计
- `docs/validation/p2-r2/C4_R_INVESTIGATION_STATUS.md`（new）— 调查中间状态快照

## 阶段门更新

```
P2-R2-C1 Store + Orchestrator         ✅ FROZEN @ 3e45da3
P2-R2-C2 Bounded Worker + Recovery    ✅ FROZEN @ 33a13a2
P2-R2-C3 Upload/Status/Retry/MD API   ✅ FROZEN @ f99caf5
P2-R2-C4 Test Evidence                🟡 RECORDED @ 169cb7e
P2-R2-C4 Freeze Candidate             ❌ NOT ACCEPTED @ f211bd2（pre-C4-R）
P2-R2-C4-R Reliability Closure        ✅ READY TO FREEZE（待 commit）
P2-R2-C PDF Ingestion Pipeline        ✅ COMPLETE / FROZEN (after C4-R commit)
P2-R2-D Final PDF Pipeline Validation ✅ APPROVED TO START（⚠ historical 8-test discrepancy → ✅ RECONCILED @ P2-R2-D-A；Ruff BLOCKER 发现 → 提出独立 P2-R2-D-Fix）
P2-R3 Heading-aware Chunk + FTS5      ⛔ BLOCKED BY COMPLETE P2-R2
P2-R4 search_knowledge + Page Marker  ⛔ BLOCKED BY P2-R3
```

## Merge / Tag / Push

⛔ NOT AUTHORIZED（任何阶段都不自动 merge/tag/push；需用户独立授权）

## 后续未解决问题（不阻塞 C4-R）

- **B7 SQLite store leak**：5 个 store 中 4 个（KnowledgeStore / SQLiteCredentialStore / ExtensionStore / SessionStore）的 `open()` 缺 try/except close 保护（仅 `SQLiteProviderConfigStore` 做对了）。是独立缺陷，不影响 15 failures，但建议后续作为独立 P2-R2-C4-R-Fix-2 处理（不阻塞 P2-R2-D）
- ~~**8-test discrepancy**（保留给 P2-R2-D）：R2-A baseline 2768 → R2-B reported 2920 → delta 152 → R2-B targeted 160 → 差异 8~~ → **✅ RECONCILED @ P2-R2-D-A**：R2-B freeze 时点误报 2920 passed（实测 2928）；selected delta = 160 = targeted；不存在 node-ID-level 差异。详见 [`P2_R2_D_TEST_COUNT_RECONCILIATION.md`](P2_R2_D_TEST_COUNT_RECONCILIATION.md)
- **Ruff failure**（2026-08-07 由 P2-R2-D-A 发现；**✅ RESOLVED @ P2-R2-D-Fix `a27494c`**）：`tests/test_r2_c_security_boundaries.py:13` `import importlib` 在 C4-R Fix-1（本 commit 9034842）移除 `importlib.reload()` 调用后变为 unused；Fix-1 漏了移除 import。本 freeze 文档原始版本错误报告 Ruff PASS。Stale import 已由 `a27494c`（1 line deletion，行为零变化）修复；post-fix Ruff PASS、Full Backend ×2 fresh process = 3113/3/14/0 failed。详见 §"C4-R Ruff archival correction"
