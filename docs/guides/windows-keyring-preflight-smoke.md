# Windows Keyring 启动预检实机 Smoke

本文用于验证运行 Web 后端的**同一 Windows 用户、同一交互式登录会话和同一 Python
解释器**能够使用 Windows Credential Manager。Smoke 只写入随机生成的非用户值，
验证 write/read/delete 后立即清理；不需要、也不得使用真实 API Key。

## 验证目标

完成后必须同时满足：

1. 独立探针输出 `roundtrip_ok=true`、`cleanup_ok=true` 和 `probe_ok=true`。
2. 开发启动器在监听端口前输出 `credential_backend=keyring (write probe passed)`。
3. 探针不输出随机值，不读取现有 Credential，不修改任何真实 Credential 引用。
4. `PI_AGENT_SECRET_BACKEND=memory` 只能作为显式的非持久化降级，不能记为 Keyring
   smoke 通过。

产品启动器调用 `OSKeyringSecretStore.probe_write_access()`。仅发现
`WinVaultKeyring` backend 不够：非交互式进程或不可用的登录 token 可能在实际
`CredWrite` 时失败，所以必须执行真实但短生命周期的写入探针。

## 前置条件

- 从将来实际启动后端的 Windows 交互式登录账号运行 PowerShell。
- 使用后端实际使用的 Python 解释器；本仓库当前验证路径为
  `D:\miniconda\envs\pipy\python.exe`。
- 已安装项目依赖，尤其是 `keyring>=25`。
- 不要从 Windows Service、CI runner、SYSTEM、不同账号的计划任务或远程非交互式
  token 中运行，然后把结果当成交互式启动验证。

先确认解释器与 backend 类型。输出只包含版本和类名，不包含 Credential：

```powershell
Set-Location -LiteralPath "D:\LLMTutorial\test"
$keyringPython = "D:\miniconda\envs\pipy\python.exe"

& $keyringPython -c "import keyring,sys; b=keyring.get_keyring(); print('python=' + sys.executable); print('keyring_backend=' + type(b).__module__ + '.' + type(b).__qualname__)"
if ($LASTEXITCODE -ne 0) { throw "Keyring backend discovery failed" }
```

正常 Windows 环境通常会显示 Windows/WinVault 类型的 backend。`fail.Keyring`、
`null.Keyring` 或 import error 均不能继续视为通过。

## 1. 独立 write/read/delete/verify 探针

下面的单引号 here-string 不执行 PowerShell 变量插值。Python 在进程内生成唯一引用和
随机值；随机值从不输出。`finally` 始终尝试删除，删除后只判断再次读取是否为 `None`。

```powershell
Set-Location -LiteralPath "D:\LLMTutorial\test"
$keyringPython = "D:\miniconda\envs\pipy\python.exe"
$keyringProbe = @'
import hmac
import secrets
from uuid import uuid4

import keyring

SERVICE = "pi-agent-core-py"


def flag(value: bool) -> str:
    return str(value).lower()


def main() -> int:
    backend = keyring.get_keyring()
    backend_name = f"{type(backend).__module__}.{type(backend).__qualname__}"
    account = f"__pi_agent_keyring_manual_smoke__-{uuid4().hex}"
    value = secrets.token_urlsafe(32)
    roundtrip_ok = False
    cleanup_ok = False
    error_type = "none"

    try:
        keyring.set_password(SERVICE, account, value)
        persisted = keyring.get_password(SERVICE, account)
        roundtrip_ok = isinstance(persisted, str) and hmac.compare_digest(
            persisted,
            value,
        )
    except Exception as exc:
        error_type = type(exc).__name__
    finally:
        try:
            keyring.delete_password(SERVICE, account)
        except Exception as exc:
            if error_type == "none":
                error_type = type(exc).__name__
        try:
            cleanup_ok = keyring.get_password(SERVICE, account) is None
        except Exception as exc:
            cleanup_ok = False
            if error_type == "none":
                error_type = type(exc).__name__

    probe_ok = roundtrip_ok and cleanup_ok
    print(f"backend={backend_name}")
    print(f"roundtrip_ok={flag(roundtrip_ok)}")
    print(f"cleanup_ok={flag(cleanup_ok)}")
    print(f"probe_ok={flag(probe_ok)}")
    print(f"error_type={error_type}")
    if not cleanup_ok:
        # The reference is not secret; emit it only for manual cleanup.
        print(f"cleanup_required_service={SERVICE}")
        print(f"cleanup_required_account={account}")
    return 0 if probe_ok else 1


raise SystemExit(main())
'@

$keyringProbe | & $keyringPython -
if ($LASTEXITCODE -ne 0) { throw "Windows Keyring smoke failed" }
```

通过时的输出形状：

```text
backend=<Windows keyring backend class>
roundtrip_ok=true
cleanup_ok=true
probe_ok=true
error_type=none
```

不得把 `cleanup_ok=false` 忽略为成功。如果失败输出了 `cleanup_required_service` 和
`cleanup_required_account`，使用输出的两个非敏感标识补删；不要调用 `keyring get`，
因为它会把值写到终端：

```powershell
& $keyringPython -m keyring del "<cleanup_required_service>" "<cleanup_required_account>"
& $keyringPython -c "import keyring,sys; print('cleanup_ok=' + str(keyring.get_password(sys.argv[1], sys.argv[2]) is None).lower())" "<cleanup_required_service>" "<cleanup_required_account>"
```

第二条命令只输出布尔值，不输出读取到的内容。确认 `cleanup_ok=true` 后再继续排障。

## 2. 验证开发启动器的 listen-before-probe 顺序

选择一个未占用端口并显式要求持久化 Keyring：

```powershell
Set-Location -LiteralPath "D:\LLMTutorial\test"
$keyringPython = "D:\miniconda\envs\pipy\python.exe"
$env:PYTHONPATH = (Resolve-Path -LiteralPath "src").Path
$env:PI_AGENT_SECRET_BACKEND = "keyring"
$env:PORT = "8765"

& $keyringPython scripts/dev_web_app.py
```

成功时必须先看到：

```text
[dev] credential_backend=keyring (write probe passed)
```

随后才会出现 uvicorn 的监听日志。看到监听成功后按 `Ctrl+C` 正常停止，并清理本次
PowerShell 的覆盖变量：

```powershell
Remove-Item Env:PI_AGENT_SECRET_BACKEND -ErrorAction SilentlyContinue
Remove-Item Env:PORT -ErrorAction SilentlyContinue
```

失败时启动器应以非零状态退出，并包含以下安全摘要；此时不应有该进程创建的端口监听：

```text
[dev] startup refused: Persistent Keyring preflight failed.
```

不要为了让 smoke 变绿而把 backend 改成 `memory`。如果明确接受重启后 API Key 丢失，
`memory` 可以临时启动应用，但它验证的是显式降级路径，不是 Keyring 可用性。

## 3. 自动化伴随回归

实机 smoke 验证 Windows Credential Manager；以下测试用 fake backend 验证失败时拒绝
启动、成功时放行，以及 memory 模式不会偷偷访问 Keyring：

```powershell
Set-Location -LiteralPath "D:\LLMTutorial\test"
$keyringPython = "D:\miniconda\envs\pipy\python.exe"
$env:PYTHONPATH = (Resolve-Path -LiteralPath "src").Path
& $keyringPython -m pytest tests/test_keyring_startup_preflight.py -q --no-cov -W error
```

该自动化测试不能替代实机 write/read/delete。

## 故障诊断

| 现象 | 处理 |
|---|---|
| `ModuleNotFoundError: keyring` | 用同一解释器安装仓库依赖；不要改用另一个碰巧装有 keyring 的 Python |
| backend 为 `fail.Keyring` / `null.Keyring` | 检查依赖安装和 backend 选择；该结果不可记为通过 |
| backend 看似正常但 `roundtrip_ok=false` | 确认进程来自当前交互式 Windows 登录会话，而非 Service、SYSTEM、CI 或不同账号任务 |
| `cleanup_ok=false` | 立即按上面的 failure-only service/account 补删并做只输出布尔值的复核 |
| 独立探针通过但启动器拒绝 | 比较 Python 路径、Windows 账号/会话、`PYTHONPATH` 和 `PI_AGENT_SECRET_BACKEND`；必须在完全相同上下文复跑 |
| 只有 `memory` 能启动 | Keyring 问题仍未解决；API Key 只存在于当前进程，重启后按设计丢失 |

排障时可以运行 `python -m keyring diagnose`，但在分享输出前必须人工检查；不要收集
PowerShell 全量环境、Credential Manager 全量列表、`keyring get` 输出或任何 API Key。

## 验收记录模板

只记录非敏感事实：

```text
date=<YYYY-MM-DD>
windows_interactive_session=true
python=<absolute interpreter path>
keyring_backend=<backend class only>
roundtrip_ok=true
cleanup_ok=true
launcher_preflight_ok=true
launcher_listened_after_preflight=true
launcher_stopped_cleanly=true
```

不要记录随机探针值、真实 Credential 引用、Provider API Key、环境变量转储或终端完整
transcript。CI 通常没有可用的交互式 Windows Credential Manager，因此实机结果应作为
人工发布/部署 smoke 保存，而不是通过降低安全约束强行塞入离线 CI。
