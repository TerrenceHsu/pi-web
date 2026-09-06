# Workspace Bash 阶段 3 验证

日期：2026-09-06。范围：本机 Web Agent 的安全制品与单独确认发布。基线提交 `79d273d`；随本次提交归档、未推送。

## 实现

- 独立 Bash：专用 manifest `pi-agent-bash-artifact/v1` 和证据 `bash-output-integrity/v1`，由 operation 内实际成功结果生成，绑定任务/脚本/cwd/基线/策略/最终树，不伪造测试通过。
- Coding/Plan：保留固定验证的 Coding v1 制品；任务内 Bash 不改变证据类型。Plan 在切换待审阅前关闭许可，修复 watchdog 收尾误撤销竞态。
- 受限导出与主机复验：共用固定 helper、文件清单/diff、tar 校验和签名；拒绝保护路径、链接/特殊文件、稀疏 tar、大小写重复、绝对/穿越/反斜线/ADS/Windows 保留名及配额超限。
- 确认与事务：绑定 artifact ID、archive SHA、签名 review receipt；目标锁内再次验证制品、purpose/policy 和完整基线。任一冲突整批零写入，不强制覆盖/自动 rebase。
- 回收与恢复：资源回收未确认时不开放发布；私有签名胶囊只恢复已回收资源的审阅制品，不恢复 runtime/grant。持久 Workspace 提交回执处理“提交成功、生命周期响应丢失”的重复确认。
- 前端：Changes/审批条/控制面板区分完整性与功能验证；Bash 私有历史指出制品位置，刷新只读。

## 验证记录

以下批次相互重叠，不能累加成全量测试数字：

| 检查 | 结果 |
| --- | --- |
| 后端相关回归，13 个测试文件，`--no-cov --basetemp .test-tmp/c3f` | 243 passed / 12 skipped，154.43s |
| 收尾资源回收/恢复屏障，`.test-tmp/c3g` | 91 passed / 11 skipped，131.68s |
| 最后撤销/回收失败不可发布补测，`.test-tmp/c3h` | 41 passed / 3 skipped，75.22s；专用阶段 3 测试文件现有 30 项 |
| Frontend Vitest | 228 passed / 33 files；含 Bash 文案、精确回执、禁用发布/重新冻结 |
| Chromium 全量 | 26 passed，0 retry，1.7m；自动恢复 production build |
| Ruff | PASS |
| strict Mypy | 289 files，PASS |
| Evals gate | 6 suites / 10 pairs，PASS；`.eval/workspace-bash-stage3-2026-09-06` |
| 真实 Docker，`.test-tmp/c3r2` | 9 passed / 37 deselected，405.15s |
| 回收发布屏障后的真实 Docker 复验，`.test-tmp/c3r3` | 4 passed / 42 deselected，271.78s；独立 Bash、Coding、Plan 发布与固定验证失败 |

真实 Docker 使用既有固定镜像 `sha256:9b3899021dbd4afaa0225ab5a5b4bb1764d1743bf8e5e103f3a56795df85a3f6`，没有拉取/重建镜像。
9 个场景为独立 Bash 读取 upload → 冻结 → 回收 → 精确确认发布；无变更；非零退出；保护路径；软链接；硬链接；
Coding 混合执行发布；Plan 混合执行发布；Coding 固定验证失败不可降级。全部使用临时 Workspace，不发布到业务 Workspace。

初次真实测试为 3 passed / 1 failed，定位到 Plan 状态先变为 awaiting_artifact_approval、许可尚未关闭时 watchdog 将其撤销；
修复关闭顺序后，上述 9 项全部通过。初始定向测试误带全项目 coverage 门槛，49 个用例均通过但聚合覆盖率未达 75%；
随后定向验证显式使用 `--no-cov`。这不是完整后端覆盖率门禁，完整门禁仍属于阶段 5。

收尾 Ruff、strict Mypy 289 files、Frontend lint/typecheck、228 项 Vitest、production build 与 diff 检查通过。
最后增补“已撤销但已清理也不能开启发布”的保守分支，使用上述 41 项离线补测验证；未再重复真实 Docker 全矩阵。
只读 Docker 列表确认：仅 `pi-wiki-parser-worker-1` 在运行，`Up 7 hours (healthy)`；测试容器均已回收。

## 边界与后续

未自动启用部署配置、未重启业务解析容器、未迁移/删除 Docker 数据。本地 Python 分析、HTTP MCP、Skills 和 E2B 选择均保留。
阶段 4 仍需后台孤儿/TTL/快照与制品缓存回收、跨账号全局配额、完整私有任务日志、管理员执行 UI 和执行 Telemetry。
未知资源继续保留 cleanup_pending，不声称已清理；不恢复或重放命令。
