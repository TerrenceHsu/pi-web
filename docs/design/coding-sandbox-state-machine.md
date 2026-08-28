# Coding Sandbox 状态机与 Validation→Freeze 屏障

> 状态：阶段 4A 状态机/Freeze 屏障与阶段 4C 发布目标屏障均已完成  
> 日期：2026-08-28  
> 范围：托管 Coding Sandbox 的持久生命周期、并发转换和已验证制品冻结

## 1. 目标

Coding Sandbox 以 Backend 的单一状态机作为规范事实源。REST 和 UI 只消费 Backend 返回的
`allowed_actions`，不得分别维护另一套可转换状态。每一次持久状态更新都比较调用方读取到的完整旧记录；
若记录已经被其他请求推进，则返回 `operation_conflict`，禁止 last-write-wins 覆盖。

## 2. 状态机

```text
creating ──> ready ──> validating ──> validated ──> freezing
    │          ▲             │             │             │
    │          │             └──> validation_failed <───┘  (屏障前 validation_stale)
    │          │                         │
    │          └─────────────────────────┘
    │
    ├──────────────────────────────────────────────> failed
    ├──> cancelling ──> cancelled
    └──> discarding ──> discarded

freezing ──> awaiting_approval ──> publishing ──> published
    │                 │
    └──> failed       ├──> cancelling ──> cancelled
                      └──> discarding ──> discarded

failed/interrupted ──> discarding ──> discarded
```

任何非终态都可在进程关闭/重启恢复时进入 `interrupted`。同状态更新只允许刷新状态投影，不代表新转换。
`published`、`cancelled`、`discarded` 是不可再操作终态；`failed`、`interrupted` 仅允许清理制品和远端实例。

## 3. Validation→Freeze 的 TOCTOU 结论

风险窗口不是简单的两个 REST 调用之间，而是“验证完成后到已签名制品落盘前”的全部时间。用户工具、
Sandbox 内后台进程和 Provider 侧带外变化都视为不可信写入。安全目标不是承诺远端文件系统在验证后绝不变化，
而是只签名并发布与成功 validation evidence 完全相同的不可变字节集合。

屏障按以下顺序执行：

1. Validation 在 operation 互斥锁内记录前后完整 workspace SHA、workspace revision、固定验证配置 SHA 和检查结果。
2. Freeze 获取同一把锁，在任何导出前重新读取验证配置并计算完整 workspace SHA；任一不一致均为
   `validation_stale`，此时尚未跨越屏障，可回到 `validation_failed` 后重新验证。
3. 构造绑定 validation evidence 与期望 workspace SHA 的导出请求后，立即把 operation 置为 fail-closed
   frozen；此后 run、validate、write、patch、delete 全部拒绝。
4. 远端导出在归档前后各扫描一次，逐文件读取同时校验 inode/size/mtime/content SHA；本机下载后重新计算
   archive SHA，并校验 manifest、成员集合、文件 SHA、二进制标记和 validation evidence。
5. 签名前再次读取远端验证配置并计算完整 workspace SHA。若屏障后任何检查发现 `artifact_stale`，operation
   进入 `failed` 并销毁，不允许在已冻结实例中重试；用户必须启动新 operation。
6. 只有全部检查通过才生成服务端签名，后续审批与发布只使用该内容寻址制品，不再读取活动 workspace。

因此 Validation→Freeze 不依赖“检查后立刻使用”的乐观假设：屏障前变化可重验，屏障后变化使整个 operation
失败；并发生命周期请求则由持久记录 CAS 单独防止状态覆盖。

发布目标另有第二道 TOCTOU 屏障：Workspace Publisher 在 Session mutation lock 内重新核验 operation
记录的 baseline revision/tree SHA，并逐文件重算当前内容 SHA；随后才进入 journaled multi-file commit。
审阅期间发生的任何 Workspace 修改都会产生 `publish_conflict`，不会自动合并或覆盖。锁内提交期间其他
mutation 只能等待，失败与未提交崩溃恢复都回到原 revision/字节集合。

## 4. API 合同

每个 operation 投影增加：

- `state_machine_version`：固定状态机版本；
- `allowed_actions`：当前可执行的命令动作；
- `allowed_transitions`：用于诊断和 UI 展示的直接后继状态。
- `publish_available`：当前 baseline 是否已有对应的事务 Publisher；4C 主应用 Workspace 组合固定为
  `true`，缺少发布目标的兼容组合仍会移除 `publish`/`publishing`。
- `published_workspace_revision`：WorkspaceStore 成功提交后的目标 revision；本地目录兼容 Publisher 为 `null`。

前端按钮由 `allowed_actions` 控制。旧响应可短期使用 status fallback，以避免刷新期间旧页面缓存失效；
Backend 仍会独立校验转换并以 CAS 决定胜者。

## 5. 验收

1. 非法转换在创建新任务或调用 Provider 前被拒绝。
2. 两个并发动作从同一旧记录出发时只有一个 CAS 成功，另一个返回 `operation_conflict`。
3. `cancelling`/`discarding` 不再错误暴露 cancel/discard 动作。
4. 既有带外修改回归继续证明屏障后不产生签名制品。
5. `artifact_stale` 在管理层保留为明确终态错误，而不是降级为笼统 `operation_failed`。
