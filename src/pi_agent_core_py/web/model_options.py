"""Static model option constants + helpers（P1-E2-2）.

E2-2 的模型策略：**不调用任何远端模型目录 API**。所有建议都来自源代码常量；
用户始终可以手动填写任意合法 ``model_id``（在 Service 层规范化，不强制在静态列表内）.

**职责边界（E2-2）**：
- ✅ ``ModelCapabilities`` / ``ModelOption`` dataclass
- ✅ ``ANTHROPIC_MODEL_OPTIONS`` / ``GLM_MODEL_OPTIONS`` 静态常量
- ✅ ``get_static_model_options(provider_id)`` 查询
- ✅ ``normalize_model_id(value)`` 纯函数校验
- ❌ 不引入 HTTP client / Anthropic /v1/models 调用 / Secret 读取
- ❌ 不引入 ``remote`` / ``custom`` / ``cached`` / ``refreshed_at`` ModelOption 字段
- ❌ 不把静态列表当成白名单——``normalize_model_id`` 只校验**形态**

**为何 Anthropic 默认空 tuple**：
项目仓库未保存可信的 Anthropic 模型 ID 基线（``.env`` 实际指向 GLM via
Anthropic-compat）。按 design doc「不自行扩充一长串模型」原则，Anthropic
建议集合默认空——用户手动填写 ``model_id`` 始终可用。

**为何 GLM 有 3 个建议**：
``providers/glm.py`` 已经把 ``glm-4.5-flash`` 作为 ``_DEFAULT_GLM_MODEL``
冻结。本模块补两个 GLM 公开稳定模型（``glm-4.5`` / ``glm-4``）作为最小建议
集合，不引入灰度 / Beta / 已下线模型.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# ============================================================================
# Dataclasses
# ============================================================================


@dataclass(frozen=True)
class ModelCapabilities:
    """Capability hints. ``None`` = unknown (must NOT be coerced to ``False``)."""

    streaming: bool | None = None
    tool_calling: bool | None = None
    reasoning: bool | None = None
    vision: bool | None = None
    context_window: int | None = None


@dataclass(frozen=True)
class ModelOption:
    """A static model suggestion (E2 内 source 永远是 ``"static"``)."""

    id: str
    display_name: str | None
    source: Literal["static"]
    capabilities: ModelCapabilities


# ============================================================================
# Static catalogs
# ============================================================================


ANTHROPIC_MODEL_OPTIONS: tuple[ModelOption, ...] = ()
"""
Anthropic 静态建议——当前为空 tuple.

项目仓库未保存可信 Anthropic 模型 ID 基线（参见模块 docstring）.
用户始终可以通过 ``normalize_model_id`` 手动保存任意合法 ``model_id``.
"""


GLM_MODEL_OPTIONS: tuple[ModelOption, ...] = (
    ModelOption(
        id="glm-4.5-flash",
        display_name="GLM-4.5-Flash",
        source="static",
        capabilities=ModelCapabilities(
            streaming=True,
            tool_calling=True,
            reasoning=None,
            vision=None,
            context_window=None,
        ),
    ),
    ModelOption(
        id="glm-4.5",
        display_name="GLM-4.5",
        source="static",
        capabilities=ModelCapabilities(
            streaming=True,
            tool_calling=True,
            reasoning=None,
            vision=None,
            context_window=None,
        ),
    ),
    ModelOption(
        id="glm-4",
        display_name="GLM-4",
        source="static",
        capabilities=ModelCapabilities(
            streaming=True,
            tool_calling=True,
            reasoning=None,
            vision=None,
            context_window=None,
        ),
    ),
)


_BY_PROVIDER: dict[str, tuple[ModelOption, ...]] = {
    "anthropic": ANTHROPIC_MODEL_OPTIONS,
    "glm": GLM_MODEL_OPTIONS,
}


def get_static_model_options(
    provider_id: str,
) -> tuple[ModelOption, ...]:
    """Return static model suggestions for ``provider_id``.

    Unknown providers return empty tuple——``ProviderConfigService`` 在创建
    Profile 时已经校验 ``provider_id`` 是否注册；本函数只负责按 provider 查表.
    """
    if not isinstance(provider_id, str):
        return ()
    return _BY_PROVIDER.get(provider_id, ())


# ============================================================================
# model_id normalization
# ============================================================================


class InvalidModelIdError(ValueError):
    """Raised when ``model_id`` fails normalization.

    **Safety**: ``str(exc)`` must NOT echo the input value.
    """


_MAX_MODEL_ID_LEN = 256
_MIN_MODEL_ID_LEN = 1

# Reject any ASCII control char (< 0x20), CR (\r), LF (\n), DEL (0x7F), NUL.
# Apply same rule as E2-1 Store-level validator so Service-level rejects pre-DB.
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


def normalize_model_id(value: str) -> str:
    """Validate and trim ``model_id``.

    Rules:
        - must be ``str`` (reject bytes / None)
        - non-empty after ``strip()``
        - length 1–256 (after trim)
        - reject NUL / CR / LF / any ASCII control character (< 0x20 or 0x7F)
        - preserve case and punctuation ``/`` ``-`` ``_`` ``.`` ``:``

    Returns:
        Trimmed ``model_id``.

    Raises:
        InvalidModelIdError: validation failed. ``str(exc)`` does **not**
            include the input value.
    """
    if not isinstance(value, str):
        raise InvalidModelIdError("model_id must be a string")
    trimmed = value.strip()
    if not trimmed:
        raise InvalidModelIdError("model_id must be non-empty after trim")
    if len(trimmed) > _MAX_MODEL_ID_LEN:
        raise InvalidModelIdError(
            f"model_id must be ≤ {_MAX_MODEL_ID_LEN} chars after trim"
        )
    if _CONTROL_CHAR_PATTERN.search(trimmed):
        raise InvalidModelIdError(
            "model_id must not contain control characters or newlines"
        )
    return trimmed


__all__ = [
    # Dataclasses
    "ModelCapabilities",
    "ModelOption",
    # Static catalogs
    "ANTHROPIC_MODEL_OPTIONS",
    "GLM_MODEL_OPTIONS",
    # Query
    "get_static_model_options",
    # Normalization
    "InvalidModelIdError",
    "normalize_model_id",
]
