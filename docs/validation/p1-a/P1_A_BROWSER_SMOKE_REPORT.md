# P1-A1 Browser Smoke Report

> **阶段**: P1-A1 真实环境验证（手动浏览器 + API smoke）
> **基线 commit**: `dafb803`
> **Tag**: `v0.0.23-web-claude-p0-mvp`（@ `8817c84`）
> **执行日期**: 2026-07-12
> **环境**: Windows 11 Pro / conda `pipy` / Python 3.12 / uvicorn 127.0.0.1:8000 / FakeClient

## 1. 启动方式

```bash
# Terminal 1 — backend (FakeClient, 无 GLM 依赖)
PYTHONPATH=src /d/miniconda/envs/pipy/python.exe tests/e2e/start_test_web_app.py
# → [e2e] starting on http://127.0.0.1:8000 (db=temp, uploads=temp)

# Terminal 2 — frontend (已 build)
# Static assets served by FastAPI at http://127.0.0.1:8000/
```

> **注意**: 本阶段用 **FakeClient**（确定性 20 个 script），不消耗 GLM 额度；涉及真实 GLM 的 #8/#9/#10 留到 P1-A3。

## 2. 25 项 checklist 状态总览

| 状态 | 数量 | 说明 |
|---|---:|---|
| PASS（已在 P0 e2e 自动化）| 14 | #1, #2, #3, #5, #6, #7, #11, #12, #13, #14, #15, #20, #24, #25 |
| **PASS（P1-A1 API smoke）** | **3** | **#21, #22, #23 — 见 §3** |
| **PASS（P1-A2 Playwright）** | **5** | **#4, #16, #17, #18, #19 — 见 §4** |
| PENDING（P1-A3 真实 GLM）| 3 | #8, #9, #10 |
| **合计** | **25** | |

## 3. P1-A1 新增验证（本次执行）

### #21 并发 prompt → 409 "Agent is already running"

**前置**: server 已启动，default session = `sess-1783791494798-755863a0`。
**操作**: Python `threading.Barrier(2)` 严格同步并发 2 个 POST /api/prompt。
**预期**: 第一个 200，第二个 409。
**实际**:
  - request#0: HTTP 200 ✓
  - request#1: HTTP 409 `{"ok":false,"error":"RuntimeError: Harness is already running (phase=running)","error_type":"RuntimeError"}`
  - elapsed: 45.7ms
**状态**: **PASS** ✓

### #22 Skills modal 上传重名 SKILL.md → 409

**前置**: 已上传 `p1-a-dup-skill`。
**操作**: 再次上传同名 SKILL.md（filename=SKILL.md，frontmatter name=p1-a-dup-skill）。
**预期**: 409 + 错误描述。
**实际**:
  - First upload: HTTP 200 — `{"count":1,"skills":[{"name":"p1-a-dup-skill",...}],"errors":[]}`
  - Second upload: HTTP 409 — `{"count":0,"skills":[],"errors":[{"filename":"SKILL.md","skill_name":"p1-a-dup-skill","error_type":"SkillRegistrationError","error":"Skill 'p1-a-dup-skill' 已注册（重复的）","status":409}]}`
**状态**: **PASS** ✓

### #23 MCP modal Test 不存在的命令 → 502

**前置**: POST /api/mcp/servers 添加 `p1-a-broken-server`，command=`/definitely/not/installed/binary`，enabled=false。
**操作**: POST /api/mcp/servers/p1-a-broken-server/test。
**预期**: 502 + 错误描述。
**实际**:
  - Add server: HTTP 200 ✓
  - Test: HTTP 502 — `{"ok":false,"server":"p1-a-broken-server","error":"MCPConnectionError: StdioMCPTransport: command not found: /definitely/not/installed/binary"}`
  - Cleanup DELETE: HTTP 200 ✓
**状态**: **PASS** ✓

## 4. P1-A2 Playwright 自动化结果（5 项全 PASS）

**测试文件**: `tests/e2e/mcp-tool-lifecycle.spec.ts`
**前端改动**: `MCPServerList.vue` / `MCPToolList.vue` / `MCPServerForm.vue` 加 data-testid（零逻辑改动）
**全套 e2e**: **12/12 PASS**（2 新增 + 10 旧 smoke 不回归）

| ID | 名称 | 实际结果 | 状态 |
|---|---|---|---|
| #4 | Shift+Enter 换行 | textarea value 含 `\n` + 消息未发送 | **PASS** ✓ |
| #16 | Add server → 字段清空 | submit 后 name/command/args 三字段全部回到默认值（`""`/`""`/`"[]"`）| **PASS** ✓ |
| #17 | Test → "✓ N tools detected" | `[data-testid="mcp-server-test-result"]` 显示 `last test: 1 tool detected` | **PASS** ✓ |
| #18 | Enable server → badge `attached · N tools` | server status badge 变 `enabled`；attached badge 显示 `attached · 1 tool` | **PASS** ✓ |
| #19 | Disable 单个 tool → badge disabled | tool status badge 由 `enabled` 变 `disabled`；GET /api/mcp/tools 同步 `enabled=false` | **PASS** ✓ |

### 链路完整性断言

- ✓ Test connection 不自动 enable server
- ✓ Enable server 后 tools 真正出现在 GET /api/mcp/tools
- ✓ Disable tool 后 API enabled=false（不仅 UI 改）
- ✓ Enable tool 后状态恢复 enabled=true
- ✓ Disable server 后 tools 不再注册（mcp-tool-row count=0）
- ✓ Delete server 后 server-card 消失
- ✓ env value 是 type=password + 提交后清空（form reset）+ body innerText 不含 secret 模式

## 5. 待 P1-A3 真实 GLM 覆盖（3 项）

| ID | 名称 | 验证方式 |
|---|---|---|
| #8 | view_file → FileRead card | `tests/integration/test_real_glm_tool_use.py` |
| #9 | 上传图片 → 不支持回复 | A3 smoke（可选手动） |
| #10 | 上传 PDF → 未解析回复 | A3 smoke（可选手动） |

## 6. 安全检查

- [ ] MCP env value 不出现在任何 HTTP response body
- [ ] GLM_API_KEY 不出现在任何日志 / 报告 / 截图
- [ ] page.body.innerText 不含 secret

## 7. 结论

_待 A1 / A2 / A3 全部完成后回填_
