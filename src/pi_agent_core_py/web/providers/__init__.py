"""Provider Profiles / Provider 选择域子包（P1-E2 / M1-5 演进，目录重组收编）。

按域聚合（与 ``web/knowledge/``、``web/credentials/`` 约定一致）：

- ``api``           —— Provider Profiles REST API（7 endpoints）+ BodyLimit middleware
- ``config_store``  —— SQLiteProviderConfigStore（profiles / bindings repository）
- ``config_service``—— ProviderConfigService（profile 管理 + session binding）
- ``config_runtime``—— Provider config runtime composition root
- ``runtime``       —— RequestProviderRuntime（请求级 provider 选择与绑定）
- ``validation``    —— credential 校验策略（registry 驱动）
- ``model_options`` —— profile 模型选项解析

注意与**核心包** ``pi_agent_core_py.providers``（registry / factory / base）
区分：本子包是 web 层的 profile 配置与选择；核心 provider 适配器不在其中。

**不做** re-export——调用方按子模块 import：

```python
from pi_agent_core_py.web.providers.api import build_full_provider_profile_router
```
"""
