# Workspace 上传目录与图片、视频链接解析方案

日期：2026-09-05。`upload/` 改动已实现；图片和视频链接部分为待实施设计。

## 1. 上传目录（本次实现）

所有新上传原件进入当前 Session Workspace 根目录的 `upload/`，包括 PDF、DOCX、XLSX、
图片、文本和代码。首次成功上传后目录自动出现在文件树；新建空 Session 时无需空目录占位。
聊天拖拽会附加文件到当前消息，右侧 Workspace 文件区拖拽或点击 Upload 只归档到 Workspace。
PDF/DOCX/XLSX 仍自动转换，聊天优先附加转换后的 `content.md`。

```text
Workspace/
├── AGENT.md
├── Memory.md
├── upload/                          # 用户上传原件
│   ├── report.pdf
│   ├── report (2).pdf               # 同名上传不覆盖
│   ├── photo.png
│   ├── notes.docx
│   ├── data.xlsx
│   └── main.py
├── documents/                       # 解析产物
│   └── report-<source-file-id>/
│       ├── content.md
│       ├── manifest.json
│       ├── tables/
│       └── assets/
└── scripts/                         # Agent 创建、修改的工作代码
```

原文件名沿用安全清理规则；同名路径自动追加 `(2)`、`(3)`，仍保留后缀。输出目录使用完整
source file ID，避免同名文件在同一毫秒上传时混用转换结果。上传 `AGENT.md` 只产生
`upload/AGENT.md`，不会改写根指令。可选子目录只能落在 `upload/` 以下。

上传代码也是只读输入。Agent 若要编辑，先从原件在 `scripts/` 创建工作副本；工程摘要只统计
工作代码。普通上传可删除后重新上传；富文档原件沿用既有不可变/删除保护规则。

Workspace 是受管逻辑文件树：字节仍保存于现有私有 file-id 存储，`logical_path` 指向
`upload/<文件名>`。UI、文件 API、下载及沙箱 materialization 使用同一份 metadata；沙箱内
实际生成 `/workspace/upload/<文件名>`。不另建未经索引的磁盘副本。
历史 `inputs/`、`scripts/` 上传和 `documents/<id>/original.*` 保留原位，后者可以继续重解析；
不扫描或移动用户旧文件，无数据库重建要求。

## 2. 图片解析（待实施）

首版接受 PNG/JPEG/WebP 单帧图片，上传原件进入 `upload/`。校验扩展名、MIME、魔数及实际
解码结果；默认单图最多 20 MiB、40 百万像素，同时受 Workspace 25 MiB 单文件和 100 MiB
Session 配额约束。超限或动画/不支持格式返回明确状态，不能显示为“已解析”。

前端在附件或原件预览中提供两种操作：

| 方式 | 用途 | 执行与结果 |
| --- | --- | --- |
| 提取文字 | 截图、扫描件、票据中的文字和表格 | 本机 MinerU，默认 pipeline；GPU medium/high 仅在能力探测通过后可选。生成 `content.md`、结构化定位和 manifest |
| 理解图片 | 场景、图表、流程图问答 | 当前 Session 明确具备视觉能力的 Provider；利用已有 `ImageContent`，保留原件 file ID 与 SHA，生成标记为模型解读的说明 |

OCR 与模型解读在 UI、manifest 中分别标记；模型描述不充当逐字转录。未配置视觉模型时仅禁用
“理解图片”，OCR 是否可用由本机 MinerU 能力决定。图片无需传给模型即可保存、下载。

MinerU 固定版本的 CLI 已识别图片输入，但当前项目 Worker 合约和预检仍为 PDF-only，不能只
改前端 accept。实施时先把可复用 MinerU 运行适配抽为不依赖 Wiki 的解析服务入口，扩展 source
类型与图片预检；Workspace 产品适配调用此入口，不能导入 Wiki Store、Ingestion 或共享 Wiki
数据库。OCR normalization 后若产生中间文件，manifest 同时记录原图 SHA 与中间文件 SHA。
参考：[MinerU 3.4.5 输入处理源码](https://github.com/opendatalab/MinerU/blob/mineru-3.4.5-released/mineru/cli/client.py)。

AI 核心已有图片块，缺口在 Web 附件解析和实际模型能力装配。实施时以模型能力、而非单纯
Provider 协议名称放行；在发送前读取和校验归属、SHA、MIME，生成受尺寸限制的派生图并删除
EXIF，原图保持不变。仅发送本轮选中的图像内容，重载历史时从 file ID 解析，禁止读取其他
Session 文件。`view_file` 可读 OCR 结果；视觉请求通过产品适配注入图片块。

图片产物建议：`documents/<source-id>/content.md`、`tables/regions.json`、
`assets/normalized.png`、`manifest.json`。定位使用原图宽高与像素 bbox，注明旋转/缩放映射。

## 3. 视频链接解析（待实施）

Workspace 增加“添加视频链接”；支持拖入 `text/uri-list`。聊天粘贴链接时显示“解析视频”
入口，用户点击后才创建任务，普通消息中的 URL 不自动下载。首版支持经实际验收的平台公开
单视频链接；先接 YouTube/Bilibili，逐个平台报告能力，登录、私有、直播及播放列表返回
`unsupported_source` 或 `authentication_required`。

链接作为不可变来源描述保存到 `upload/<标题或video>-<id>.source.json`，内容包括来源 URL、
平台、视频 ID、获取时间和选择的模式。描述通过专用导入 API 创建，普通用户上传 JSON 不会
触发远程抓取。重复导入使用独立 file ID；同一个来源对象重试可复用已验证结果。

| 方式 | 流程 | 输出范围 |
| --- | --- | --- |
| 字幕优先（默认） | yt-dlp 查询人工字幕，其次自动字幕；没有字幕时返回 `no_subtitles`，提供转录按钮 | 带开始/结束时间的字幕文本与原始字幕来源 |
| 语音转录 | 用户选择后获取有界音轨，在本机用 faster-whisper 转录 | 分段时间戳、语言、转录文本；不声称理解画面 |
| 画面分析（后续阶段） | 有界抽帧，复用图片视觉适配，与字幕/音轨时间对齐 | 标记抽样时间的画面描述；不能声称逐帧覆盖 |

yt-dlp 提供跳过视频下载、字幕和自动字幕选项，采用固定参数与参数数组，不接受任意命令、
浏览器 Cookie 路径或用户拼接选项。参考：[yt-dlp 官方选项](https://github.com/yt-dlp/yt-dlp/blob/master/README.md)。
faster-whisper 的转录结果提供分段时间戳，适合生成可引用片段；模型在管理端预装并固定版本，
运行时不下载模型。参考：[faster-whisper 官方用法](https://github.com/SYSTRAN/faster-whisper#usage)。

产物统一进入 `documents/<source-id>/`：`content.md` 为带时间链接的正文；
`tables/segments.json` 记录 `{start_ms,end_ms,text,origin,language}`；
`assets/captions.vtt` 保存取得的字幕；manifest 记录视频 ID、字幕类型、工具/模型版本、
来源文件和所有输出 SHA。音轨仅在本机有界 staging 使用，完成后清理，默认不占用持久空间。
字幕内容与 LLM 后续总结分别记录；引用指向具体时间段。

远程抓取是新增网络边界：只允许 HTTP(S) 公网地址，拒绝 URL 用户信息、私网/环回/link-local、
凭据查询参数及重定向到禁止目标。解析页、字幕、媒体 CDN 请求均受服务端出站策略约束；
不能只校验起始 URL 后把下载器开放到任意网络。子进程通过受控出站代理，不能直连绕过。
初始上限建议 30 分钟、staging 256 MiB、任务 15 分钟，字幕 5 MiB；受管理端和剩余存储
配额共同限制。平台登录/限制错误如实呈现，不承诺所有公开链接均可抓取。

## 4. 本机任务、API 与验收（待实施）

复用 `WorkspaceStore` 的 revision、SHA 校验、原子发布和 Session 隔离。
`agent_workspace` 定义 source/result/job port；`coding_agent_app/media/` 装配 MinerU、
视频获取及 ASR；Web 仅提供接口和任务状态。耗时处理由本机受控子进程执行，主事件循环
不跑 OCR/ASR。新增用户数据目录下的 `workspace-media.sqlite` 保存任务，不共用 Wiki 表。

拟增接口（沿用登录态、UI 请求标识和 Session 所有权校验）：

- `GET /api/sessions/{sid}/media-capabilities`：图片格式、OCR 档位、视觉能力、视频平台/模式和上限。
- `POST /api/sessions/{sid}/media-jobs`：指定 `source_file_id`、`mode`、可选 MinerU profile；返回 202。
- `POST /api/sessions/{sid}/video-sources`：来源 URL 与模式，保存描述并入队；返回 source + job。
- `GET /api/sessions/{sid}/media-jobs/{id}`：状态、阶段、进度、产物 ID 和安全错误码。
- `POST /api/sessions/{sid}/media-jobs/{id}/cancel`、`/retry`：幂等取消、显式重试。

```text
queued → running → publishing → succeeded
             └→ failed / cancelled / interrupted
```

阶段为 validating / fetching / ocr / transcribing / analyzing；未执行阶段不显示进度。
重启后 queued 可恢复，运行中的任务标记 interrupted，显式重试。终态与事件按 job ID 去重；
缓存键包括 source SHA、模式、配置、解析器/模型版本与视觉 prompt 版本。不同 Workspace 可并行，
默认每 Session 1 个任务，全局 CPU 2 个、GPU 1 个；不会在推理期间持有 Workspace 写锁。
发布前重验来源和当前 revision；无关文件改动可重建发布基线，来源变化则终止且保留旧产物。

产物发布仍限制到本 source 的 `documents/<id>/content.md`、`manifest.json`、`tables/*`
及 `assets/*`，新增媒体类型需要显式 source kind，不能把任意上传都伪装成文档原件。
任务取消、失败或超限不会留下半成品；上传成功与解析成功分别展示；解析完成后刷新文件树，
只有仍属于原 Session 的待发送附件才替换为 `content.md`。Telemetry 记录类型、阶段、耗时、
profile/模型与错误码，不写入图片、URL 查询参数、字幕全文或访问凭据。

实施顺序：图片 OCR 和 Web 视觉接入 → 视频链接及字幕 → 本机语音转录 → 可选画面抽样。
每步都须验证上传/来源保存 → 任务 → 产物 → Agent 读取 → 刷新恢复；验收覆盖 PNG/JPEG/WebP、
缺少视觉/模型能力、两 Session 并发、同名图片、取消和崩溃恢复；视频覆盖人工/自动字幕、
无字幕转录、私有/过期链接、时间戳引用及受控出站失败。真实模型与真实平台代表样例通过前，
能力接口不得返回 ready。
