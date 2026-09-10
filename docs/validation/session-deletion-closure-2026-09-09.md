# Web Session 删除闭环验收（2026-09-09）

## 问题与范围

用户已在侧栏删除多余会话，但删除 Provider 仍提示 `Profile is referenced by session bindings`。
本机核对发现：仅剩 1 个 Session、4 条消息，却有 26 条模型绑定，其中 25 条引用已不存在的 Session
（22 条关联 zai、3 条关联 Qwen）。这些是孤儿绑定，不是 25 个仍存活的会话，无需迁移会话。

本轮修复侧栏“×”到后端的会话删除链路；没有删除当前唯一业务 Session，也没有删除 Provider 或凭证。

## 实现

- Provider Store 在 Web 初始化时安装持久化 SQLite 触发器：Session 删除与绑定删除同事务；
  拒绝为不存在的 Session 插入/更新绑定，并只修复 `NOT EXISTS sessions` 的历史孤儿绑定。
  不含 Session 表的独立 Provider Store 保持兼容。并发写入遇到会话已删除返回安全的 404。
- Workspace MCP、Skill、Tool 选择与扩展选择由同事务触发器删除；不删除全局配置。
- 先停止会话请求、确认执行资源清理，再移除分析结果、完整 Workspace 目录和运行时，最后删除 Session。
  已有 SQLite 外键继续级联消息、快照、Memory、上下文记录等会话所有数据。
- 沙箱清理未确认返回 `session_cleanup_pending`，不开始文件删除；文件/运行时清理失败返回
  `session_cleanup_failed`，保留 Session 行供重试。文件系统和 SQLite 之间不宣称具有原子回滚：
  失败时部分资源可能已删除，界面会明确告知。
- Workspace 删除检查目标归属、符号链接/reparse point 和特殊文件，清理隐藏元数据及嵌套目录。
  成功后阻止排队写入重新创建相同 Session 目录。分析记录删除不再受列表 20 条分页限制，
  文件删除失败保留分析记录以便重试。
- 清理会话的请求查询历史、事件重放缓冲与前端文件/预算缓存；阻断迟到响应重新填回已删除数据。
  侧栏等待服务端 `ok` 才移除项目；失败显示可重试提示，重复点击受保护，旧路由被替换。
- Chromium 实测额外发现 Provider DELETE 使用 `JSONResponse(None)`，在 204 响应中发送 `null`，
  引发 Uvicorn/h11 `Too much data for declared Content-Length` 并断开复用连接。
  已改为空 `Response(204)`，新增空正文断言与删除后的连续请求验收。

## 删除语义与保留边界

- 删除的是该聊天会话及其私有工作区、分析结果和绑定，不是删除整个账号。
- 共享 Provider、凭证、全局 MCP/Skill、Wiki 知识与按独立策略保留的执行审计不随会话误删。
  Wiki 的会话删除回调仍按原策略归档知识关联，不清空共享知识库。
- 最后一个 Session 删除后，原有产品策略会创建一个**新 ID 的空白 default**；不是恢复旧会话。
- 这是应用层删除，不承诺对 SQLite 空闲页/WAL、操作系统备份或外部服务日志进行物理安全擦除。

## 本机历史残留修复

在 Web 停止、取得与网关共用的安装维护锁后，调用生产 Store 的
`install_session_lifecycle_guards()`，仅处理已核对的 admin Workspace 数据库。
初次校验脚本误用 DTO 凭证字段名，在任何业务删除前中止；核对实际 `web_credentials.id` 后重跑成功。

| 核对项 | 清理前 | 清理后 |
|---|---:|---:|
| 孤儿 Provider 绑定 | 25 | 0 |
| 存活 Session | 1 | 1 |
| 消息 | 4 | 4 |
| Provider Profiles | 2 | 2 |
| 凭证 | 3 | 3 |
| 存活 Session 绑定 | 1 | 1 |

前后比对确认 Session、消息、Profile、凭证 ID 和存活绑定未变化；保留当前 Qwen 绑定。
此前只读文件盘点仅发现存活 Session 的上传目录，分析库为 0 条，未据此批量删除其他业务目录。
删除的 25 条过期绑定不提供 UI 恢复入口；共享配置及存活会话无需恢复。

## 验证结果

以下是独立批次，存在重叠，不相加，也不冒充本轮全量后端门禁。

- 最终删除专项四文件：**50 passed / 3 skipped**，临时目录 `.t/session-delete-release`。
  跳过项为当前 Windows 无权限创建真实符号链接；模拟 reparse 防护用例通过。
- Provider API 与绑定删除回归：**34 passed**，包含 204 空正文检查，目录 `.t/session-delete-204`。
- 前序相关回归：核心/文件/Memory/投影 **88 passed**；扩展/迁移/生命周期 **111 passed**；
  执行运行时/维护/生命周期 **69 passed / 3 Docker deselected**。这些不是本轮真实 Docker 验收。
- 前端全套 **37 files / 277 passed**；ESLint、类型检查及生产构建通过。
- Ruff `src tests` 通过；strict Mypy `src evals` 在 win32/linux 均 **309 files** 通过。
- 新增 Chromium `session-deletion.spec.ts`：**1 passed，零重试**，端口 8185、全新临时账户。
  覆盖真实侧栏“×”、刷新不恢复、旧 Session/文件 404、删除已释放的 Provider、保留其他会话及共享凭证。
  不向业务 8000/5173 发删除请求，不发模型请求。
  前次沙箱运行受 Chromium `spawn EPERM` 阻断；交互用户重跑发现并复现 204 协议错误；修复后通过。
- 隔离验收成功后已自动恢复 production bundle。截图保留于本机忽略目录：
  `tests/e2e/test-results/session-deletion-sidebar-X-faa7c-s-only-its-provider-binding-chromium/session-deletion-complete.png`。
- `git diff --check` 通过；本轮没有 commit、push 或运行真实模型连接验收。

## 加载结果

前后端此前已停止；本轮恢复后端 PID 16908（8000）与前端 PID 28928（5173），仅监听 127.0.0.1。
Keyring 写入探针与应用启动通过；未认证 `/api/sessions` 返回 401，前端首页 200。
隔离测试端口 8179/8181/8183/8185 均已退出。未更换业务解析容器或放宽 Bash/发布审批。

日志：`.run-logs/backend-20260909-192326-447889cf/`、
`.run-logs/frontend-20260909-192427-e1a30ef0/`。用户需刷新并重新登录；未读取登录 Cookie 或代填密码。
