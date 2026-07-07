"""Provider 错误类型（Step 21）。

ModelClient.stream 会捕获所有 ProviderError 子类和未知 Exception，
统一转成 `ErrorEvent`——不让 provider 原始异常穿透到 Agent loop。

异常层级：

```text
ProviderError
├── ProviderConfigError          # 配置缺失 / 非法（缺 api_key、bad base_url）
├── ProviderProtocolError        # 协议层错误（invalid tool input JSON / unknown event）
├── ProviderAuthenticationError  # 401 / 鉴权失败
├── ProviderRateLimitError       # 429 / 限流
└── ProviderStreamError          # 流式过程中网络异常 / 连接断开
```

设计要点：
- ProviderAdapter 内部抛 ProviderError 子类
- ModelClient.stream 捕获 ProviderError → ErrorEvent
- 其它未知异常也转 ErrorEvent，但保留 type/message
"""
from __future__ import annotations


class ProviderError(Exception):
    """所有 provider 异常的基类。"""


class ProviderConfigError(ProviderError):
    """配置错误——缺 api_key / 非法 base_url / model 名为空等。

    通常在 adapter 构造阶段抛，让上层能拿到清晰的初始化错误。
    """


class ProviderProtocolError(ProviderError):
    """协议错误——provider 返回了无法解析的内容。

    例如 tool_use 的 input 不是合法 JSON object。
    """


class ProviderAuthenticationError(ProviderError):
    """鉴权失败——401 / 403 / token 失效。"""


class ProviderRateLimitError(ProviderError):
    """限流——429 / quota 用尽。"""


class ProviderStreamError(ProviderError):
    """流式过程错误——连接断开 / 超时 / 中途网络异常。"""


__all__ = [
    "ProviderError",
    "ProviderConfigError",
    "ProviderProtocolError",
    "ProviderAuthenticationError",
    "ProviderRateLimitError",
    "ProviderStreamError",
]
