# web/ 子包目录结构（2026-08-15 目录重组）

## 背景

P2 freeze（HEAD `58e66d7`）后的一次纯结构性重组：把 FastAPI 相关代码按**域子包**
聚合，形成 core（包根）/ 域子包（FastAPI）/ frontend（Vue）三层清晰边界。
动机是目录分类明确，不改变任何行为。

- **只做** `git mv` + Python import 路径更新；HTTP 路由、前端、DB schema 零改动。
- 重组按域分两个 commit：credentials 域 `b90dd4c`、providers 域 `bb6dc2e`。
- 每个 commit 后 full-suite 回归：3511 passed / 0 failed（与 P2 freeze baseline 一致）；
  前端 build:e2e + 347 vitest + E2E 抽样（regenerate.spec.ts 9/9）通过。

## 当前结构

```text
src/pi_agent_core_py/
├── agent.py / loop.py / harness.py / ...   # 核心 Agent（包根，与主仓库 pi-py 一致）
├── mcp/ policy/ providers/ secrets/ tools/ # 核心子包
└── web/
    ├── app.py                 # FastAPI 入口 + 主路由（session/prompt/MCP/skills/events）
    ├── state.py               # WebAppState / TraceEventBuffer / WebRunRequest
    ├── serializers.py         # JSON-safe 序列化
    ├── local_web_security.py  # WebSecurityConfig（TrustedHost / UI header / Origin）
    ├── extension_store.py     # uploaded Skills / MCP server 持久化
    ├── files.py               # VirtualFileStore（P0-2 上传文件）
    ├── markdown_export.py     # session Markdown 导出 renderer
    ├── credentials/           # ★ 域子包：凭证管理（P1-E1/E2）
    ├── providers/             # ★ 域子包：Provider Profiles / 请求级选择（P1-E2/M1-5）
    ├── knowledge/             # 域子包：知识库 / RAG（P2，原有）
    ├── frontend/              # Vue 前端（npm 项目）
    └── static/                # Vue build 产物（vite outDir，勿改）
```

## 模块迁移映射

### web/credentials/（commit b90dd4c）

| 旧路径（web/） | 新路径 |
|---|---|
| `credentials_api.py` | `credentials/api.py` |
| `credentials_dto.py` | `credentials/dto.py` |
| `credentials_errors.py` | `credentials/errors.py` |
| `credentials_service.py` | `credentials/service.py` |
| `credentials_store.py` | `credentials/store.py` |
| `credentials_runtime.py` | `credentials/runtime.py` |
| `secret_store_router.py` | `credentials/secret_store.py` |

### web/providers/（commit bb6dc2e）

| 旧路径（web/） | 新路径 |
|---|---|
| `provider_profiles_api.py` | `providers/api.py` |
| `provider_config_store.py` | `providers/config_store.py` |
| `provider_config_service.py` | `providers/config_service.py` |
| `provider_config_runtime.py` | `providers/config_runtime.py` |
| `provider_runtime.py` | `providers/runtime.py` |
| `provider_validation.py` | `providers/validation.py` |
| `model_options.py` | `providers/model_options.py` |

## 约定与注意事项

1. **域子包不做 re-export**——调用方按子模块 import：
   `from pi_agent_core_py.web.credentials.api import build_full_credential_router`。
2. **命名歧义**：`pi_agent_core_py.providers`（核心：registry/factory/base）与
   `pi_agent_core_py.web.providers`（web 层 profile 配置）是两个包。web 内相对
   import 需注意 dot 深度：域子包内引用核心包用 `...providers`，引用兄弟域子包
   用 `..credentials`。
3. **顶层运行时产物保持现状**（用户确认）：`dev_sessions.db*` / `uploads/` /
   `dev_data/` 已被 .gitignore 覆盖且未跟踪，不迁移、不改脚本 cwd-relative 默认。
4. 主仓库 `D:\LLMTutorial\pi\pi-py` 未做同样重组——本副本与主仓库的文件级 diff
   对应关系以本文档映射为准。
