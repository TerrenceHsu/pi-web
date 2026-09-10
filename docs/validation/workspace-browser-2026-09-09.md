# Workspace 多标签与本机浏览器验证

日期：2026-09-09。仅记录本轮分批定向验收，不与历史测试数量累加，不宣称全仓库/远端 CI 通过。

## 实现范围

- Markdown/网页统一标签栏：当前标签、相邻关闭、独立 ×、新网页 ＋、键盘切换、横向滚动；New Markdown 只保留分隔线下入口。
- 单栏、左右、上下分屏；文件预览实例与未保存草稿按 Session 保留；关闭标签不是删除文件，保存保留 SHA 冲突检查。
- 可选 Playwright Python + 独立 Chromium，不是 iframe；每 Session 临时 BrowserContext，网页登录、键盘/IME、鼠标/滚动、弹出 Page。
- 用户可交互命令白名单；无 evaluate/CDP/Cookie/文件/Agent API。输入、密码和截图不写业务 SQLite 或 Telemetry 正文。
- 公网 DNS 校验后固定 IP 代理；保留 Chromium sandbox；私网、loopback、非常规端口阻断。
- 上下文/页面/请求限额、15 分钟闲置回收、关最后网页/删 Session/应用关闭清理；清理失败不报告 Session 删除成功。

## 最终分批证据

| 批次 | 结果 | 范围 |
|---|---|---|
| 前端 Vitest | 38 files / 284 passed | 标签去重/隔离/相邻关闭/键盘、草稿、既有 UI 回归 |
| 后端专项 | 73 passed / 3 skipped | `test_web_browser.py` 与 Web/Workspace/Provider/执行 Session 清理相关 4 个模块；3 项既有显式实机集成未启用 |
| 真实 Python Chromium | 2 passed | 独立临时上下文、合成中文输入/登录、popup、隔离、删除/线程/代理回收；真实 TLS 访问 `https://example.com` |
| Chromium Web E2E | 8 passed，零重试 | Session 删除、Markdown 草稿/保存/重开、多标签/分屏/登录/弹出页、文件上传与窄屏 |
| 静态与构建 | Ruff、ESLint、Windows/Linux strict Mypy 各 313 files、Vue 类型检查与生产构建通过 | 新代码与当前工作树；并非新增远端 CI 结果 |

E2E 使用独立临时数据库、Fake LLM 和固定测试网页，密码是虚构输入；公网测试未使用真实账号。
真实浏览器测试需 `PI_RUN_INTEGRATION=1`、`PI_RUN_LOCAL_BROWSER_TESTS=1`、
`PI_BROWSER_PUBLIC_SMOKE=1`，并显式 `pytest -m integration --no-cov`；普通默认测试不会访问公网。
Python 定向回归使用 `--no-cov`，不把子集覆盖率当作全仓库覆盖率门禁。
Web 浏览器 E2E 由 `PI_E2E_BROWSER=1` 显式启用，测试专用路由 fixture 不进入生产启动配置。

迭代中修复：保存自身 SHA 更新竞态、隐藏标签定位、分屏缩放后旧帧点击、
跨 Session 启动配额竞态、空闲回收锁内复查、关闭/创建时迟到列表回填与重复 Markdown 测试入口。
受限环境最初无法启动测试 Chromium（EPERM），在批准的独立测试进程中复验；未关闭产品 Chromium sandbox。

## 未验收或刻意限制

- 任意第三方真实账号登录、验证码、反自动化、DRM/流畅视频、Passkeys、浏览器扩展不能由上述 smoke 推断可用。
- 仅公网 HTTP 80 / HTTPS 443；无浏览器上传/下载、内网服务、系统剪贴板读取或日常浏览器账号导入。
- Markdown 草稿仅内存、刷新需先保存；浏览器登录在关最后网页、后端重启或闲置回收后失效。
- 未改真实 Provider/业务数据，未开放 Web 服务到公网，未提交或推送 Git。

## 本机加载

- 已核验并仅重启原后端 PID 16908，新 PID 32308；沿用原数据目录、Keyring 与 Sandbox 配置。
- 日志确认 Keyring write probe passed、Application startup complete；生产前端构建已恢复。
- 5173 页面返回 200；8000 Session API 与 5173 代理后的 browser API 对未登录请求均返回 401。
- 前端进程继续运行；用户刷新后若旧登录失效，需重新登录。此处未代替用户执行真实账号登录。
