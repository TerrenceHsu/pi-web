"""Credentials 域子包（P1-E1 / E2 演进，目录重组收编）。

按域聚合（与 ``web/providers/`` 约定一致）：

- ``api``       —— Credential REST API（8 endpoints）+ BodyLimit middleware
- ``dto``       —— API 层 DTO（request/response models）
- ``errors``    —— 域错误类型（跨 api/service/store 共享）
- ``service``   —— CredentialService（safe API 层）
- ``store``     —— SQLiteCredentialStore（repository 层）
- ``runtime``   —— Credential runtime composition root
- ``secret_store`` —— SecretStoreRouter（credential -> SecretStore 路由）

**不做** re-export——调用方按子模块 import：

```python
from pi_agent_core_py.web.credentials.api import build_full_credential_router
```
"""
