# P1-D1 Export Markdown Validation Report

> **阶段**: P1-D1（Export Markdown）
> **Tag**: `v0.0.26-export-markdown`
> **Commit**: `ebbc896`
> **完成日期**: 2026-07-14
> **状态**: ✅ FROZEN

## 1. 范围

把 SQLite 持久化的 session messages 渲染为 Markdown 文件并提供浏览器下载。

**核心链路**：SQLite messages → 按 idx ASC → 只映射 user/assistant → canonical Markdown renderer → 大小检查 → 安全 Content-Disposition → 浏览器 Blob 下载。

## 2. 实现内容

### 新增

- `src/pi_agent_core_py/web/markdown_export.py`——纯函数 renderer
  - `ExportMessage` dataclass
  - `render_session_markdown(title, messages, *, max_chars, max_bytes)`
  - `_sanitize_filename`——path traversal / 控制字符 / 80 字符 / RFC 5987 UTF-8
  - `MAX_EXPORT_CHARS = 2_000_000` / `MAX_EXPORT_BYTES = 5_000_000`
- `src/pi_agent_core_py/web/app.py` +92 行：`GET /api/sessions/{sid}/export/markdown`
- 前端 `api/client.ts` +80 行：`requestBlob` + `downloadBlob`（try/finally + `setTimeout(0)` 释放 Blob URL）
- 前端 `api/sessions.ts` +13 行：`exportMarkdown(sid)`
- 前端 `SessionSidebar.vue` +19 行：Export 按钮

### 修改

无 core runtime 改动；所有逻辑在 Web 层 + 持久化层。

## 3. 三道安全边界

### 边界 1：Blob URL 释放

**审核推荐**：`downloadBlob` 用 `try/finally` 包裹 `URL.createObjectURL` / `URL.revokeObjectURL`。

**额外加固**：`setTimeout(0)` 延迟 revoke——Safari 在同步上下文中 revoke 会让下载中断。

```typescript
async function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  try {
    // trigger download
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }
}
```

### 边界 2：Content-Disposition CRLF 阻断

**问题**：filename 中嵌入 CRLF 可让攻击者注入额外 HTTP header。

**修复**：`_sanitize_filename` 正则阻断 `\x00-\x1f`（含 CR/LF）。

**验证**：`test_21_crlf_injection_blocked` 显式证明。

### 边界 3：MCP env value 不导出

**实现**：
- 按 `role` 过滤——只导出 `user` / `assistant`
- **不读** `web_mcp_servers` 表
- 不扫描用户正文中的 secret 字符串（用户正文原样导出）
- system prompt / request_id / toolResult / MCP env 一律不导出

## 4. 测试覆盖

### 专项测试 `tests/test_web_markdown_export.py`——22 用例

| 类别 | 用例 |
|---|---|
| 基本导出 | 4 |
| Filename 安全 | 6（path traversal / 控制字符 / 超长 / 非 ASCII / RFC 5987） |
| 大小限制 | 3（chars / bytes / 413） |
| 边界 | 5（空 session / 404 双路径 / active draft 不导出） |
| 审核 4 补测 | 4（空 session / 404 双路径 / CRLF 阻断 / active draft 不导出） |

## 5. 测试基线（HEAD `ebbc896`）

| 命令 | 结果 |
|---|---|
| `pytest tests/ -m "not slow and not integration and not docker"` | **990 passed**（986 baseline + 4 新增） |
| Coverage gate | **90.38%** ≥ 75% PASS |
| `ruff check src tests scripts` | All checks passed |
| Frontend production build (e2e mode) | 137.53 KB JS / 39.80 KB CSS |
| Playwright e2e | **26/28**——两个失败是 P1-C 持久化引入的跨测试状态污染（stash + 重测证明非 D1 引入回归） |

## 6. 已知限制

- **`POST /api/prompt` 同步阻塞**——慢 LLM 会让 HTTP 请求挂住（D1 不修，留待 D2+）
- **MCP / Skill 配置跨重启恢复**——已在 P1-C 实现
- **PDF 正文不解析**——P1-D3 处理
- **不支持图片理解**（不做 OCR）
- **`view_file` 大文本自动截断到 max_bytes**（默认 8KB）

## 7. 显式不做

- ❌ 右栏调试面板 / Drawer
- ❌ CLI
- ❌ RAG / Vector Memory / Long-term Memory
- ❌ 多用户账号 / OAuth / RBAC
- ❌ 公网部署 / 横向扩展 / rate limit
- ❌ Regenerate（P1-D2 范围）

## 8. 相关文档

- [Persistence and Startup](../../architecture/persistence-and-startup.md)——SQLite schema + session_store
- [Release notes](../../releases/v0.0.26-export-markdown.md)
