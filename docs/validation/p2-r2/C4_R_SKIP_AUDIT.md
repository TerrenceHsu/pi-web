# P2-R2-C4-R — 3rd Skip Audit

完整 backend 套件运行时观察到 **3 skipped**。逐一归档如下。

| # | node ID | 文件:行 | skip 原因 | platform 条件 | 未覆盖边界 | 等价替代测试 |
|---|---|---|---|---|---|---|
| 1 | `tests/test_knowledge_files_and_service.py::TestSymlinks::test_symlink_target_resolved` | `tests/test_knowledge_files_and_service.py:125` | `POSIX symlink test — Windows requires admin` | Windows（POSIX 才 skip） | symlink 解析在 Windows 上需 admin 权限创建 symlink | R1-era platform skip；功能在 Linux/Mac CI 自动覆盖 |
| 2 | `tests/test_multi_provider_runtime_restart.py::TestStorageMode::test_keyring_storage_mode_persists` | `tests/test_multi_provider_runtime_restart.py:448` | `keyring storage_mode not supported via this API path` | 跨平台 | keyring backend 在测试 API 路径下不可用（生产可能用系统 keyring） | P1-E M1 已记录；functional test 在真实 keyring 环境覆盖 |
| 3 | `tests/test_upload_api.py::TestUploadHTTP::test_archived_library_returns_409` | `tests/test_upload_api.py:722` | `HTTP-level archive test requires cross-loop async; service-level covered` | 跨平台 | HTTP 层 archive library upload → 409（需 cross-loop async 调用） | `TestLibraryState::test_archived_library_rejected`（service-level 已覆盖同样的 LibraryNotMutableError 路径） |

## 设计意图

- Skip #1：R1-era platform skip（Windows symlink 限制），不在 C4-R 修复范围
- Skip #2：P1-E provider keyring backend 限制，不在 C4-R 修复范围
- Skip #3：C3 显式设计决策（`pytest.skip(...)` 而非 platform skip），由 service-level `TestLibraryState::test_archived_library_rejected` 等价覆盖

## C4-R 行动

3 个 skip 全部为**预期 skip**，无未覆盖关键路径。C4-R 不需要"修复"任何 skip，但 freeze 报告必须显式列出每个 skip 的 node ID + reason + 等价覆盖证明。
