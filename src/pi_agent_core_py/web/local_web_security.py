"""Localhost Web Security Config（P1-E1-4A）.

冻结 localhost 安全边界配置——所有字段在 App 创建时一次性确定：

- `allowed_hosts`         TrustedHostMiddleware 的 host 白名单（E1-4A 安装）
- `allowed_ui_origins`    E1-4B Origin 校验用的精确 origin（scheme://host:port）
- `require_ui_header`     E1-4B mutating endpoint 是否要求 `X-PI-Agent-UI`
- `max_request_body_bytes` E1-4B HTTP body 上限（默认 32 KiB）

**关键不变量**：

1. 生产默认 `allowed_hosts = ("localhost", "127.0.0.1", "::1")`——不含 `*`、不含 `testserver`
2. `testserver` 仅由测试通过 `extra_hosts` 显式注入——不放入生产默认
3. `allowed_ui_origins` 必须是精确 origin——禁止 `*` / `null`
4. `max_request_body_bytes` 默认 32 KiB——Credential API 全路由强制
5. Config 是 frozen dataclass——App 创建后不可变

E1-4A 只用到 `allowed_hosts`（TrustedHostMiddleware）. 其他字段在 E1-4B 引入对应
middleware / route dependency 时使用.
"""
from __future__ import annotations

from dataclasses import dataclass

from starlette.datastructures import URL, Headers
from starlette.types import Scope

__all__ = [
    "WebSecurityConfig",
    "DEFAULT_ALLOWED_HOSTS",
    "default_web_security_config",
    "websocket_origin_allowed",
]


# 生产默认 Host——localhost 三种形式（IPv4 / IPv6 / hostname）
# 严禁 "*" / "testserver"——后者仅测试用，通过 extra_hosts 显式注入
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")

# 默认 body 上限——Credential API 全路由强制（含 chunked / 伪造 Content-Length）
DEFAULT_MAX_REQUEST_BODY_BYTES: int = 32 * 1024


@dataclass(frozen=True)
class WebSecurityConfig:
    """Localhost security boundary config.

    Frozen at app creation; stored in CredentialRuntimeConfig.
    """

    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    allowed_ui_origins: tuple[str, ...] = ()
    require_ui_header: bool = True
    max_request_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES


def websocket_origin_allowed(scope: Scope, config: WebSecurityConfig) -> bool:
    """Allow exact same-origin UI/dev origins; retain non-browser clients.

    Browsers supply Origin during the handshake, including for same-site
    requests to another port. Host trust is enforced separately by middleware.
    """
    origin = Headers(scope=scope).get("origin")
    if origin is None:
        return True
    if origin == "null":
        return False
    url = URL(scope=scope)
    scheme = "https" if url.scheme in {"https", "wss"} else "http"
    same_origin = f"{scheme}://{url.netloc}"
    return origin == same_origin or origin in config.allowed_ui_origins


def default_web_security_config(
    *,
    extra_hosts: tuple[str, ...] = (),
    extra_ui_origins: tuple[str, ...] = (),
    require_ui_header: bool = True,
    max_request_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES,
) -> WebSecurityConfig:
    """Build default config with optional test/dev extras.

    Production: 不传任何 extra → 仅 localhost / 127.0.0.1 / ::1.
    Test: 传 `extra_hosts=("testserver",)` → TestClient 默认 Host 仍可用.
    Dev: 传 `extra_ui_origins=("http://localhost:5173",)` → Vite dev origin.

    `extra_hosts` 永远不在生产默认值中出现——调用方必须显式传.
    """
    # 不允许 "*" 通过 extra_hosts 注入——防御性拒绝
    if "*" in extra_hosts:
        raise ValueError(
            "wildcard '*' is forbidden in allowed_hosts "
            "(use explicit origins / hosts)"
        )
    return WebSecurityConfig(
        allowed_hosts=DEFAULT_ALLOWED_HOSTS + extra_hosts,
        allowed_ui_origins=extra_ui_origins,
        require_ui_header=require_ui_header,
        max_request_body_bytes=max_request_body_bytes,
    )
