"""Secret 工具函数（P1-E1-1）。

`mask_secret`：用户可见的脱敏——"sk****5678"。
`fingerprint_secret`：内部去重 SHA-256——绝不返回前端、不进 serializer、不进日志。
"""
from __future__ import annotations

import hashlib
import re

__all__ = ["mask_secret", "fingerprint_secret"]


# 安全阈值——总长度不足此值则全部遮蔽
_MIN_MASKABLE_LENGTH = 8

# 前缀保留字符数——显示用户识别但不泄漏完整 Key
_PREFIX_KEEP = 3

# 后缀保留字符数
_SUFFIX_KEEP = 4

# SHA-256 fingerprint 前缀长度（hex；不含算法前缀）
_FINGERPRINT_HEX_LENGTH = 12

# fingerprint 完整格式：sha256:{12 hex}
_FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{12}$")


def mask_secret(secret: str) -> str:
    """把 API Key 脱敏为用户可识别但不可还原的形式。

    规则：
    - 总长度 < 8：返回 "********"（全遮蔽）
    - 否则：保留前 3 + 末 4，中间用 "****" 替换
    - 空 / 纯空格：拒绝（ValueError）

    Examples:
        >>> mask_secret("sk-example-12345678")
        'sk-****5678'
        >>> mask_secret("abc")
        '********'
        >>> mask_secret("")
        Traceback (most recent call last):
            ...
        ValueError: secret must be non-empty and non-whitespace
    """
    if not secret or not secret.strip():
        raise ValueError("secret must be non-empty and non-whitespace")

    if len(secret) < _MIN_MASKABLE_LENGTH:
        return "********"

    prefix = secret[:_PREFIX_KEEP]
    suffix = secret[-_SUFFIX_KEEP:]
    return f"{prefix}****{suffix}"


def fingerprint_secret(secret: str) -> str:
    """返回 SHA-256(secret UTF-8) 的内部去重指纹。

    格式：`sha256:{12 hex chars}`（小写）

    **内部使用**——绝不返回前端、不进 API serializer、不进日志、不进 snapshot、
    不进 WS event、不进 export。仅用于：
    - 本地重复 Key 检查
    - 内部一致性测试
    - 安全测试断言

    Examples:
        >>> fingerprint_secret("sk-test-1234567890")
        'sha256:0e1f3e08d8df'
        >>> # 同一 secret 总是产生同一指纹
        >>> fingerprint_secret("sk-test-1234567890") == fingerprint_secret("sk-test-1234567890")
        True
        >>> # 不同 secret 产生不同指纹
        >>> fingerprint_secret("sk-test-1234567890") != fingerprint_secret("sk-test-9876543210")
        True
    """
    if not secret:
        raise ValueError("secret must be non-empty")

    h = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return f"sha256:{h[:_FINGERPRINT_HEX_LENGTH]}"


def is_valid_fingerprint(value: str) -> bool:
    """Check whether a string matches the internal fingerprint format.

    Internal helper——used by tests and SQLite repository to validate persisted values.
    """
    return bool(_FINGERPRINT_PATTERN.match(value))
