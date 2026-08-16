"""Web 子包入口（Step 20 新增）。

提供本地调试 Trace Viewer：

- `create_app(harness)` → FastAPI 实例
- `WebAppState` / `TraceEventBuffer` —— 运行态 / 事件缓冲
- `to_json_safe` + `serialize_*` —— JSON-safe 序列化

**不**从顶层 `pi_agent_core_py` 导出——避免普通 import 强依赖 FastAPI。
使用方式：

```python
from pi_agent_core_py.web import create_app
```

如果 FastAPI 未安装，import 本子包会抛 ImportError——调用方应该安装
`pip install -e ".[web]"`。
"""
from __future__ import annotations

from .app import create_app
from .auth import create_authenticated_app
from .serializers import (
    serialize_event,
    serialize_mcp_server_state,
    serialize_message,
    serialize_policy_audit_record,
    serialize_session,
    serialize_skill,
    serialize_snapshot_full,
    serialize_snapshot_summary,
    to_json_safe,
)
from .state import TraceEventBuffer, WebAppState

__all__ = [
    "create_app",
    "create_authenticated_app",
    "WebAppState",
    "TraceEventBuffer",
    "to_json_safe",
    "serialize_message",
    "serialize_event",
    "serialize_snapshot_summary",
    "serialize_snapshot_full",
    "serialize_session",
    "serialize_mcp_server_state",
    "serialize_skill",
    "serialize_policy_audit_record",
]
