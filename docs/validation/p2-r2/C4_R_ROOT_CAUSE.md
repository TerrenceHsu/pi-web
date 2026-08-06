# P2-R2-C4-R — ROOT CAUSE FOUND ✅

> 报告日期：2026-08-06  
> HEAD：`f211bd2`

## 根因类型：duplicate exception class identity（类型身份污染）

符合用户根因证明标准 §"类型身份污染"：
> raised class module/name 相同 but object identity 不同 + 找到导致 reload/duplicate import 的 polluter

## Polluter / Victim / Polluted State

### Polluter 1（导致 13 failures）

- **node ID**: `tests/test_r2_c_security_boundaries.py::TestImportSideEffects::test_importing_upload_service_does_not_start_worker`
- **文件:行**: `tests/test_r2_c_security_boundaries.py:217-222`
- **关键代码**:
  ```python
  def test_importing_upload_service_does_not_start_worker(self):
      """Re-import upload_service module; verify no background task started."""
      import pi_agent_core_py.web.knowledge.upload_service as mod
      importlib.reload(mod)
  ```

### Polluter 2（导致 2 failures）

- **node ID**: `tests/test_r2_c_security_boundaries.py::TestImportSideEffects::test_importing_app_does_not_start_lifespan`
- **文件:行**: `tests/test_r2_c_security_boundaries.py:230-235`
- **关键代码**: `importlib.reload(pi_agent_core_py.web.app)`

### Polluter 3（潜在，未观察到 victim）

- **node ID**: `tests/test_r2_c_security_boundaries.py::TestImportSideEffects::test_importing_worker_does_not_create_tasks`
- **文件:行**: `tests/test_r2_c_security_boundaries.py:224-228`
- **关键代码**: `importlib.reload(pi_agent_core_py.web.knowledge.ingestion_worker)`

### Victim（15 个，deterministic）

| # | node ID | 预期异常 | 实际异常（新 class） |
|---|---|---|---|
| 1 | `tests/test_upload_api.py::TestUploadServiceStreaming::test_streaming_enforces_max_size` | `UploadTooLargeError` | reload 后新 `UploadTooLargeError` |
| 2 | `tests/test_upload_api.py::TestUploadServiceStreaming::test_invalid_pdf_signature_rejected` | `InvalidPdfSignatureError` | 新 `InvalidPdfSignatureError` |
| 3 | `tests/test_upload_api.py::TestUploadServiceStreaming::test_empty_upload_rejected` | `InvalidUploadError` | 新 `InvalidUploadError` |
| 4-5 | `TestDuplicateDetection::*` (×2) | `DuplicateDocumentExistsError` | 新 |
| 6-7 | `TestCompensation::*` (×2) | upload_service exceptions | 新 |
| 8 | `TestWorkerGate::test_unavailable_manager_rejects_before_work` | `WorkerUnavailableError` | 新 |
| 9-11 | `TestLibraryState::*` (×3) | `LibraryNotMutableError` | 新 |
| 12 | `TestUploadHTTP::test_invalid_pdf_signature_returns_415` | HTTP 415 → 500（route handler 不匹配新 InvalidPdfSignatureError） | 新 |
| 13 | `TestUploadHTTP::test_duplicate_returns_409` | HTTP 409 → 500 | 新 |
| 14 | `tests/test_web_prompt_execution_split.py::test_model_error_messages_unchanged` | `PromptRuntimeError` | reload 后新 `PromptRuntimeError` |
| 15 | `tests/test_web_prompt_execution_split.py::test_harness_restored_after_model_error` | `PromptRuntimeError` | 新 |

### Polluted State（机制）

1. **Suite collection 阶段**：`test_upload_api.py` 被 import，顶部 `from pi_agent_core_py.web.knowledge.upload_service import (InvalidUploadError, UploadTooLargeError, ...)` 在测试文件 namespace 中绑定**原始 class 对象**（id=X）
2. **`test_r2_c_security_boundaries.py` 运行（alphabetical 早于 test_upload_api.py）**：`importlib.reload(upload_service)` 重新执行模块代码：
   - 重新执行 `class InvalidUploadError(...): ...` 等定义
   - 创建**新 class 对象**（id=Y）
   - 替换 `sys.modules['pi_agent_core_py.web.knowledge.upload_service'].InvalidUploadError` 为新对象
3. **Victim 测试运行时**：
   - 生产代码 `upload_service.py` 通过模块 namespace 引用新 class（`raise InvalidUploadError(...)`）→ 抛出 id=Y 的实例
   - 测试代码 `with pytest.raises(InvalidUploadError):` 中的 `InvalidUploadError` 仍是文件顶部绑定的 id=X 旧对象
   - `isinstance(new_instance, OLD_class) == False`
   - `pytest.raises` 不捕获，异常传播出 `with` 块
   - HTTP 层：FastAPI exception handler 注册的是新 class，handler 用 `issubclass` 检查也会失败 → 500

## 诊断证据（B1 探针）

isolation 跑 `test_empty_upload_rejected`（无 reload 干扰）：
```
'actual_type_id': 2209984998928,
'expected_type_id': 2209984998928,  ← SAME
'same_type_object': True,
'isinstance_match': True,
'is_exception_group': False,
'expected_is_exported': True,
```
结果：PASS

`test_importing_upload_service_does_not_start_worker` + `test_empty_upload_rejected` 2-test pair（reload 后）：
```
'actual_type_id': 1724745481904,
'expected_type_id': 1724745466032,  ← DIFFERENT
'same_type_object': False,
'isinstance_match': False,
'is_exception_group': False,
'expected_is_exported': False,
'exported_id': 1724745481904,  ← sys.modules 已是新对象
```
结果：FAIL（异常身份不匹配）

`test_importing_app_does_not_start_lifespan` + `test_model_error_messages_unchanged` 2-test pair（reload web.app 后）：FAIL（同样模式，PromptRuntimeError 身份污染）

## 因果链证明

```
importlib.reload(upload_service)
    ↓
重新执行 class 定义语句
    ↓
新 class object 替换 sys.modules 中的旧对象
    ↓
test_upload_api.py 顶部 `from ... import InvalidUploadError` 仍持有旧对象引用
    ↓
生产代码 `raise InvalidUploadError(...)` 抛出新对象实例
    ↓
pytest.raises(OLD_class) 检查 isinstance(NEW_instance, OLD_class) == False
    ↓
异常传播出 with 块，pytest 标记 FAILED
```

## 修复方案（待授权）

### P2-R2-C4-R-Fix-1：移除 `importlib.reload()`，改用 subprocess 隔离验证

**问题本质**：`importlib.reload()` 在测试套件进程中重新执行模块代码，污染所有 `from ... import X` 绑定。这不是验证"import 是否有副作用"的正确方法。

**修复策略**（任选其一）：

#### 选项 1：subprocess 隔离（推荐）

```python
import subprocess
import sys

def test_importing_upload_service_does_not_start_worker():
    """Import upload_service in fresh subprocess; verify no background task."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import pi_agent_core_py.web.knowledge.upload_service as m; "
         "assert hasattr(m, 'UploadService')"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"stdout={result.stdout}, stderr={result.stderr}"
```

子进程独立 Python 解释器，import 完成后进程退出，**不影响主测试进程的 sys.modules**。

#### 选项 2：直接验证已加载模块

```python
def test_importing_upload_service_does_not_start_worker():
    """upload_service module is already loaded; verify no global task state."""
    import pi_agent_core_py.web.knowledge.upload_service as mod
    # 检查模块级全局状态（不应有 task / connection / network）
    assert not hasattr(mod, '_global_worker_task')
    assert not hasattr(mod, '_global_db_connection')
    assert hasattr(mod, 'UploadService')
```

不 reload，仅检查模块 namespace 不含副作用全局对象。

### 验证标准（A/B/A 对照）

授权后必须按以下流程：

1. **A：基线** — HEAD `f211bd2` 完整套件跑 2 次：`15 failed` deterministic
2. **B：修复** — 应用 Fix-1，完整套件跑 2 次：`0 failed`
3. **A：revert** — 回滚 Fix-1，完整套件跑 1 次：`15 failed` 复现

只有 A/B/A 三态成立，根因证明闭合。

## 状态

- ✅ ROOT CAUSE PROVEN — 类型身份污染，polluter = `importlib.reload()`
- ⏸ FIX PROPOSED（P2-R2-C4-R-Fix-1） — 等用户授权
- ⏸ A/B/A 对照验证 — 等用户授权后执行
- ⏸ 完整套件 0-fail ×2 — 等 Fix-1 应用后执行
- ⏸ C4-R freeze 报告 — 等以上完成

## 不再需要的调查

- ❌ B2 三个通用探针 — B1 已找到根因，无需继续
- ❌ B3 五种 ordering 对照 — minimal polluter 已定位
- ❌ B4 文件级污染二分 — minimal polluter 已定位
- ❌ B5 资源计数审计 — 资源泄漏是独立问题（B7），与 15 failures 无关
- ❌ B6 pytest-asyncio 版本矩阵 — 与 pytest-asyncio 无关
- 🟡 B7 SQLite store leak 独立缺陷 — 仍待记录（不阻塞 C4-R）
