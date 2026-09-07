# 本机 Web Agent 工程化验收

日期：2026-09-07。范围为用户指定的四项顺序修复，以及之前未提交的 Bash 阶段 5。
没有操作业务数据库/密码、替换业务 Worker、自动启用 Bash、新增 release tag 或推送。

## 已实现

1. Telemetry 周期裁剪终态 span、保护 running、启动安装锁内崩溃对账、错误码可观测与后台任务退出；
   MinerU 5 秒心跳/30 秒新鲜度；已清理终态执行对象释放，持久审计和清理债务不丢失。
2. 离线 SQLite backup API、SHA 文件清单、完整性/FK 校验、新目录恢复；网关与维护共享 OS 锁。
   Wiki v7 严格兼容升级或 v1–v7 显式原件重建，完整 legacy 归档保留，不静默覆盖原库。
3. `scripts/run_local_gates.py`：主工程/Worker 静态、锁、分析环境探针、后端覆盖率、Evals、
   前端与 Chromium 零重试；Docker 单独 opt-in，CI frozen 依赖与 Windows/Linux 定向配置。
4. 当前密码验证的新密码入口；原子撤销全部旧登录，阻断并发旧密码登录、关闭旧 WebSocket；
   前端密码只保留在弹窗局部状态，成功后重新登录；统一当前状态与历史验证的边界。

## 门禁记录

最终一键门禁日志：`.test-tmp/gate-all-20260907T034352Z/`。
`scripts/run_local_gates.py --port 8121` 退出码 **0**，15 个检查/恢复步骤全部通过。

| 检查 | 最终结果 |
|---|---|
| 锁文件 / 分析环境只读探针 | PASS，无安装或升级 |
| 主工程 Ruff / strict Mypy | PASS，308 source files |
| Worker Ruff / strict Mypy | PASS，16 source files |
| 后端全量 | 2674 passed / 1 skipped / 21 deselected，731.59 秒 |
| 覆盖率 | 78.36%，总门槛 75%（含分支统计） |
| Worker 单元 | 11 passed |
| 本机 Fake Evals | 6 suites / 10 pairs，PASS，独立目录 `32340d177747494ba761fb13d3893e13` |
| 前端 lint / typecheck / unit / build | PASS，35 files / 236 tests |
| Chromium | 27 passed，零重试 |
| 浏览器后 production 恢复 | PASS |

整套结束后，针对交付复核补充的不可读子目录处理再跑维护/Auth：**25 passed / 11.05 秒**。
本轮最后一次全仓 Ruff 与 strict Mypy 亦通过；不把补充批次数量加到全量 2674 中。

此前已完成的独立证据（不与全量数字累加）：

- Docker `.test-tmp/gate-docker-20260906T160749Z`：25 项底层检查通过，12 passed / 62 deselected，434.48 秒。
  固定 Bash image ID：`sha256:9b3899021dbd4afaa0225ab5a5b4bb1764d1743bf8e5e103f3a56795df85a3f6`。
- Browser `.test-tmp/gate-browser-20260907T013545Z`：27 passed，零重试，production bundle 恢复成功。
  包含专用测试账号改密、旧登录失效与重新登录；弹窗截图已目视确认，无布局截断。
- 备份/Wiki/Auth 邻接最终专项：38 passed；涵盖恢复后启动真实网关、旧 Cookie 失效、原密码登录、聊天与 AGENT.md 保留。
- 临时数据 CLI backup → verify → restore：3 个文件、20,492 字节，全部成功，报告要求手工激活新目录。
  没有对业务数据执行上述命令；OS Keyring、Docker 镜像和外置目录不在备份中。

### 门禁暴露的问题与修正

- 初次全量在 Wiki 源文件清理失败：重命名后路径跨越 Windows MAX_PATH。
  已校验归属的删除路径改用 Windows 扩展路径，保留 reparse/链接拒绝，不改短测试目录掩盖问题。
- 离线 WAL 模式库首次备份读操作生成空 WAL/SHM，引起来源变化误报。
  无既有 WAL 时 immutable 只读，有 WAL 时普通只读纳入已提交帧；恢复必须通过哈希与数据库检查。
- 中间全量后端 2674 passed / 1 skipped / 21 deselected、coverage 78.46%；前端 236 passed，
  但浏览器旧 MCP 测试用 `/SECRET|PASSWORD|API_KEY/` 检查整个页面，被新增 Password 按钮误触发。
  改为实际提交虚构环境变量秘密值，并断言 UI 不回显该值；没有删掉秘密泄露检查。
  该次整套命令仍记 FAIL，后续浏览器 27 项通过，不把子项通过冒充整套成功。
- 一键入口第二次完整复跑中，后端 2674 passed / 78.49%，但 Evals 拒绝复用旧输出目录。
  已改为每次唯一证据目录，保持禁止覆盖历史证据的约束；随后从完整入口重跑。
- 最后实机修复后的协议、清理、探针、Worker 与门禁组合回归：23 passed；全仓 Ruff 通过。
  smoke 脚本只在成功销毁后输出 passed，避免成功消息早于完整清理。
- 交付复核还补充目录枚举失败即中止：`os.walk` 默认会忽略不可读子目录，不能用部分清单认证完整备份。
  增加权限故障回归，确认不生成成功 manifest；在全量门禁之后追加维护/Auth 邻接复验。

## MinerU 实机证据

### 环境与隔离

- MinerU 3.4.5、PyTorch 2.14.0/CUDA 13.0；RTX 4060 Laptop GPU，8,188 MiB，驱动 581.57。
- 首次完整测试镜像：`sha256:0b447401f3100a0f566cbb1b5b8aa095f9d21483a4d58ad1c641f45d7027d65d`。
  基础 Python digest、131 包哈希锁与模型构建期下载沿用 Worker Dockerfile；依赖/model 构建实际完成。
- 最终增量验收镜像：`sha256:11f1ed9a4fd97423962820889aba0f5827dc025a4474acaa921febd1ff134822`。
  在上述已核验完整镜像上断网 COPY Worker 源码并恢复只读权限；没有重装或替换依赖、没有从浮动标签拉取新基础层。
  标准重建未命中依赖缓存而启动重复下载，已仅停止该构建进程；改用固定父镜像的增量测试构建。
- 独立临时 exchange；network none、只读根、UID 65532、全部 capabilities drop、no-new-privileges、
  init、2 CPU、6 GiB RAM、pids 256；`/tmp` 512 MiB tmpfs，noexec/nosuid/nodev。
  CPU 不暴露 GPU，两个 GPU 档显式 `--gpus all`；未挂载业务 Workspace/队列。
- 路由 `mineru_profiles_2026_09_04_v2`，SHA `a2ba8bc5879124312497ec7f8b2bc53a79ce652b6d2f78f67d1bbaa103785519`。

### 语料和质量判定

使用 `scripts/create_mineru_fixture.py` 生成的两页合成 PDF：文字+三列表格页，以及纯扫描 OCR 页。
ReportLab/Pillow 仅用于独立作者环境，没有加入产品依赖。PDF 已渲染，两页均目视检查。
源 SHA：`75a0f90b50adfd6ea258fa5548ba98085378264d72dbe41d40c8c12febf7874d`。

表格预期逐行是 `Region / Revenue / Status`、`North / 101 / Reviewed`、
`Central / 202 / Reviewed`、`South / 303 / Reviewed`、`Total / 606 / Verified`；
扫描页应识别 `ORANGE-741`。不能只在整篇文本中找到数字，就判定表格正确。

首次 CPU pipeline 技术 smoke 通过：一个真实 MinerU succeeded attempt、2 页、SHA 核对、销毁完成；
75,094 ms，tar 61,440 字节，artifact SHA `2dab3b321c0f726d63af8756606e93ff8ab1f32bd059ddab23465ded961684a8`。
但表格丢失/错配 Revenue 单元格和 Status 列，OCR 口令正确。
内置 quality score 为 1.0，仅反映页数/字符覆盖/结构健康，**不证明语义或单元格准确率**。

### 实机定位和修复

1. 初始 GPU medium/high 都返回安全错误 `parsing_failed`。独立容器内诊断显示真正异常为
   Triton JIT 在只读 `/opt/mineru-home/.triton` 创建缓存失败；CUDA 可用，不是“没有显卡”。
   镜像也不包含 C 编译器。通过 [PyTorch 公开 python_native 接口](https://docs.pytorch.org/docs/stable/backends#module-torch.backends.python_native)
   禁用可选 Triton overrides，继续普通 CUDA 推理；GPU medium 诊断随后成功。
   未把 `/tmp` 改成 exec，也未将模型树设为可写。
2. CPU 重验解析成功，但销毁替换 status 时遇到 Windows bind mount 的短暂 PermissionError，
   使 supervisor 退出、客户端最终超时。旧代码依据 `os.name`，导致 Linux 容器内完全不重试。
   改为跨平台有界原子替换重试；销毁意图保留到终态落盘，失败后可重启继续清理。
   回归覆盖短暂占用和持久失败后保留意图并恢复，不能只检查解析返回值。

### 最终分档结果

以下全部使用最终镜像、同一来源 SHA 和固定路由，每个 Job 恰好一个真实 MinerU attempt；
每档 CLI 退出码为 0，制品下载校验通过且队列终态为 destroyed。

| 档位 | Attempt 耗时 | 制品大小 | 页数 | 人工核对 |
|---|---:|---:|---:|---|
| CPU pipeline（不暴露 GPU） | 61,582 ms | 61,440 bytes | 2 | 正文/OCR 正确，表格单元格错误 |
| GPU medium | 26,151 ms | 61,440 bytes | 2 | 预期三列/五行及 OCR 口令正确 |
| GPU high | 17,772 ms | 71,680 bytes | 2 | 预期三列/五行及 OCR 口令正确 |

耗时是本机同一合成样本的一次记录，high 在复用已加载模型后运行，不用于宣称 high 比 medium 快。
三个 artifact SHA 分别为：

- CPU：`60ea4784b9a82e09601ee3ff5d77cedd9673c06e8c4b8b64fc2cd9de2aa47408`。
- medium：`e9e152094f8cbd36ba670a9cb67ffa4c9c620c58c05168a8f954f4376bb0bc5d`。
- high：`80a11e4ccc84a6fa26af71023fae3a2d448f052fc801ff1567d9be0ffb0046f8`。

保留于 `.test-tmp/mineru-acceptance-20260907/fixed-{cpu,gpu}-results/`，未入 Git。
额外真实 GPU 生命周期：等待 attempt 已 running 后取消 → cancelled → destroyed；
另一个 running Job 强制重启**独立测试容器** → failed/provider_unavailable → destroyed，不重放解析。
测试脚本/交换记录保留于同一验收临时目录。

最终容器 inspect 已核验上述资源与隔离参数；容器内 parsers/protocol/service 三份源码 SHA
与本地最终源文件完全一致。所有本轮测试容器已回收，测试镜像、制品与失败迭代证据保留。
最后 `docker ps` 仅剩原业务 `0eb6a9da36c3 / pi-wiki-parser-worker-1`，healthy，未被重启或替换。

## 交付边界

- runtime manifest 保持 `runtime_ready=false`：尚无代表性中文、论文、复杂表格/图像质量基准通过证据；
  CPU 合成表格已有明确错误。虽然三档链路、取消/重启恢复及配置核验已通过，
  完整代表性质量、压力/配额与镜像合规检查清单没有因此自动全部通过。
- 本机门禁/Fake Evals 不证明真实 LLM 智能水平或外部服务稳定性；CI 配置已更新但未执行远端 CI。
- 维护不等于自动升级部署：恢复目录需手工切换，旧 Keyring/签名密钥需另行保全，原件重建需重新解析/审批。
- 当前仅完成 Wiki 删除路径的 MAX_PATH 修复，不宣称所有极深路径读写都支持。
