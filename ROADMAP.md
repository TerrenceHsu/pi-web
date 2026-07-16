# Roadmap

> 未来阶段。当前状态见 [STATUS.md](STATUS.md)；已发布版本见 [CHANGELOG.md](CHANGELOG.md)。

## P1-D3 — PDF Text Extraction

**状态**：设计待批准（3 个边界冲突阻塞）

**范围**：
- 在 `view_file` 工具中支持 PDF 正文提取
- 不做 OCR / 图像理解
- 不做 PDF 表单 / 注释 / 嵌入对象

**待解决边界冲突**：
1. `tools/view_file.py` 修改边界——允许 PDF adapter，或保持 tools/ 不动把 adapter 放到 web 层
2. `asyncio.to_thread + wait_for` 是软超时——长 PDF 提取无法硬取消
3. PDF metadata（页数、提取字数、错误标记）存储位置——FileRef 是否需要加 metadata 字段
4. 失败文件清理策略——方案 A 整体拒绝 vs 方案 B 保存但标记

**预期验证**：
- 新增专项测试覆盖空 PDF / 加密 PDF / 超时 / 损坏 PDF
- 不回归现有 view_file（md / html / csv / parquet / 文本）
- coverage ≥ 75% 保持

## P1-D4 — E2E + Docs + Release

**状态**：等 D2/D3 完成

**范围**：
- 把 P1-D2 / D3 / D4 整合为可发布版本
- 打 tag `v0.0.27-product-actions`
- 完整 release notes + 验证报告

## P2 — Architecture Consolidation

**状态**：未启动

**候选主题**（独立设计、独立测试、独立冻结）：

### P2-A Web App 模块化
- `web/app.py` 当前 4400+ 行——按 route / service / repository / serializer 拆分
- startup restore 集中
- request lifecycle 集中
- file processing 独立模块
- 不破坏 API / response schema

### P2-B Session Routing / Full Reload Recovery
- URL-based session routing（`?sid=...` 或 `/sessions/[sid]`）
- reload 后恢复原 session
- 恢复 active Regenerate draft
- 列入 P2 是因为 D2 显式不做（依赖 URL routing）

### P2-C Context Budget / Compaction
- 上下文窗口估算
- 自动压缩策略
- 真 LLM 摘要器（当前 `default_summary_generator` 是规则式）

### P2-D Operational Hardening
- WebSocket event_id 去重 + reconnect replay 已支持；进一步增强
- request registry 持久化（server restart 不丢 active request）
- 多 session 并行执行（解除 single harness 限制）
- audit log / rate limit（如果未来需要远程访问）

## 明确不做（长期）

- OCR / Image understanding / 视觉理解
- RAG / Vector Memory / Long-term Memory
- Multi-Agent 编排
- CLI / RPC mode
- 多用户账号 / OAuth / RBAC / 企业 secret vault
- 公网部署 / 横向扩展
- MCP marketplace / Skill marketplace
- Skill 在线编辑 / 跨项目共享 / 热加载
- 本地文件系统操作工具（bash / read / write / edit / grep / find / ls）
