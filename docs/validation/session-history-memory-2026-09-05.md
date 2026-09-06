# Session History / Structured Memory 阶段 1–4 验证

日期：2026-09-05。范围是用户批准的只读会话历史、检索隔离与限额、每轮结构化增量记忆、
来源追溯和用户纠正。没有修改压缩模块、阈值、摘要器或完整轮次保留策略。

## 实现与回归范围

- Writer 事务建立/回填正文投影和 FTS；工具用固定路径的 `mode=ro`、`query_only=ON` 连接，
  SQL 内绑定当前 Session。模型不能指定 SQL、账号、数据库路径或其它 Session。
- 验证中文短词/三字符检索、分页、跨 Session 拒绝、原文无 thinking/附件/工具参数、常见凭证脱敏、
  活动/归档分支标记、取消、缺失数据库不建库、查询不建表、初始化失败回滚和删除后的索引一致性。
- Memory 验证增量 JSON/schema、不可伪造的来源 ID、真实请求 ID、no-change 文件/revision 不变、
  旧笔记保留、手工编辑固定、更正来源、执行成果来源要求、容量失败保留旧文件、旧分支待确认，
  以及 Web Agent 工具调用、自动记忆和重启后的 no-op journal 恢复。
- 前端验证刷新遗漏的工具结果/详情恢复、原始顺序、重复同步幂等；扩展配置延迟不阻塞活跃请求恢复。

设计与使用说明：[`session-history-memory.md`](../design/session-history-memory.md)。

## 门禁

Python 使用 `D:\miniconda\envs\pipy\python.exe`，设置 `PYTHONPATH=src`。

| 门禁 | 结果 |
|---|---|
| 后端完整离线套件 | **2243 passed、7 skipped、9 deselected**；540.83 秒 |
| Coverage | **76.59% ≥ 75%**；未为新增实现降低门槛或添加排除 |
| 最终历史/记忆 + Checkpointer + SQLite 专项 | **36 passed**；11.52 秒；独立 `--basetemp=.test-tmp/history-final --no-cov` |
| Ruff | `ruff check src tests scripts evals`：**PASS** |
| strict Mypy | `mypy src evals`：**277 source files PASS** |
| 前端 Vitest | **202/202 passed，30 files**；6.94 秒 |
| 前端静态门禁 | ESLint / `vue-tsc --noEmit`：**PASS** |
| 最终完整浏览器验收 | **24/24 passed，1.4 分钟，11 个规格，禁用重试** |
| 生产构建 | 完整 E2E 的 posttest 执行 `vite build`：**PASS**；已恢复生产资源 |
| Evals | **5 suites / 10 observations，candidate gate PASS**；`.eval/history-memory-2026-09-05/` |
| 依赖锁一致性 | `uv lock --check --offline`：**PASS，115 packages** |

完整后端通过后，最后收紧了一个证据角色过滤条件：summary/custom 不得作为新增记忆的原始来源；
同一既有测试增加断言，最终定向 36 项及全量 Ruff/Mypy 均再次通过。此后生产代码的修改仅为前端恢复，
并复跑了全部前端测试和浏览器验收；未把局部复验表述成第二次完整后端运行。

## 浏览器验收过程

使用真实 Chromium、单 worker、独立本机测试后端，`CI=1`、`E2E_PORT=8059`；Provider 为 Fake。

1. 首轮完整浏览器退出成功，但 Python 批准场景重试一次才通过，不能作为稳定验收结论。
2. 对批准/拒绝重复三次且禁用重试：批准 **3 次失败**、拒绝 **3 次通过**。确认后端已完成计算，
   但终态同步只补 user/assistant，不补刷新遗漏的 tool result；修复通用恢复逻辑并增加两项前端回归。
3. 修复后批准/拒绝重复测试通过。随后完整禁用重试验收为 **21 passed、1 failed、2 did not run**：
   Regenerate 刷新测试期待 running，但扩展配置 GET 等待 Session 资源锁约 2.37 秒，期间模拟生成已结束。
   前端改为独立加载扩展；既有浏览器测试显式暂缓扩展请求，要求其返回前已恢复 running 和 Stop 按钮。
4. 下一次完整验收为 **23 passed、1 failed**：新增同步逻辑将 tool_call 换成 tool_result 组件，
   正在保存结果时重建了组件，导致保存提示丢失。修复为原位更新并保留卡片类型、ID 和参数；
   后端返回的完整正文在无专用 details 时仍可查看。两项既有新增回归同时验证重复同步不改变卡片身份。
5. 最终以 `npm --prefix tests/e2e run test:e2e -- --retries=0` 再跑完整套件：
   **24/24 PASS，1.4 分钟，0 retry / 0 flaky / 0 failure**；覆盖上述三个失败场景，posttest 生产构建 PASS。

首次失败产物保留于 `.test-tmp/history-memory-e2e-initial/`，不以跳过断言或启用重试掩盖失败。

## 环境与边界

- 一次早期全量后端与定向 pytest 共用了 `.pytest-tmp`，互相干扰临时目录；主动停止该次全量，
  随后串行重跑完整后端成功。最终定向测试使用独立 basetemp，不再竞争目录。
- 没有调用真实模型、外部 MCP/E2B 或 MinerU OCI，不以离线门禁代表模型提炼质量、任意秘密脱敏或 OS 沙箱安全。
- 16,000 字符是 Memory 文件安全上限，不是新压缩策略；达到上限会保留旧记忆并报错，不自动淘汰内容。
- 压缩前来源暂与旧分支一样标记待确认；更精细覆盖关系留给后续压缩方案。模型语义正确性仍需人工复核。
- 未重启用户开发后端，未提交或覆盖此前未提交工作；加载默认历史工具和索引初始化需要重启本机 Web 后端。
