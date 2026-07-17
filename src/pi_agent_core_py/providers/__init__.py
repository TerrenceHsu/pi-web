"""Provider Adapter 子包（Step 21）。

把 provider-specific 协议适配（GLM / Anthropic 兼容 / OpenAI 兼容 / Fake）
从核心 runtime 中拆出来。Agent / Loop / Harness 只处理内部标准 `StreamEvent`，
不感知 provider 原始协议。

```text
Agent / Loop
  ↓ stream(system_prompt, messages, tools, signal)
ModelClient  (thin wrapper)
  ↓ ProviderRequest
ProviderAdapter
  ↓ StreamEvent (TextDeltaEvent / ToolCallEvent / DoneEvent / ErrorEvent)
```

设计要点：
- ProviderAdapter 只做协议转换，**不**执行工具 / 不做权限 / 不写 session
- ProviderError 子类是统一的 provider 异常类型；
  ModelClient.stream 捕获后转成 ErrorEvent，不让异常穿透 Agent loop
- FakeProviderAdapter 用于离线测试 / demo，与 FakeClient 等价
"""
from __future__ import annotations

from .anthropic_compat import (
    AnthropicCompatAdapter,
    AnthropicCompatConfig,
    to_anthropic_messages,
    to_anthropic_tools,
)
from .base import ProviderAdapter, ProviderRequest
from .errors import (
    ProviderAuthenticationError,
    ProviderConfigError,
    ProviderError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderStreamError,
)
from .fake import FakeProviderAdapter
from .glm import GLMConfig, GLMProviderAdapter
from .registry import (
    CredentialValidationStrategyId,
    ProviderAPIStyle,
    ProviderDefinition,
    ProviderHintConfidence,
    ProviderHintResult,
    ProviderRegistry,
    detect_provider_hint,
    get_provider_definition,
    list_provider_definitions,
)

__all__ = [
    # base
    "ProviderAdapter",
    "ProviderRequest",
    # errors
    "ProviderError",
    "ProviderConfigError",
    "ProviderProtocolError",
    "ProviderAuthenticationError",
    "ProviderRateLimitError",
    "ProviderStreamError",
    # adapters
    "FakeProviderAdapter",
    "AnthropicCompatAdapter",
    "AnthropicCompatConfig",
    "GLMProviderAdapter",
    "GLMConfig",
    # conversion helpers
    "to_anthropic_messages",
    "to_anthropic_tools",
    # registry (P1-E1-1 + E1-3B1)
    "ProviderAPIStyle",
    "CredentialValidationStrategyId",
    "ProviderDefinition",
    "ProviderHintConfidence",
    "ProviderHintResult",
    "ProviderRegistry",
    "detect_provider_hint",
    "get_provider_definition",
    "list_provider_definitions",
]
