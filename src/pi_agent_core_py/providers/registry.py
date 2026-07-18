"""Provider definitions registry + provider hint（P1-E1-1 + E1-3B1）.

定义内置 Provider 的 metadata，用于：
- P1-E1: provider hint 本地匹配 + credential validation strategy 冻结
- P1-E2: ProviderProfile 关联 + model catalog
- P1-E3: request-scoped ModelClient 创建

**关键安全约束**：
- `ProviderDefinition` 是 frozen dataclass——不可变
- `default_base_url` 必须是 HTTPS
- `credential_validation_endpoint`（非 None 时）必须是 HTTPS、不含 userinfo、
  host 与内置定义一致——不接受运行时自定义
- `credential_validation_strategy="unsupported"` 时 endpoint 必须为 None
- 其它 strategy 必须提供 HTTPS endpoint
- URL 不得内嵌 userinfo（`user:pass@host`）
- `detect_provider_hint()` 是纯函数——无 HTTP / socket / DNS 调用
- 不创建网络连接
- 不 import ProviderAdapter / SecretStore——保持模块依赖最小

P1-E1 只注册当前明确支持验证的内置 provider：
- Anthropic：strategy=anthropic_models（GET /v1/models）
- GLM：strategy=unsupported（远端验证暂未批准）

`openai_compatible` 和 `custom` 留待 P1-E2 ProviderProfile 引入.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

__all__ = [
    "ProviderAPIStyle",
    "ProviderHintConfidence",
    "CredentialValidationStrategyId",
    "ProviderDefinition",
    "ProviderHintResult",
    "ProviderRegistry",
    "detect_provider_hint",
    "get_provider_definition",
    "list_provider_definitions",
]


# ============================================================================
# Types
# ============================================================================


ProviderAPIStyle = Literal[
    "anthropic_compatible",
    "openai_compatible",
]

ProviderHintConfidence = Literal["high", "medium", "low", "unknown"]

# 凭证远端验证策略 ID——具体策略名而非泛化名（不同 provider 认证 Header /
# 版本 Header / 响应结构不同，不能假设一个通用 Models 验证器适用所有 provider）.
CredentialValidationStrategyId = Literal[
    "anthropic_models",   # Anthropic Models API（GET /v1/models with x-api-key）
    "unsupported",        # 暂无安全的远端验证策略——Service 层应直接返回
                           # validation_not_supported，不发任何网络请求
]


# ============================================================================
# Dataclasses
# ============================================================================


@dataclass(frozen=True)
class ProviderDefinition:
    """Built-in provider metadata.

    不含 user secret——仅描述 provider 协议特征与固定 validation endpoint。
    """

    id: str
    display_name: str
    api_style: ProviderAPIStyle
    default_base_url: str

    credential_validation_strategy: CredentialValidationStrategyId
    credential_validation_endpoint: str | None      # None 当且仅当 strategy="unsupported"

    supports_model_listing: bool
    key_prefix_hints: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ProviderHintResult:
    """detect_provider_hint() 返回——纯本地匹配结果.

    `candidates` 是 provider id 列表（可能 0/1/2+ 项）。
    `confidence` 描述匹配置信度（不是 provider 确定度）。

    前端（P1-E4）必须用"可能属于 X"措辞——**绝不**写"已识别为 X"。
    """

    candidates: tuple[str, ...]
    confidence: ProviderHintConfidence
    reason_code: str


# ============================================================================
# Internal URL validators（必须先于 _DEFAULT_REGISTRY 定义）
# ============================================================================


# 禁止 URL userinfo——`user:pass@host` 会泄漏凭据
_URL_HAS_USERINFO = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/@]*@")
# HTTPS scheme 校验
_HTTPS_SCHEME = "https"


def _validate_https_url(url: str, label: str) -> None:
    """Ensure URL is HTTPS, has a host, and does not contain userinfo."""
    if not url:
        raise ValueError(f"{label} must be non-empty")

    parsed = urlparse(url)
    if parsed.scheme != _HTTPS_SCHEME:
        raise ValueError(
            f"{label} must be HTTPS (got scheme={parsed.scheme!r})",
        )
    if not parsed.netloc:
        raise ValueError(f"{label} must have a host")
    if _URL_HAS_USERINFO.match(url):
        raise ValueError(
            f"{label} must not contain userinfo (user:pass@)——would leak credentials",
        )


def _validate_definition(d: ProviderDefinition) -> None:
    """Validate a ProviderDefinition at registry construction time.

    Strategy ↔ endpoint 一致性：
        - strategy="unsupported" → endpoint 必须为 None
        - 其它 strategy → endpoint 必须为 HTTPS、无 userinfo
    """
    if not d.id or not d.id.strip():
        raise ValueError("provider id must be non-empty")
    if not d.display_name:
        raise ValueError(f"provider {d.id}: display_name must be non-empty")

    _validate_https_url(d.default_base_url, f"provider {d.id}: default_base_url")

    if d.credential_validation_strategy == "unsupported":
        if d.credential_validation_endpoint is not None:
            raise ValueError(
                f"provider {d.id}: credential_validation_strategy='unsupported' "
                f"requires credential_validation_endpoint=None"
            )
    else:
        if d.credential_validation_endpoint is None:
            raise ValueError(
                f"provider {d.id}: credential_validation_strategy="
                f"{d.credential_validation_strategy!r} requires an HTTPS endpoint"
            )
        _validate_https_url(
            d.credential_validation_endpoint,
            f"provider {d.id}: credential_validation_endpoint",
        )


# ============================================================================
# Registry
# ============================================================================


class ProviderRegistry:
    """Frozen registry of ProviderDefinitions.

    提供查 / 列 / 存在性判断——不创建网络连接，不持有 secret。
    """

    def __init__(self, definitions: tuple[ProviderDefinition, ...]) -> None:
        # 校验：ID 唯一 / endpoint HTTPS / 无 userinfo
        seen_ids: set[str] = set()
        for d in definitions:
            _validate_definition(d)
            if d.id in seen_ids:
                raise ValueError(f"duplicate provider id: {d.id}")
            seen_ids.add(d.id)
        self._definitions: dict[str, ProviderDefinition] = {d.id: d for d in definitions}
        self._tuple: tuple[ProviderDefinition, ...] = definitions

    def get(self, provider_id: str) -> ProviderDefinition | None:
        """Return the definition for provider_id, or None if unknown."""
        return self._definitions.get(provider_id)

    def list(self) -> tuple[ProviderDefinition, ...]:
        """Return all registered definitions (stable order)."""
        return self._tuple

    def has(self, provider_id: str) -> bool:
        """Return True if provider_id is registered."""
        return provider_id in self._definitions


# ============================================================================
# Built-in Provider Definitions
# ============================================================================


_GLM_DEFINITION = ProviderDefinition(
    id="glm",
    display_name="Zhipu GLM (Anthropic-compatible)",
    api_style="anthropic_compatible",
    default_base_url="https://open.bigmodel.cn/api/anthropic",
    # GLM 远端验证 DEFERRED——公开 API 索引中未文档化无推理 / 无费用的统一
    # 模型列表 endpoint. 不要用 Messages endpoint 做 probe.
    credential_validation_strategy="unsupported",
    credential_validation_endpoint=None,
    supports_model_listing=False,
    # GLM keys 不带确定性前缀——避免误报
    key_prefix_hints=(),
)

_ANTHROPIC_DEFINITION = ProviderDefinition(
    id="anthropic",
    display_name="Anthropic",
    api_style="anthropic_compatible",
    default_base_url="https://api.anthropic.com",
    # 用 Models API 验证（无推理 / 无费用）：GET /v1/models with x-api-key
    credential_validation_strategy="anthropic_models",
    credential_validation_endpoint="https://api.anthropic.com/v1/models",
    supports_model_listing=True,
    key_prefix_hints=("sk-ant-",),
)


# ============================================================================
# Built-in registry singleton + module-level accessors
# ============================================================================


_DEFAULT_REGISTRY: ProviderRegistry = ProviderRegistry(
    (_GLM_DEFINITION, _ANTHROPIC_DEFINITION),
)


def get_provider_definition(provider_id: str) -> ProviderDefinition | None:
    """Module-level accessor for the default registry."""
    return _DEFAULT_REGISTRY.get(provider_id)


def list_provider_definitions() -> tuple[ProviderDefinition, ...]:
    """Module-level accessor for the default registry."""
    return _DEFAULT_REGISTRY.list()


# ============================================================================
# detect_provider_hint——pure local function
# ============================================================================

# 用于本地的 hint 规则——按"明显程度"排序
# 每条规则：prefix + provider_id + confidence
_HINT_RULES: tuple[tuple[str, str, ProviderHintConfidence], ...] = (
    # Anthropic keys: 高度确定——sk-ant- 唯一指向 Anthropic
    ("sk-ant-", "anthropic", "high"),
)


def detect_provider_hint(api_key: object) -> ProviderHintResult:
    """Detect possible provider from API Key format——pure local, no network.

    Rules:
    - `sk-ant-...` → candidates=("anthropic",), confidence="high"
    - 其它 `sk-...` 前缀但无法确定 → candidates=(), confidence="unknown"
    - 空或非字符串 → candidates=(), confidence="unknown"

    **关键安全约束**：纯本地——不调用 httpx / socket / urllib / DNS。
    测试可 monkeypatch 这些模块入口为直接抛异常，证明检测完全本地。
    """
    if not isinstance(api_key, str) or not api_key:
        return ProviderHintResult(
            candidates=(),
            confidence="unknown",
            reason_code="empty_or_invalid_input",
        )

    # 1. 高度确定的 provider-specific prefix
    for prefix, provider_id, confidence in _HINT_RULES:
        if api_key.startswith(prefix):
            return ProviderHintResult(
                candidates=(provider_id,),
                confidence=confidence,
                reason_code=f"prefix_match_{prefix.rstrip('-').replace('-', '_')}",
            )

    # 2. 通用 `sk-` 前缀——多 provider 都可能（GLM / Anthropic / OpenAI-compat）
    #    不把 OpenAI-compatible 当成已确认供应商——返回 unknown
    if api_key.startswith("sk-"):
        return ProviderHintResult(
            candidates=(),
            confidence="unknown",
            reason_code="ambiguous_sk_prefix",
        )

    # 3. 无法识别
    return ProviderHintResult(
        candidates=(),
        confidence="unknown",
        reason_code="no_hint_match",
    )
