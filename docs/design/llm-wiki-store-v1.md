# LLM WikiStore v1 与 Legacy Retirement

> 决策日期：**2026-08-23**  
> 状态：**阶段 2 已实现；尚未切换旧 Knowledge 运行时**  
> 上位设计：[`llm-wiki.md`](llm-wiki.md)

## 1. 交付边界

阶段 2 新增 `pi_agent_core_py.web.wiki`，实现全新的页面中心型 `WikiStore`、`wiki.db`、固定
目录镜像、Space CRUD 和显式 legacy retirement。它与旧
`pi_agent_core_py.web.knowledge` 没有 import 或数据库依赖。

当前应用仍运行旧 Knowledge API/Worker/UI；阶段 2 不在 lifespan 中自动打开新 Store，也不会
移动真实用户的 `knowledge.db` 或 `libraries/`。原因是旧 Ingestion/Indexing Worker 与
KnowledgeStore 必须先全部停止和关闭。阶段 3 接入新 Source API 时可用 `preserve` 模式并行打开
新库；最终退役切换才显式使用 `retire` 模式。

## 2. 规范目录

```text
knowledge_root/
├── wiki.db
├── legacy/
│   ├── recovery/
│   └── knowledge-{created_at_ms}-backup_{id}/
│       ├── knowledge.db
│       ├── libraries/
│       └── legacy-manifest.json
└── spaces/
    └── space_{id}/
        ├── space.json
        ├── raw/
        └── pages/
```

`wiki.db` 是唯一规范事实源；`space.json` 与后续 `pages/**` 是可重建镜像。Raw 原件及 Parser
制品在阶段 3 写入，但阶段 2 已提供相同路径所有权与原子写入原语。

## 3. SQLite 身份与版本 Gate

Schema v1 使用三重身份：

- `PRAGMA application_id = 1464421193`（ASCII `WIKI`）。
- `PRAGMA user_version = 1`。
- `wiki_schema_meta.schema_version = 1`。

打开现有文件时，任一身份不一致、未来版本、无版本的部分 schema、缺表/缺列、
`quick_check`/`foreign_key_check` 失败都会 fail closed，不会把未知数据库当成新库覆盖。新库要求：

- SQLite `>=3.37.0`，支持 `STRICT` tables。
- JSON `json_valid` / `json_type` 可用。
- `foreign_keys=ON`。
- `trusted_schema=OFF`、`synchronous=FULL`、5 秒 busy timeout；文件库进入 WAL。

所有表都使用 `STRICT`、CHECK、UNIQUE 和真实 SQLite Foreign Key。数据库路径以及固定 WAL/SHM
路径如果是 symlink、junction/reparse point、目录或特殊文件，连接前直接拒绝。

## 4. Schema v1

阶段 2 一次性建立后续阶段所需规范表，但只开放 Space CRUD：

| 表 | 规范职责 |
|---|---|
| `wiki_spaces` | Space 元数据、状态、graph revision |
| `wiki_sources` | 不可变来源身份、状态和受控相对路径 |
| `wiki_artifacts` | parsed Markdown、manifest、内嵌图片 hash 证据 |
| `wiki_pages` | 页面身份、slug、当前 revision 指针 |
| `wiki_page_revisions` | 不可变页面版本与 content SHA |
| `wiki_page_sources` | 系统维护的 `derived_from` 事实 |
| `wiki_edges` | 五种固定 Agent 可提案页面关系；不允许 `derived_from` |
| `wiki_change_sets` | 一次审批的状态和 graph 基线 |
| `wiki_change_set_items` | 稳定顺序的页面/边操作、before 证据、payload、diff |
| `wiki_conversations` | Wiki Space 与独立 Agent Session 的可信绑定 |
| `wiki_jobs` | parse/synthesis/search/graph projection 后台任务 |

新库不创建 `knowledge_chunks`、`knowledge_chunks_fts` 或 Session-Library Binding，也不复制旧表。
页面 FTS5 在阶段 5 建立，只索引已批准页面。

## 5. Space CRUD

Space ID 固定为服务端生成的 `space_` + 24 位小写十六进制。名称必须为 1–120 个已 trim、无
控制字符的 Unicode 字符，说明最多 4000 字符。

状态机：

```text
active   -> archived | deleting | failed
archived -> active | deleting | failed
failed   -> active | deleting
deleting -> terminal in phase 2
```

`delete_space` 只进入 `deleting`，不在阶段 2 递归删除数据。更新支持
`expected_updated_at_ms` CAS；时间戳至少单调增加 1，即使宿主时钟没有前进。写事务使用
`BEGIN IMMEDIATE`，双连接竞争只能有一个匹配旧版本。

创建/更新先提交规范数据库，再重建镜像；若镜像失败，返回固定 `mirror_failed`，数据库事实仍在，
下次打开或显式 repair 会恢复。这样不会为了修复可重建 JSON 反向覆盖已提交数据库。

## 6. 文件与镜像安全

`WikiFileStore` 只接受合法 Space ID 与规范相对 POSIX 路径，且首段必须是 `raw/` 或
`pages/`。拒绝：

- 绝对路径、`..`、反斜杠、空/点段和控制字符。
- Windows drive/ADS 冒号、保留设备名、尾随点/空格。
- 任意层 symlink、junction/reparse point 和特殊文件。
- 文件或目录的 Unicode `casefold()` 冲突。

Raw 写入使用原子 no-clobber hard link，不能覆盖已存在原件；Page mirror 使用同目录 temp、
flush/fsync 和 `os.replace`。读取前后核对文件身份、大小和上限。公开异常只有固定码与安全文本，
不回显原件正文或宿主绝对路径。

启动 repair：

- 为数据库中每个 Space 重建缺失目录和 canonical `space.json`。
- 报告数据库没有对应行的合法 orphan space，不自动删除。
- 把 `.creating-*` 崩溃 staging 移入 `legacy/recovery/`，不直接销毁证据。

## 7. Legacy Retirement

`WikiStore.open(..., legacy_policy="preserve")` 是默认值，只初始化/打开新库，不碰旧数据。
`legacy_policy="retire"` 是显式切换动作，调用前置条件是旧 Worker 已停止、旧 Store 已关闭。

Retirement 顺序：

1. 以固定 `.wiki-legacy-retirement.lock` 排他，第二个执行者得到 `legacy_busy`。
2. 只检查固定 `knowledge.db` 与 `libraries/`；拒绝链接、reparse 和特殊文件。
3. 使用 SQLite backup API 生成包含 WAL 已提交内容的一致数据库快照。
4. 逐个复制普通文件，前后核对文件身份，生成相对路径/大小/SHA-256 清单。
5. 验证备份 inventory 后写 canonical `legacy-manifest.json`，原子发布完整 backup 目录并把文件
   标记为只读。
6. 先 checkpoint 并移除旧 DB/WAL/SHM，最后移除旧 `libraries/`。
7. cleanup 失败时返回带 backup receipt 的固定 `legacy_cleanup_failed`；完整备份已经存在，调用方
   不得初始化新产品运行时，需先处理恢复。

任何原件都不会在完整、已验证、已发布的备份出现前删除。旧 Library/Document/Chunk/Binding
业务数据不会插入 `wiki.db`。删除 `legacy/` 仍需以后单独、明确的用户授权。

## 8. 阶段 2 验收

- 新 Wiki Store/File/Legacy/独立性回归：52 passed、4 个 Windows symlink capability skipped。
- 与旧 Knowledge Store/File 和 SQLite open-failure 邻接回归：149 passed、5 skipped。
- Ruff 全仓：PASS。
- strict Mypy 全仓：152 files / 0 issues。
- Backend 非网络全量：3949 passed、12 skipped、12 deselected，83.35% coverage。
- 真实用户 legacy 数据：未触碰。

## 9. 阶段 3 入口

阶段 3 在此基础上实现 Space/Source API、不可变 PDF/HTML 上传、Parser job 和不可信 artifact
导入。接入应用时先使用 `preserve` 模式，保证旧 UI 仍能运行；最终产品切换仍必须经过显式
retirement Gate，而不能在普通启动中静默移动旧数据。
