"""Path sandbox——Step 18。

防止工具调用里的 path 参数逃逸出 workspace_roots。

提供两个函数：

- `is_path_within_roots(path, roots)`：path（resolve 后）是否在任一 root 内
- `extract_candidate_paths(arguments)`：从工具参数里把常见路径字段挑出来

兼容 Windows / POSIX：`Path.resolve()` 在各自平台都能得到绝对路径；
`Path.relative_to` 用来判定包含关系。`..` 会被 resolve 展平，无法逃逸。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

#: 工具参数里常见的路径字段名（小写比较）。
_PATH_FIELD_NAMES = {
    "path", "file_path", "filepath",
    "target_path", "source_path",
    "dest_path", "destination",
    "directory", "root",
}


def is_path_within_roots(path: str, roots: list[str]) -> bool:
    """path 是否在任一 root 内。

    判定方式：把 path 和 root 都 resolve 成绝对路径，再用 `relative_to`
    检查是否被包含；包含或相等都算 within。任何异常（不存在、权限等）
    返回 False——保守。

    roots 为空时返回 False（调用方据此决定如何处理）。
    """
    if not roots:
        return False
    try:
        target = Path(path).expanduser().resolve(strict=False)
    except Exception:
        return False
    for r in roots:
        try:
            root_resolved = Path(r).expanduser().resolve(strict=False)
        except Exception:
            continue
        if target == root_resolved:
            return True
        try:
            target.relative_to(root_resolved)
            return True
        except ValueError:
            continue
    return False


def extract_candidate_paths(arguments: Any) -> list[str]:
    """从工具参数里把疑似路径字段挑出来。

    支持 dict + 嵌套 dict（仅一层）。值非 str 的字段跳过。
    返回顺序按字段插入序；同一字段不重复。
    """
    out: list[str] = []
    seen: set[str] = set()

    def _consider(key: str, value: Any) -> None:
        if not isinstance(key, str) or not isinstance(value, str):
            return
        if key.lower() not in _PATH_FIELD_NAMES:
            return
        if not value:
            return
        if value in seen:
            return
        seen.add(value)
        out.append(value)

    if isinstance(arguments, dict):
        for k, v in arguments.items():
            if isinstance(v, dict):
                for kk, vv in v.items():
                    _consider(kk, vv)
            else:
                _consider(k, v)
    return out


__all__ = ["is_path_within_roots", "extract_candidate_paths"]
