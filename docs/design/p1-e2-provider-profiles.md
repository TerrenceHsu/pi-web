# P1-E2 Provider Profiles + Session Model Bindings — 最简后端方案

> **状态**：BACKEND FOUNDATION ✅ FROZEN（实施完成 @ `cad7ca7`；不单独 merge / tag）
> **PIVOT（2026-07-19）**：原 E2-4 独立 Security Freeze cancelled。E2 配置后端停止扩展，进入 P1-E M1 Multi-Provider Runtime。详见 [§19 Pivot 附录](#19-pivot-附录--2026-07-19)。
> **基线**：master `de05c66`（P1-E1 已 MERGED + TAGGED `v0.0.27-secure-credentials`）
> **实施 HEAD**：`89fabfd` / `a2c7932` / `9878ef9` / `17c843d` / `9bbd0f2` / `cad7ca7`
> **前置**：P1-E1 ✅ FROZEN（Credential 子系统已交付）
> **日期**：2026-07-19
> **设计模式**：最小持久化——3 张表 / 4 个主要新增生产模块 + 现有 app/lifespan/session/security 最小接线修改 / 7 个 API；**E2 内零远程模型目录调用**（静态建议 + 手动 `model_id`）

## 0. 设计动机

P1-E1 已交付 Credential 子系统（`web/credentials_*.py` + `secrets/` + `providers/registry.py`）。E2 只解决一件事：

> 把 E1 的 Credential 组合成可选择的 Provider Profile，并把 Profile/Model 持久化绑定到 Session。

原 ROADmap 把 E2 定义为 ProviderProfile + Model Catalog + SessionModelBinding + restart persistence。本方案将「Model Catalog」降级为按需查询结果——**不建立复杂的模型目录数据库**。这样后端现在和以后都保持简单。

### E2 数据流

```
Credential (E1 已完成)
    ↓
ProviderProfile (E2 持久化)
    ↓
SessionModelBinding (E2 持久化)
    ↓
Request-scoped Provider (E3 才执行)
```

## 1. 范围

### E2 保留

- Provider Profile CRUD
- 每个 Profile 关联一个 Credential
- 每个 Profile 保存默认模型
- 每个 Session 保存独立的 Profile/Model
- 一个 Profile 可以设为新 Session 的默认 Profile
- **模型列表只返回静态建议**（Anthropic 静态 + GLM 静态；不读取 Credential、不访问网络）
- **用户始终可以手动填写合法 `model_id`**（不强制在静态列表内）
- server restart 后 Profile 和 Binding 恢复
- Session-only Credential 重启后 Profile 仍在，但状态变成 `needs_key`

### E2 显式不包含

- ❌ 不让当前 Prompt 真正切换 Provider（E3）
- ❌ 不接入 Regenerate（E3）
- ❌ 不修改 Agent Runtime（Core Runtime diff = 0）
- ❌ 不实现前端模型选择器（E4）
- ❌ **不调用任何远程模型目录 API**（`Anthropic /v1/models` 等远端探测均不进入 E2）
- ❌ **不在 E2 中读取 Secret / 不构造 HTTP client / 不引入 remote `ModelOption` source**
- ❌ 不建立模型目录表
- ❌ 不建立模型能力表
- ❌ 不建立 Custom Model CRUD
- ❌ 不建立全局配置表
- ❌ 不实现自动 Provider fallback
- ❌ 不实现 Custom Base URL
- ❌ 不实现 Custom Provider
- ❌ 不实现 OpenAI-compatible Adapter
- ❌ 不修改 D2 Revision schema

> **E2 真实外部网络调用 = 0**。所有模型列表都来自静态常量；`model_id` 由用户输入，不强制要求在静态列表中。模型实际可用性由 E3 创建 Provider Client 时确认。

请求真正使用 Session 选择的 Provider/Model 仍属于 **E3**。

## 2. 对原 ROADMAP 的精简

| 原设计 | 最简方案 |
|---|---|
| ProviderProfile | 保留 |
| SessionModelBinding | 保留 |
| 持久化 Model Catalog | **删除** |
| Model Catalog Repository | **删除** |
| Custom Model CRUD | **删除** |
| Model refresh 持久化 | **删除** |
| 独立 Global Default 表 | **删除**（用 `Profile.is_default`）|
| ModelCapabilities 持久化 | **删除**（运行时返回，不存）|
| Custom Base URL | **暂不支持** |
| 远程模型目录 API（`/v1/models`）| **删除**——E2 内不再调用任何远端模型 endpoint |
| 模型列表 | **静态建议常量**（Anthropic + GLM 各一份）|
| 自定义模型 | 前端允许直接填写 `model_id` |
| 全局默认 | `ProviderProfile.is_default` |

### 最终数据库（3 张表）

- `web_provider_config_schema_meta`——schema 版本
- `web_provider_profiles`——Profile 业务表
- `web_session_model_bindings`——Session 绑定表

## 3. ProviderProfile

### 3.1 数据结构

```python
@dataclass(frozen=True)
class ProviderProfile:
    id: str
    name: str
    provider_id: str
    credential_id: str
    default_model: str
    enabled: bool
    is_default: bool
    created_at: int
    updated_at: int
```

Profile 聚合：

- Provider（`provider_id`）
- Credential（`credential_id`）
- Default Model（`default_model`）

### 3.2 Profile 不保存

- ❌ API Key
- ❌ `secret_ref`
- ❌ `fingerprint`
- ❌ `masked_value`
- ❌ validation endpoint
- ❌ SecretStore 类型
- ❌ 模型列表
- ❌ base_url（见 §3.3）

`masked_value` 可以在返回 Profile View 时通过 Credential Service 动态投影，但**不复制到 Profile 表**。

### 3.3 为什么不保存 Base URL

最简版本只允许**内置 Provider**：

- `anthropic`
- `glm`

Base URL 直接来自 `ProviderDefinition.default_base_url`（E1 已冻结）。

这样可以删除：

- Custom URL SSRF 防护
- URL 重新验证状态机
- URL 与 Credential 的重新绑定逻辑
- 自定义模型目录 endpoint 组合
- Profile 级 Base URL migration

Profile API 可以返回解析后的 `base_url` 供 UI 显示，但它是**只读字段**，不落 Profile 表。

> Custom Base URL 以后也不默认加入；确有业务需求时单独设计，不让它进入基础 Profile 模型。

## 4. SessionModelBinding

### 4.1 数据结构

```python
@dataclass(frozen=True)
class SessionModelBinding:
    session_id: str
    profile_id: str
    model_id: str
    source: Literal["default", "explicit"]
    created_at: int
    updated_at: int
```

含义：

```
Session A → Anthropic Profile → model-A
Session B → GLM Profile       → model-B
```

每个 Session **最多一条** Binding。

### 4.2 默认 Profile 的语义

Profile 表中加入 `is_default: bool`。SQLite 使用 partial unique index：

```sql
CREATE UNIQUE INDEX ux_provider_profiles_default
ON web_provider_profiles(is_default)
WHERE is_default = 1;
```

系统**最多有一个默认 Profile**，也可以没有默认 Profile。

#### enabled / is_default 交叉约束（冻结）

| 规则 | 实现位置 |
|---|---|
| `is_default=true` 要求 `enabled=true` | Service `create_profile` / `update_profile` 入参校验 |
| 设置某 Profile 为 default 时，若它当前 `enabled=false` → 409 `profile_disabled`（不允许同时设默认 + 禁用） | Service update 事务内 |
| 禁用当前默认 Profile 时，**同一 `BEGIN IMMEDIATE` 事务**自动清除 `is_default`（atomic `SET enabled=0, is_default=0`） | Service update 事务内 |
| 新 Session 只物化 `enabled=true` 的默认 Profile；当前默认被禁用后 → 新 Session 不创建 Binding（无默认可用） | `initialize_new_session_binding` |
| 显式绑定（`PUT /api/sessions/{sid}/model-binding`）到 `enabled=false` 的 Profile → 409 `profile_disabled` | Service `set_session_binding` |
| 已有 Binding 在 Profile 后续被禁用时**继续保留**（不级联清空、不切回默认）——用户下次显式切换时再处理 | Service 行为契约 |

> 「禁用」是用户主动行为；「删除」是用户主动行为。两者都不应静默影响已有 Session 的当前选择——这是「请求启动后 Provider/Model 不可变」的局部体现。

#### 新建 Session 流程

```
创建 Session
  → 查询 is_default=true AND enabled=true 的 Profile
  → 存在：创建 SessionModelBinding（source=default，使用 Profile 当时的 default_model）
  → 不存在（无默认 OR 默认被禁用）：Session 不创建 Binding
```

绑定使用 Profile 当时的：

- `profile_id`
- `default_model`
- `source = "default"`

之后修改默认 Profile，**不影响已有 Session**。

#### 示例

```
默认 Profile = Anthropic
创建 Session A → Session A 绑定 Anthropic

默认 Profile 改为 GLM
Session A → 仍然是 Anthropic
新建 Session B → 绑定 GLM
```

这是「全局默认仅用于新建 Session」的最小实现。

## 5. 数据库设计

### 5.0 Connection 不可变约束（冻结）

Provider Config Store 的独立 aiosqlite connection **必须**：

```python
async with aiosqlite.connect(db_path, isolation_level=None) as conn:
    await conn.execute("PRAGMA foreign_keys=ON")
    cursor = await conn.execute("PRAGMA foreign_keys")
    row = await cursor.fetchone()
    assert row[0] == 1, "foreign_keys=ON failed to apply"
```

> aiosqlite 默认不开启 FK；必须显式 ON 并验证返回 1。若 PRAGMA 失败则视为 Store 初始化失败，App 启动失败。

`PRAGMA foreign_keys=ON` 与「显式 profile-in-use 查询」**双重保险**处理并发删除：

- FK `profile_id ON DELETE RESTRICT`：DB 层兜底——若已有 Binding 引用，DELETE Profile 立即失败
- Service 层 profile-in-use 查询：应用层显式返回 409 `profile_in_use`（避免依赖 FK 错误码做错误映射）

两者都不可省略。

### 5.1 Schema metadata

```sql
CREATE TABLE web_provider_config_schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

初始版本：`version = 1`。

**不复用**：

- Credential schema version（`web_credentials_schema_meta`）
- Extension schema version（`extension_store` v2）
- D2 Revision schema version（v2）

### 5.2 Provider Profile

```sql
CREATE TABLE web_provider_profiles (
    id TEXT PRIMARY KEY,

    name TEXT NOT NULL
        CHECK(length(trim(name)) BETWEEN 1 AND 128),

    provider_id TEXT NOT NULL
        CHECK(length(trim(provider_id)) BETWEEN 1 AND 64),

    credential_id TEXT NOT NULL
        CHECK(length(trim(credential_id)) BETWEEN 1 AND 256),

    default_model TEXT NOT NULL
        CHECK(length(trim(default_model)) BETWEEN 1 AND 256),

    enabled INTEGER NOT NULL DEFAULT 1
        CHECK(enabled IN (0, 1)),

    is_default INTEGER NOT NULL DEFAULT 0
        CHECK(is_default IN (0, 1)),

    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
```

索引：

```sql
CREATE INDEX ix_provider_profiles_updated_at
ON web_provider_profiles(updated_at DESC);

CREATE INDEX ix_provider_profiles_credential
ON web_provider_profiles(credential_id);

CREATE UNIQUE INDEX ux_provider_profiles_default
ON web_provider_profiles(is_default)
WHERE is_default = 1;
```

**`credential_id` 不建外键**。原因是删除 Credential 后，Profile 应**保留**并显示 `needs_credential` 状态，而不是一起被删除。这也符合 P1-E 最终验收中的「删除 Key 后 Profile → needs_key」目标。

### 5.3 Session Binding

```sql
CREATE TABLE web_session_model_bindings (
    session_id TEXT PRIMARY KEY,

    profile_id TEXT NOT NULL,

    model_id TEXT NOT NULL
        CHECK(length(trim(model_id)) BETWEEN 1 AND 256),

    source TEXT NOT NULL
        CHECK(source IN ('default', 'explicit')),

    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,

    FOREIGN KEY(profile_id)
        REFERENCES web_provider_profiles(id)
        ON DELETE RESTRICT
);
```

索引：

```sql
CREATE INDEX ix_session_bindings_profile
ON web_session_model_bindings(profile_id);
```

**不对 `session_id` 建 Sessions 外键**——避免 Provider Config schema 与现有 Session Store 强耦合。Service 层在写 Binding 前确认 Session 存在。

> FK `profile_id ON DELETE RESTRICT` 在 `PRAGMA foreign_keys=ON`（§5.0）下生效。Service 仍保留显式 profile-in-use 查询，避免依赖 FK 错误码做错误映射。

### 5.4 数据约束（冻结）

| 字段 | 约束 | 实现位置 |
|---|---|---|
| `profile_id` | **随机不可预测 ID**（`secrets.token_urlsafe(32)` 或等价），不接受用户提交的 ID | Service `create_profile`（POST 不接受 `id` 字段；DB PK 由 Service 生成） |
| `provider_id` | **必须存在于 `ProviderRegistry`**（E1 已冻结的 registry）——不存在 → 422 `unknown_provider` | DTO validator + Service 二次校验 |
| `credential_id` | **创建 / 更新时必须存在**（查询 `CredentialService` 或其安全接口）——不存在 → 422 `unknown_credential`；创建后 Credential 可被独立删除，Profile 派生 `needs_credential` | Service `create_profile` / `update_profile` |
| `model_id`（Profile.default_model + Binding.model_id）| 非空 / 长度 ≤ 256 / **无控制字符 / 无换行 / 无 NUL**（reject `\x00-\x1f` / `\x7f` / `\r` / `\n`）| DTO validator `validate_model_id` |
| `name` | 1–128 chars（trim 后），允许 Unicode 文本（用户可读名称）| DTO validator |
| `is_default` 切换 | **必须使用单个 `BEGIN IMMEDIATE` 事务**——切换默认 Profile = UPDATE 旧 default=0 + UPDATE 新 default=1 在同一事务内 | Store `set_default_profile(profile_id)` 单事务 |
| `enabled` / `is_default` 交叉 | 详见 §4.2 enabled/default 表 | Store + Service 协同 |

#### 5.4.1 `profile_id` 为什么必须随机不可预测

- 防止攻击者枚举 `/api/provider-profiles/{profile_id}` 探测他人 Profile（虽然当前是 localhost-only，未来若放开 Origin 仍需保护）
- 防止 Session Binding 被恶意指向（`PUT /api/sessions/{sid}/model-binding` 接受 `profile_id`）
- 与 E1 `credential_id`（`secrets.token_urlsafe()`）保持一致

#### 5.4.2 `credential_id` 创建后可独立删除

Profile 创建/更新时校验 `credential_id` 存在；创建之后 Credential 可被独立删除：

- DB 无 FK 约束（§5.2 显式不建 FK）
- Profile row 保留
- 下次 list/get Profile 时派生 status=`needs_credential`
- 用户可以 PATCH Profile 指向新的 `credential_id`

> 这与 E1 「删除 Credential 后 Profile → needs_credential」的目标语义一致（验收清单 §15 #13）。

## 6. 不建立 Model Catalog 表

不增加：

- `web_model_catalog`
- `web_model_capabilities`
- `web_custom_models`
- `web_model_refresh_jobs`

模型列表**只作为静态建议常量**：

```
GET Profile Models
  → 根据 profile.provider_id 选对应静态常量
  → 返回 ModelOption[]
  → 不读取 Credential
  → 不访问网络
  → 不写 SQLite
```

### 优点（删除项）

- 不处理模型目录过期
- 不处理 refresh transaction
- 不处理模型删除
- 不处理 cache invalidation
- 不处理远端与 custom 合并
- 不处理 Profile 删除后的 catalog 清理
- 不处理 catalog schema migration
- **不实现 `Anthropic /v1/models` HTTP 调用**——任何远端探测均不进入 E2
- **不在 E2 引入 HTTP client / Secret 读取 / 远程 `ModelOption` source**

## 7. 模型列表策略（静态优先，无网络）

统一返回：

```python
@dataclass(frozen=True)
class ModelOption:
    id: str
    display_name: str | None
    source: Literal["static"]   # E2 内只有 static；remote source 留待未来阶段
    capabilities: ModelCapabilities
```

Capabilities：

```python
@dataclass(frozen=True)
class ModelCapabilities:
    streaming: bool | None = None
    tool_calling: bool | None = None
    reasoning: bool | None = None
    vision: bool | None = None
    context_window: int | None = None
```

**未知必须使用 `None`——不能猜成 `False`**。这与冻结 ROADMAP 中的能力字段语义一致。

### 7.1 Anthropic —— 静态建议

```python
ANTHROPIC_MODEL_OPTIONS: tuple[ModelOption, ...] = (
    # 维护方式：Provider 公布新模型时同步更新源代码常量；
    # 不读取 Credential、不访问网络。
)
```

静态列表只作为 UI 建议，**不构成强校验**。

### 7.2 GLM —— 静态建议

```python
GLM_MODEL_OPTIONS: tuple[ModelOption, ...] = (
    # 维护方式：与 GLM 公开模型同步；
    # 不发送网络请求，不通过 Messages endpoint 探测。
)
```

### 7.3 手动模型 ID —— 始终允许

无论静态列表是否包含某个模型，都允许用户提交合法的 `model_id`。前端可以：

- 选择静态列表中的模型
- **或**手动填写模型 ID

后端只验证（见 §5 数据约束 / §9 规则）：

- 非空
- 长度 ≤ 256
- **无控制字符 / 无换行 / 无 NUL**

**不强制要求 `model_id` 必须出现在静态列表中**。

#### 原因

模型目录可能：

- 暂时不可用（E2 不依赖网络，从架构层面规避）
- Provider 文档滞后
- 用户拥有灰度模型
- GLM 使用特殊计划模型
- 静态列表与实际可用模型不完全一致

模型是否真正可调用，由 **E3** 创建 Provider Client 和执行请求时确认——E2 阶段不做这件事。

## 8. Profile 运行时状态

Profile 状态**不持久化**，每次 list/get 时派生：

```python
ProfileStatus = Literal[
    "ready",
    "disabled",
    "needs_credential",
    "needs_key",
    "backend_unavailable",
    "credential_invalid",
    "credential_error",
]
```

### 映射规则

按以下顺序判断（首条命中即返回）：

| # | 条件 | Profile status |
|---|---|---|
| 1 | Profile `enabled = false` | `disabled` |
| 2 | `CredentialRecord` 不存在（已被独立删除） | `needs_credential` |
| 3 | Credential `storage_status = needs_key` | `needs_key` |
| 4 | Credential `storage_status = backend_unavailable` | `backend_unavailable` |
| 5 | `record.last_validated_provider_id == profile.provider_id` **AND** Credential `validation_status = invalid` | `credential_invalid` |
| 6 | `record.last_validated_provider_id == profile.provider_id` **AND** Credential `validation_status = error` | `credential_error` |
| 7 | 其他（含 `never_validated`、其他 provider 的验证结果） | `ready` |

### 8.1 Provider 作用域约束（关键）

**只有当 `record.last_validated_provider_id == profile.provider_id` 时**，Credential 的 `validation_status` 才会影响当前 Profile 的状态。

- Credential 可被多个 Profile（不同 `provider_id`）复用——比如同一个 Key 在 Anthropic 和 OpenAI-compat 下表现不同
- 用户在 Anthropic Profile 下验证 → `last_validated_provider_id = "anthropic"` → 仅 Anthropic Profile 看到 `credential_invalid` / `credential_error`
- 同一 Credential 在 GLM Profile 下验证失败不应污染 Anthropic Profile 的状态
- 反之亦然

### 8.2 `never_validated` 不阻止 Profile 使用

```
storage ready + never_validated → ready
```

因为 E1 Validation 是用户主动操作，不应成为 Profile 创建的硬前置条件。

## 9. 删除与更新规则

### 9.1 删除 Credential

允许删除。

```
Credential 被删除
  → Profile 保留
  → Profile.status = needs_credential
  → Session Binding 保留
```

后续用户可以修改 Profile 的 `credential_id`。

### 9.2 删除 Profile

若存在 Session Binding：

```
HTTP 409 profile_in_use
```

**第一版不自动级联删除 Binding**，也不自动把相关 Session 切到默认 Profile。用户必须先修改相关 Session Binding。

### 9.3 修改 Profile Provider

**禁止**。

Profile 创建后：

- `provider_id` immutable

允许修改：

- `name`
- `credential_id`
- `default_model`
- `enabled`
- `is_default`

修改 Provider 时直接新建 Profile，避免：

- Provider 与 Credential 验证状态混淆
- 模型语义变化
- Session Binding 含义突变
- 未来 E3 Provider Client 行为改变

### 9.4 修改 Profile 默认模型

只影响**以后基于该 Profile 创建的新 Session Binding**。**不覆盖**现有 Session 的 `model_id`。

## 10. 最简后端模块

**四个主要新增生产模块**：

```
src/pi_agent_core_py/web/provider_config_store.py
src/pi_agent_core_py/web/provider_config_service.py
src/pi_agent_core_py/web/provider_profiles_api.py
src/pi_agent_core_py/web/model_options.py
```

**加现有 app / lifespan / session / security 的最小接线修改**：

允许的最小修改：

- `web/app.py`——`create_app()` 加 Provider Config Store 装配 + lifespan 接入 + router include + TrustedHost/host allowlist 复用
- 当前通用安全边界模块（E1 已落地的 `web/credentials_api.py` 中的 TrustedHost dep / Origin dep / `X-PI-Agent-UI` dep / Body limit / SafeValidationError）——Provider Profile API **复用**，不复制第二套
- 当前 Session 创建接线文件（E2-3A 审计后确定具体文件，预计为 `web/app.py` Session create handler 或 `web/state.py`）

**禁止**：

- ❌ 复制第二套安全中间件（TrustedHost / Origin / `X-PI-Agent-UI` / Body limit / SafeValidationError 全部复用 E1）
- ❌ 大规模重构 `web/app.py`
- ❌ 修改 Core Runtime（`loop.py` / `agent.py` / `context.py` / `events.py` / `stream_events.py` / `messages.py` / `providers/base.py` / `providers/glm.py` / `providers/anthropic_compat.py`）

### 10.1 `provider_config_store.py`

包含：

- Profile / Binding dataclass
- schema 初始化和校验
- Profile CRUD
- Binding get / upsert / delete
- default Profile transaction
- profile-in-use 查询

一个 Store 管两张表，共用：

- 一条独立 aiosqlite connection（`isolation_level=None` autocommit + 显式 `BEGIN IMMEDIATE`）
- 启动时 `PRAGMA foreign_keys=ON` + 验证返回 1（见 §5.0）
- 一个 `asyncio.Lock`
- `BEGIN IMMEDIATE` 用于涉及 `is_default` 切换 / Profile CRUD / Binding upsert 的写事务
- CAS / transaction
- **显式 profile-in-use 查询**（与 FK `ON DELETE RESTRICT` 双重保险，见 §5.0）

**不拆成 `ProfileRepository` / `BindingRepository` / `DefaultRepository` 三层**。

### 10.2 `provider_config_service.py`

包含：

```python
class ProviderConfigService:
    create_profile(...)
    update_profile(...)
    delete_profile(...)
    list_profiles(...)
    list_models(...)
    get_session_binding(...)
    set_session_binding(...)
    initialize_new_session_binding(...)
```

通过 **E1 的安全接口**读取 Credential 状态，不直接访问：

- `SecretStoreRouter` 内部映射
- SQLite Credential row
- OS Keyring

仅 `list_models()` 在明确需要时读取 Secret。

### 10.3 `model_options.py`

只包含：

- `ModelOption` / `ModelCapabilities` dataclass
- `ANTHROPIC_MODEL_OPTIONS`（静态常量）
- `GLM_MODEL_OPTIONS`（静态常量）
- `validate_model_id(model_id)`——控制字符 / 换行 / NUL 拒绝 + 长度 ≤ 256

**不做**：

- Catalog Repository / Cache / Refresh Scheduler
- HTTP client / Anthropic /v1/models 调用 / Secret 读取
- Remote `ModelOption` source（`source` 字段 E2 内只有 `"static"`）

### 10.4 `provider_profiles_api.py`

包含：

- Pydantic Request / Response DTO
- Router
- 固定安全错误映射
- Profile / Binding API

**复用 E1 已完成的**：

- TrustedHost
- Origin
- `X-PI-Agent-UI`
- Body limit
- Safe ValidationError

**不重新实现第二套安全中间件**。

## 11. 最简 REST API

共 **7 个操作**。

### 11.1 Profile

```
GET    /api/provider-profiles
POST   /api/provider-profiles
PATCH  /api/provider-profiles/{profile_id}
DELETE /api/provider-profiles/{profile_id}
```

### 11.2 Models

```
GET /api/provider-profiles/{profile_id}/models
```

这个 GET 可能读取 Credential 并访问远程 Provider，因此仍要求：

- `X-PI-Agent-UI: 1`
- 现有 Host / Origin 安全边界

### 11.3 Session Binding

```
GET /api/sessions/{session_id}/model-binding
PUT /api/sessions/{session_id}/model-binding
```

### 11.4 不提供

- ❌ `POST /refresh`
- ❌ `POST /custom-model`
- ❌ `DELETE /custom-model`
- ❌ `GET /global-default`
- ❌ `PUT /global-default`

默认 Profile 通过 Profile PATCH 管理。

## 12. API DTO

### 12.1 Create Profile

```json
{
  "name": "Work Anthropic",
  "provider_id": "anthropic",
  "credential_id": "cred-123",
  "default_model": "claude-model-id",
  "enabled": true,
  "is_default": true
}
```

**不接受**：

- `secret_value`
- `secret_ref`
- `base_url`
- `headers`
- `Authorization`
- `validation_endpoint`
- `capabilities`

### 12.2 Update Profile

```json
{
  "name": "Personal Anthropic",
  "credential_id": "cred-456",
  "default_model": "claude-new-model",
  "enabled": true,
  "is_default": false
}
```

**不允许修改**：

- `id`
- `provider_id`
- `created_at`

### 12.3 Set Session Binding

```json
{
  "profile_id": "profile-123",
  "model_id": "claude-model-id"
}
```

Service 固定写入 `source = "explicit"`。

### 12.4 Profile Response

```json
{
  "id": "profile-123",
  "name": "Work Anthropic",
  "provider_id": "anthropic",
  "provider_display_name": "Anthropic",
  "credential_id": "cred-123",
  "credential_masked_value": "sk-****8A31",
  "default_model": "claude-model-id",
  "enabled": true,
  "is_default": true,
  "status": "ready",
  "created_at": 1780000000000,
  "updated_at": 1780000000000
}
```

**允许返回 `credential_id`**——它是内部记录 ID，不是 Secret reference。

**禁止返回**：

- `secret_ref`
- `fingerprint`
- 完整 Key
- Credential Record
- Provider validation endpoint

## 13. Session 创建接线（审计门 pending）

> **本节实现细节不冻结**——必须先完成 E2-3A Session Creation Audit 后才能确定补偿策略。
>
> **审计已完成 @ 2026-07-19**：详见 [docs/design/p1-e2-session-binding-integration-audit.md](p1-e2-session-binding-integration-audit.md)。
>
> 审计结论：**方案 A 选定**——Session 创建后初始化 Binding，失败时 `asyncio.shield` 包裹的补偿删除（`_compensate_delete`）。Session 创建无 WS 事件、无 `current_session_id` 更新、无 harness attach，副作用极简；现有 `delete_session()` 可用作内部补偿；窗口期 < 100ms 可接受。错误码：`provider_config_unavailable` / `default_binding_failed` / `session_creation_rollback_failed` / `session_not_found` / `profile_deleted_during_binding`。**可进入 E2-3 编码**。

在现有 Web Session 创建成功后调用：

```python
await provider_config_service.initialize_new_session_binding(
    session_id=session.id,
)
```

### 内部逻辑（冻结部分）

```
查询 is_default=true AND enabled=true 的 Profile
  → 不存在：不写 binding
  → 存在：写 profile_id + default_model + source="default"
```

### 失败策略（**待 E2-3A 审计后冻结**）

候选方案 A：**整体回滚**

```
Session 已创建
  → Provider Binding 初始化失败
  → Session 创建整体失败并回滚
```

候选方案 B：**补偿删除**

```
创建 Session
  → 创建 Binding 失败
  → 删除刚创建的 Session
```

候选方案 C：**Binding 失败不阻塞 Session 创建**

```
创建 Session
  → Provider Binding 初始化失败 → 记 warning，Session 仍创建成功（用户无默认 binding）
```

方案选择依赖 E2-3A 审计结果：

- 现有 Session creation 是否能做跨连接原子事务？
- Session delete 能力是否安全（messages / files / snapshots cleanup）？
- ID 返回时机是否在 binding 写入之前？
- 并发可见性是否要求 Binding 在同一事务内可见？

**审计完成前不冻结实现细节**。审计完成后本节改写为确定的单一方案 + 测试要求。

## 14. E2 阶段拆分

建议拆**五个阶段**（E2-3A 是审计门，不写生产代码）。

### E2-1：Schema + Store

**实现**：

- `web_provider_profiles`
- `web_session_model_bindings`
- schema meta v1
- 独立 connection + `PRAGMA foreign_keys=ON` 验证返回 1（§5.0）
- Profile CRUD
- default Profile transaction（含 enabled/is_default 交叉约束）
- Binding CRUD
- 显式 profile-in-use 查询 + FK RESTRICT 双重保险
- restart persistence

**提交**：

```
feat(providers): add profile and session binding store
```

### E2-2：Service + Static Model Options

**实现**：

- `ProviderConfigService`
- Profile status 派生（含 provider 作用域约束 §8.1）
- Credential 状态组合（通过 E1 安全接口）
- `ANTHROPIC_MODEL_OPTIONS` / `GLM_MODEL_OPTIONS` 静态常量
- 手动 `model_id` 校验（控制字符 / 换行 / NUL 拒绝）
- `model_options.py`（无 HTTP client、无 Secret 读取、无远程 source）

**提交**：

```
feat(providers): add profile configuration service
```

### E2-3A：Session Creation Audit（审计门，无生产代码）

**审计项**：

1. 现有 Web Session **create 函数**的调用路径（哪些 handler / lifespan / 测试入口会创建 Session）
2. Session **delete 能力**——是否暴露 API？是否清理 messages / files / snapshots / revisions？
3. messages / files / snapshots / revisions **cleanup 顺序与原子性**
4. **ID 返回时机**——`session.id` 在哪个阶段可被 Provider Store 引用？
5. **并发可见性**——新 Session 在另一并发请求下何时可见？Binding 写入需要同一事务内可见吗？
6. **补偿失败语义**——若 Binding 初始化失败，回滚 Session 是否安全？是否会留下孤儿 messages？

**产出**：

- 审计报告 `docs/design/p1-e2-session-creation-audit.md`（或本设计 doc 附录）
- 确定 §13 失败策略（方案 A / B / C 之一）
- 列出对 `web/app.py` / `web/state.py` / `session_sqlite.py` / `extension_store.py` 的**最小接线修改清单**

**提交**：

```
docs(providers): audit session creation for E2-3 binding wiring
```

**未完成审计前**：

- 不冻结 §13 失败策略的具体实现
- 不修改 `web/app.py` / `session_sqlite.py` 中的 Session 创建路径
- 不进入 E2-3

### E2-3：REST API + Session Creation Binding

**实现**（依赖 E2-3A 审计结论）：

- 7 API operations
- 安全 DTO
- E1 security envelope 复用（TrustedHost / Origin / `X-PI-Agent-UI` / 32 KiB body / SafeValidationError）
- Session 创建时物化默认 Binding（按 E2-3A 冻结的策略）
- 现有 `web/app.py` / `web/state.py` / 通用安全边界模块的**最小接线修改**（不复制第二套安全中间件，不大规模重构 `web/app.py`）

**提交**：

```
feat(web): expose provider profiles and session bindings
```

### E2-4：Restart + Security + Freeze

**验证**：

- Profile restart
- Binding restart
- session-only → `needs_key`
- env → 动态恢复
- keyring → `ready`
- Credential 删除后 Profile 保留 + status=`needs_credential`
- Profile in-use 删除返回 409 `profile_in_use`
- 显式绑定 disabled Profile 返回 409 `profile_disabled`
- disabled Profile 仍可保留已有 Binding
- `model_id` 含控制字符 / 换行 / NUL → 拒绝
- `provider_id` 不在 ProviderRegistry → 拒绝
- `is_default` 切换 / 清除在单事务内（含禁用默认 Profile 自动清除）
- `PRAGMA foreign_keys=ON` 在 Store 启动时验证返回 1
- API / SQLite / log / WS / export 无 Key
- **E2 真实外部网络调用 = 0**（marker 测试：所有 E2 测试均不发出 HTTP 请求）

**提交**：

```
test(providers): freeze profile and session binding foundation
```

## 15. E2 验收清单

E2 完成时只要求：

1. 创建多个 Provider Profile
2. Profile 关联已有 Credential（`credential_id` 创建/更新时必须存在）
3. 一个 Credential 可以被多个 Profile 复用（不同 `provider_id`）
4. Profile 可以设置默认模型
5. 系统最多一个默认 Profile（partial unique index）
6. `is_default=true` 要求 `enabled=true`；禁用当前默认 → 同事务清除 `is_default`
7. 显式绑定 disabled Profile → 409 `profile_disabled`
8. 已有 Binding 在 Profile 后续被禁用时继续保留
9. 新 Session 自动物化默认 Profile/Model（仅当默认 `enabled=true` 时）
10. Session A/B 可以保存不同 Profile/Model
11. 修改默认 Profile 不影响已有 Session
12. 修改 `Profile.default_model` 不覆盖已有 Session
13. Profile / Binding 在 server restart 后恢复
14. session-only Credential 重启后 Profile = `needs_key`
15. env Credential 每次动态解析
16. 删除 Credential 后 Profile 保留（status=`needs_credential`）
17. 被 Session 使用的 Profile 不允许删除（409 `profile_in_use`；显式 profile-in-use 查询 + FK RESTRICT 双重保险）
18. **Profile status 的 `credential_invalid` / `credential_error` 仅在 `last_validated_provider_id == profile.provider_id` 时生效**（其他 provider 的验证结果不污染当前 Profile）
19. **`profile_id` 随机不可预测**（不接受用户提交）
20. **`model_id` 拒绝控制字符 / 换行 / NUL**；长度 ≤ 256
21. **`provider_id` 必须存在于 ProviderRegistry**
22. **`is_default` 切换/清除使用单个 `BEGIN IMMEDIATE` 事务**
23. **`PRAGMA foreign_keys=ON` 在 Store 启动时验证返回 1**
24. **Anthropic 与 GLM 都只返回静态建议**——`GET /api/provider-profiles/{profile_id}/models` 不读 Credential、不访问网络
25. 任意合法 `model_id` 可以手动保存
26. API、SQLite、日志、事件、Export 中无完整 Key
27. **E2 真实外部网络调用 = 0**（marker 测试：所有 E2 测试均不发出 HTTP 请求）
28. 当前 Prompt 行为不发生变化
29. Core Runtime diff 为 0

## 16. E2 完成后的状态

```
Session A 已保存：
  Profile = Anthropic
  Model   = model-A

Session B 已保存：
  Profile = GLM
  Model   = model-B
```

但是**当前 Agent 仍可能使用旧 Provider**。

这是正常阶段边界：

- E2：保存选择
- E3：执行选择
- E4：前端选择器

跨阶段规则仍然是：**请求启动后 Provider/Model 不可变，切换只影响下一次请求；Regenerate 使用当前 Session 的当前模型**。

## 17. 最终方案总结

| 维度 | 决策 |
|---|---|
| 数据库 | 3 张表（schema meta + provider profiles + session model bindings）|
| Connection | 独立 aiosqlite + `PRAGMA foreign_keys=ON` 验证返回 1（§5.0） |
| 生产模块 | 4 个主要新增模块 + 现有 app/lifespan/session/security 最小接线修改（§10）|
| API | 7 个操作 |
| 持久化 | 只存 Profile 和 Binding |
| 模型目录 | 不持久化 / 无缓存 / **静态建议常量** / 不访问网络 |
| 远程模型 API | **不调用**（E2 真实网络调用 = 0）|
| 自定义模型 | 直接填写 `model_id`（校验控制字符 / 换行 / NUL）|
| 默认设置 | `Profile.is_default`（partial unique index；切换/清除单事务；与 enabled 交叉约束）|
| Profile status provider 作用域 | 仅 `last_validated_provider_id == profile.provider_id` 的验证结果影响 status |
| Profile.provider_id | 创建后 immutable |
| Custom Base URL | 不支持 |
| 实际 Provider 切换 | 留给 E3 |
| 阶段数 | 5 个（E2-1 / E2-2 / E2-3A 审计门 / E2-3 / E2-4）|

这套方案既满足 E2 的核心产品需求，也不会把后端发展成 Profile / Catalog / Capability / Refresh / Custom Model / Global Setting 等多套状态系统。

---

## 18. 待 user 决定

1. **本设计 doc 已 DESIGN FROZEN @ 2026-07-19** → 进入 E2-1 编码
2. **若需进一步调整** → 标注修订项，重新进入 DRAFT 状态，重审后定稿
3. **若发现实施期不可行** → E2-3A 审计结论可调整 §13 失败策略；其余冻结项需重新审议

---

## 19. Pivot 附录 — 2026-07-19

> **决策**：E2 配置后端 frozen 不扩展；E2-4 独立 Security Freeze cancelled；进入 P1-E M1 Multi-Provider Runtime。

### 19.1 历史状态

- E2-1 / E2-2 / E2-3A / E2-3（Composition + REST API + Session binding）已 ✅ FROZEN @ `89fabfd` / `a2c7932` / `9878ef9` / `17c843d` / `9bbd0f2` / `cad7ca7`
- 测试基线：2063 full pytest + 2×37/37 E2E + ruff clean + 0 Core Runtime diff + 0 network + 0 secret reads
- 原 §14 阶段拆分中 E2-4「Restart + Security + Freeze」**未实施**—— cancelled，并入 M3 Unified Freeze

### 19.2 Pivot 动机

原 ROADMAP 把「多 Provider 切换」拆为 P1-E2（持久化）/ E3（请求执行）/ E4（前端）/ E5（验收），拆得过细；并且 E2-4 独立 Security Freeze 会冻结一个用户无法直接使用的配置后端——冻结口径与产品价值错配。

改为单一 milestone：**P1-E Multi-Provider Switching = M1 Runtime / M2 Frontend / M3 Unified Freeze**。

### 19.3 本文档的语义变化

- §0 ~ §17 保留为**历史设计**（不再作为 active design）
- §0 设计动机仍然成立：E2 只解决「把 Credential 组合成可选择的 Profile 并持久化绑定到 Session」——这件事已经做完
- §3 / §4 / §5 / §9 / §10 / §11 / §12 / §13 / §14 / §15 / §16 全部已实施并 frozen，作为 **M1 持久化基础**继续使用
- §6 / §7（静态模型列表策略）继续有效——M1/M2/M3 不引入 remote ModelOption source

### 19.4 M1 之后不再扩展的能力

E2 配置后端 frozen 不再增加：

- Profile 表字段
- Binding 表字段
- REST API 数量（保持 7 个）
- Profile status 枚举
- ProviderDefinition 字段
- 静态模型选项数据来源

如需新增能力，进入 M2/M3 之后单独评估，不回填 E2 设计。

### 19.5 后续阶段定位

| 阶段 | 范围 | 状态 |
|---|---|---|
| ~~E2-4 Restart + Security + Freeze~~ | cancelled | 并入 M3 |
| **M1 Multi-Provider Runtime** | `providers/openai_compat.py` + Qwen/Kimi presets + `factory.py` + `web/provider_runtime.py` + Prompt/Regenerate 接线 | ⚪ 待启动（M1-0 审计先行）|
| **M2 Frontend Switching** | `providerStore` + `ProviderSelector` + `ProviderSettingsModal` | ⚪ 待启动 |
| **M3 Unified Freeze** | Secret leak audit + GLM/Qwen/Kimi contract tests + Playwright + merge + tag | ⚪ 待启动 |

详细 M1/M2/M3 范围见 [ROADMAP.md](../../ROADMAP.md) § P1-E。

### 19.6 跨阶段 frozen 边界（M1/M2/M3 全程有效）

E2 已冻结的所有约束继续有效：

- 3 张表 schema 不变（§5）
- Profile / Binding 字段不变（§3 / §4）
- 7 个 API 不变（§11）
- Profile status 派生规则不变（§8）
- enabled/is_default 交叉约束不变（§4.2）
- `profile_id` 随机不可预测（§5.4.1）
- `credential_id` 创建后可独立删除（§5.4.2）
- `model_id` 控制字符 / 换行 / NUL 拒绝（§5.4）

### 19.7 source of truth

- **仓库文档**（ROADMAP / STATUS / TODO / 本设计 doc）是 source of truth
- **memory** 仅作辅助上下文，可能与仓库文档存在时差——以仓库文档为准
