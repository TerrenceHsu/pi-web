"""Secret 工具函数测试（P1-E1-1）.

覆盖：
- mask_secret：长 key / 短 key / 空 / 纯空格
- fingerprint_secret：稳定 / 不同输入不同输出
- is_valid_fingerprint：格式校验
"""
from __future__ import annotations

import hashlib

import pytest

from pi_agent_core_py.secrets.utils import (
    fingerprint_secret,
    is_valid_fingerprint,
    mask_secret,
)

# ============================================================================
# mask_secret
# ============================================================================


class TestMaskSecret:
    def test_long_key_masked_correctly(self) -> None:
        masked = mask_secret("sk-example-12345678")
        # 前 3 + 末 4
        assert masked == "sk-****5678"

    def test_min_maskable_length_boundary(self) -> None:
        # 长度 = 8 是最小 maskable——保留前 3 + 末 4，中间 1 个字符被遮蔽
        # 但 prefix(3) + suffix(4) = 7，所以长度 8 时只有 1 个字符被遮蔽
        masked = mask_secret("12345678")
        assert masked == "123****5678"

    def test_short_key_fully_masked(self) -> None:
        assert mask_secret("abc") == "********"
        assert mask_secret("1234567") == "********"  # 长度 7 < 8

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError):
            mask_secret("")

    def test_whitespace_rejected(self) -> None:
        with pytest.raises(ValueError):
            mask_secret("   ")
        with pytest.raises(ValueError):
            mask_secret("\t\n")

    def test_mask_not_invertible(self) -> None:
        """masked value 不可还原原值——前 3 + 末 4 信息不足以重建中间."""
        secret = "sk-XXXXXXXXXXXXXXXXXXX-YYYY"
        masked = mask_secret(secret)
        # 中间部分应该被替换
        assert "X" not in masked.replace("XXX", "")  # 仅前 3 X 留下
        assert "YYYY" == masked[-4:]

    def test_mask_stable_for_same_input(self) -> None:
        s1 = mask_secret("sk-test-1234567890")
        s2 = mask_secret("sk-test-1234567890")
        assert s1 == s2

    def test_mask_at_min_boundary(self) -> None:
        """长度 = 8 时刚好触发 normal masking（不是全遮蔽）."""
        m = mask_secret("ABCDEFGH")
        assert m == "ABC****EFGH"


# ============================================================================
# fingerprint_secret
# ============================================================================


class TestFingerprintSecret:
    def test_stable_for_same_input(self) -> None:
        f1 = fingerprint_secret("sk-test-1234567890")
        f2 = fingerprint_secret("sk-test-1234567890")
        assert f1 == f2

    def test_different_input_different_output(self) -> None:
        f1 = fingerprint_secret("sk-test-1234567890")
        f2 = fingerprint_secret("sk-test-0987654321")
        assert f1 != f2

    def test_format_is_sha256_prefix(self) -> None:
        secret = "sk-test-1234567890"
        expected_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        expected = f"sha256:{expected_hash[:12]}"
        assert fingerprint_secret(secret) == expected

    def test_format_matches_pattern(self) -> None:
        f = fingerprint_secret("sk-test")
        assert is_valid_fingerprint(f)

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError):
            fingerprint_secret("")

    def test_length_is_fixed(self) -> None:
        f = fingerprint_secret("sk-test-1234567890")
        # "sha256:" (7 chars) + 12 hex = 19
        assert len(f) == 19

    def test_lowercase_hex(self) -> None:
        f = fingerprint_secret("sk-test-1234567890")
        hex_part = f.split(":", 1)[1]
        assert hex_part == hex_part.lower()


# ============================================================================
# is_valid_fingerprint
# ============================================================================


class TestIsValidFingerprint:
    @pytest.mark.parametrize(
        "value",
        [
            "sha256:0e1f3e08d8df",
            "sha256:abcdef012345",
            "sha256:000000000000",
        ],
    )
    def test_valid_format(self, value: str) -> None:
        assert is_valid_fingerprint(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "sha256:",
            "sha256:short",
            "sha256:UPPERCASE12",
            "sha256:0e1f3e08d8df0e1f3e08d8df",  # too long
            "0e1f3e08d8df",  # missing prefix
            "md5:0e1f3e08d8df",  # wrong algorithm
        ],
    )
    def test_invalid_format(self, value: str) -> None:
        assert is_valid_fingerprint(value) is False
