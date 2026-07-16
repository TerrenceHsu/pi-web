# Current TODO

> Current status: [STATUS.md](STATUS.md)
> Future roadmap: [ROADMAP.md](ROADMAP.md)
> Released versions: [CHANGELOG.md](CHANGELOG.md)

## Current phase

P1-D3 — PDF Text Extraction Design（**阻塞中**，待 4 个边界冲突解决）

## Blocking decisions

- [ ] PDF tool-layer boundary（`tools/view_file.py` 修改 vs web 层 adapter）
- [ ] Soft-timeout and concurrency model（`asyncio.to_thread + wait_for` 不是硬取消）
- [ ] File processing metadata storage（FileRef 加字段 vs 旁路表）
- [ ] Failed-upload cleanup strategy（方案 A 整体拒绝 vs 方案 B 保存但标记）

## Next

- [ ] Resolve 4 blocking decisions（设计批准）
- [ ] Implement P1-D3 after design approval
- [ ] Run D3 validation（新增专项测试 + 不回归现有 view_file）
- [ ] Complete P1-D4 release validation + tag `v0.0.27-product-actions`

## Explicitly out of scope

- OCR / Image understanding
- RAG / Vector Memory
- Multi-Agent
- Public deployment
- CLI（仅 Web UI）
- 本地文件系统操作工具
