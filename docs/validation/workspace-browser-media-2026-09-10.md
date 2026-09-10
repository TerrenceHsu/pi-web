# Workspace 浏览器停帧恢复与单标签媒体验证

日期：2026-09-10。以下记录提交前的隔离验收；验收时未提交、未推送，未重启用户后端或替换其 Chromium。
前端构建与隔离测试不代表正在运行的旧后端已经加载新代码；后端修复需下次重启生效。

## 已交付的代码

- `PageFrames` 对旧尺寸、截图失败、静默源和 resize 配置失败增加有界处理；每个视口版本最多两次重建。
  恢复耗尽后固定 `browser_capture_failed`，释放监听并通知订阅者；自动重连不重置预算，显式 Reconnect 可重试。
- 前端只在当前尺寸的新画面解码成功后显示 Live；心跳不伪装为新帧，旧版本回包丢弃。
  标签切换时等待尚未完成的 resize，避免提前建立新流；隐藏/显示不绕过终态错误。
- REST 捕获错误返回固定 503；修复继承凭证路由导致浏览器异常被错误吞为通用 500 的问题。
  验证错误、HTTP 异常与未知异常均清洗详情和异常响应头，保留 no-store/nosniff。
- 网关每 10 秒复核连接原始登录 token、单次超时 5 秒；失效/查询失败停止流并执行 finally，
  改密码仍即时撤销；一个登录退出不影响同账户另一个独立登录。
- 公网代理保持鉴权、DNS/IP 钉住、64 连接和 10 秒握手；180 秒改为双向成功写入共享的空闲期限，
  独立 6 小时硬上限和 10 秒写超时，取消/超时释放双向任务。

## 验证

- 相关后端：`test_web_browser.py`、`test_web_browser_stream.py`、`test_web_browser_proxy_lifetime.py`、
  `test_web_browser_capture_errors.py`、`test_web_auth.py` 合并运行 **84 passed**，无 coverage 全仓推断。
- 真实 Chromium：`test_web_browser_geometry_live.py` **4 passed**，覆盖五组像素几何/DPR、
  连续 16 次 popup resize、真实 CDP 旧尺寸污染和截图异常注入。
- 前端 Vitest **312 passed**；typecheck、ESLint、正式构建及相关源码 Ruff/Mypy 通过。
- Web E2E：`workspace-browser`、`panel-resize`、`workspace-results`、`math-rendering` 最终 **11 passed**，
  40.0 秒、0 retry，包含此前失败的弹出标签调宽；正式前端构建已恢复。早期批次不累加。
  存在 Windows 连接关闭 10054 日志及 Playwright NO_COLOR 提示，不将通过描述为零日志告警。

命令示例（PowerShell，Chromium 使用隔离合成站点，不调用真实 Provider）：

```powershell
$env:PYTHONPATH='src'
D:\miniconda\envs\pipy\python.exe -m pytest tests/test_web_browser.py tests/test_web_browser_stream.py tests/test_web_browser_proxy_lifetime.py tests/test_web_browser_capture_errors.py tests/test_web_auth.py --no-cov --basetemp .test-tmp/braf0910 -q
```

E2E 截图输出：`.test-tmp/browser-recovery-e2e-0910/`；历史失败 trace 未删除。

## 媒体试验：不能计作产品已经支持有声视频

1. 已安装的 Chrome / HeadlessChrome **151.0.7922.34** 均返回
   `Page.startScreenRecording wasn't found`。
2. 从 [Google 官方稳定版列表](https://googlechromelabs.github.io/chrome-for-testing/) 下载
   **153.0.8010.36** 到 `.test-tmp/browser-media-probe/chrome153/`，未安装到系统或修改生产路径。
   对本机合成 canvas/tone 进行 1080p/30 配置录制，0.5 秒轮询；首读只有 36 字节 ftyp，
   约 2.5 秒后出现 moov/媒体片段，之后边录边读成立。停止录制并关闭 IO handle、关闭测试浏览器。
   此处未测浏览器实际解码、音频非零值或持续稳定 30 fps。
3. 原生录制路线暂不采纳为长期低延迟通道：官方实现使用软件 AV1＋Opus，视频关键帧约每
   `frame_rate × 2` 帧；片段在关键帧边界输出，数据持续追加临时文件，读取不裁剪已消费前缀。
   这是源码判断，实际不同版本仍需运行验证。
   依据：[编码服务](https://raw.githubusercontent.com/chromium/chromium/main/content/services/devtools_media_encoding_service/devtools_media_encoding_service_impl.cc)、
   [MP4 分片](https://raw.githubusercontent.com/chromium/chromium/main/media/muxers/mp4_muxer.cc)、
   [临时流文件](https://raw.githubusercontent.com/chromium/chromium/main/content/browser/devtools/devtools_stream_file.cc)。
4. 扩展备用探针位于 `.test-tmp/browser-tab-capture-probe/`，仅在新建临时 profile 加载本地最小扩展。
   直接调用、普通扩展页点击或 openPopup 均不能授予目标 activeTab；浏览器级 CDP 对**顶层 tab target**
   的 `Extensions.triggerAction` 成功后，取得视频轨道 1920×1080、frameRate 30、
   音频双声道 48kHz。最终对照移除 `--enable-unsafe-extension-debugging` 后仍成功，故不需要新增此参数；
   早期 Method not allowed 是页面级 CDP 与浏览器级 CDP 的调用位置差异。只使用内部调试管道，未开放端口，
   生产仍未加载扩展。接口参考：[CDP Extensions](https://chromedevtools.github.io/devtools-protocol/tot/Extensions/)。
5. 所有试验都保留宿主静音，因此音轨存在不等于已经有声。
   `--mute-audio` 在 PCM 读取阶段清零，简单移除又可能产生宿主外放；后续必须单独验证标签采集与
   本地输出抑制的组合，不使用系统混音或麦克风。
   依据：[Chromium SyncReader](https://raw.githubusercontent.com/chromium/chromium/main/services/audio/sync_reader.cc)。

## 前一阶段交接（当时尚需确认）

引入项目专用扩展的标签采集权限是新的运行权限边界，未自动加入生产；无需 unsafe 调试启动参数。
确认后需要实现精确 Session/page/tab 绑定（不能按 URL 猜测）、非零音频且不外放、实时传输、
多路径回收和真实性能测量。后台音频、多标签媒体与 60 fps 以该单标签闭环为前置，仍未完成。

## 用户确认“下一步”后的阶段 2 实施

上述原生录制和静音探针是历史试验，不代表最终媒体实现。当前新增：

- 固定项目 MV3 扩展、精确 Session/page/tab 身份桥接，完整 Chromium 151、Playwright 1.62；
  无日常 profile、远程 CDP 端口、unsafe 调试参数、host/content-script 权限。
- 标签视频与 Opus 音轨通过认证媒体 WebSocket 保序传给 MSE；单账号一路，固定 1080p、目标 30 fps。
  加入用户播放解锁、静音、音量、停止及图片模式恢复。点击沿用原 REST 白名单和固定源坐标校验。
- 单块/队列/播放缓存/操作时长有界；源静默、观看期限和 ACK 超时回收。启动失败、取消与关闭失败
  不提前释放占用名额；后台重试，确认轨道或实际上下文关闭后才允许新媒体。
- 修复 MediaRecorder 首个分片只有一个 EBML 字节的情况：保序拼够头部再发送，不丢弃任何前缀字节。
- 启动时间线间隙可能令音频继续而视频停在约 11 帧。默认和强制关键帧配置均曾复现，
  不能简单归因为某个 GOP 参数。采用有界同播放器 seek 恢复，按真实呈现帧判断是否恢复；
  每 30 帧关键帧辅助历史裁剪，不强行重写音视频时间戳为 sequence 模式。
- 修复旧页面导航等待输入队列后跨 Session 误发：恢复前核对 generation，新增先失败后通过的回归。

### 最终实现的分层验证

- 相关后端合并运行 **191 passed / 24.26 s**，包括原 84 项以及媒体协议、生命周期和扩展桥接；
  不将旧批次重复累加，不代表全仓门禁或覆盖率。
- 真实 Chromium 合并 **5 passed / 29.56 s**：媒体持续传输、同 URL 两个 Session 的独立 generation、
  单路配额、停止/删除释放，加原 4 项像素几何/旧帧/resize 故障注入。
  本批首媒体块 **641 ms**，30 块 **108,312 bytes**；首块时间不等于前端首个已呈现帧时间。
- 前端 **336 passed**；typecheck、ESLint、浏览器相关 Ruff 和 strict Mypy（11 个文件）通过。
- 提交前补查全仓 Ruff（src/tests/scripts/evals）及 Linux/Windows 两个平台 Mypy（各 320 源文件）通过；
  仅修正合成媒体测试页 CSS 的长行换行，不改行为。这不等于重新运行全量测试门禁。
- 扩展 4 个静态资产按 Hatch 现有 packages 配置纳入包路径、无匹配忽略规则；
  本环境缺 hatchling/build，未额外安装，因此本轮没有 wheel archive 实测，不将配置审查当打包验收。

### 声音和连续性证据

- 独立真实标签采集探针在 `--disable-audio-output` 下读取 12 秒增量数据，96 块、1,581,925 bytes，
  解码音频 RMS **0.0568034**；`local_echo=false`、采集 active、停止后所有轨道 ended 且目标重新静音。
  不使用系统混音或麦克风；探针声源是内存中的合成 tone。
- 宿主虚拟输出依据完整 Chromium 的实际启动参数与官方源码路径；未获得有效音频 histogram，
  不声称操作系统声卡探针已证明。`--disable-audio-output` 转为 `AUDIO_FAKE`，假输出仍消费 PCM；
  音轨不清零，也不开普通硬件输出流。依据：[参数转交](https://chromium.googlesource.com/chromium/src/+/HEAD/content/browser/service_host/utility_process_host.cc)、
  [音频流选择](https://chromium.googlesource.com/chromium/src/+/HEAD/media/audio/audio_manager_base.cc)、
  [虚拟输出](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/media/audio/fake_audio_output_stream.cc)。
  DRM/原生受保护播放路径未验证，不作通用无外放保证。
- 独立 MSE 探针使用与生产相同的保序队列和约 3 秒历史裁剪：30 秒正常播放 **28.04 fps**，
  尾 10 秒 **28.4 fps**、RMS **0.05669**、最大缓存 **4.153 s**、25 次裁剪，无恢复触发。
- 注入启动时 250 ms 画面暂停而音轨连续：实际间隙 **0.398→0.700 s**，一次同步跳转到 0.710 s；
  30 秒整体 **26.79 fps**，尾 10 秒 **29 fps**，RMS **0.056818**，最大缓存 **4.321 s**，
  26 次裁剪、末尾停帧 **12 ms**。这验证故障恢复，不将丢失的源画面伪算成帧。
  保留间隙后时间戳，裁剪需要随机访问点；参考 [MSE coded frame removal](https://www.w3.org/TR/media-source-2/#sourcebuffer-coded-frame-removal)。

### Web 页面端到端验收

同一后端 Chromium 151 采集，同一页面操作与严格 30 秒断言：

| 前端解码环境 | 整体 / 尾 10 秒 fps | 音频 RMS | 最大缓冲 | 首帧等待 | 结果 |
|---|---:|---:|---:|---:|---|
| 完整 Chromium 151 | 29.49 / 28.7 | 0.045878 | 4.637 s | 2,504 ms | 2 passed / 53.6 s |
| 完整 Chromium 149 | 29.49 / 28.6 | 0.045718 | 4.670 s | 2,631 ms | 2 passed / 57.4 s |
| 最终完整 Chromium 149 媒体回归 | 29.53 / 28.9 | 0.045827 | 4.537 s | 1,098 ms | 纳入最终 13 项回归 |

覆盖真实帧呈现、用户在远端页面点击启动 tone、前端解码后的非零声音、静音、音量、
停止恢复图片、再次启动、关闭页面、未登录访问拒绝与 Session 删除。
为避免测试外放，前端分析节点接固定零增益输出；测量点位于零增益前，音频时钟正常运行。
没有用录制配置或首末帧间隔冒充帧率：整个 30 秒窗口与尾段均要求 >=20 fps，
末尾无帧须 <1 秒，持续采样缓存 <=8.5 秒；本批生产目标仍为 30 fps。

旧的默认 headless shell 149 未通过：间隙恢复后短暂恢复到 28 fps，但随后供包停顿/突发，
最终按 8 秒缓冲上限停止。完整 149 与完整 151 均通过，说明不能简单归因于版本号。
媒体 E2E 现明确采用 `channel: chromium` 的完整浏览器，与实际 Web 用户目标一致；
不降低断言、不调整安全上限来通过。独立 Chromium 内部根因未定，不承诺所有运行模式都兼容。
失败 trace 保留于 `.test-tmp/browser-media-e2e-0910e/`，两组成功输出目录为 `...0910f/` 和 `...0910g/`。

最终运行 `browser-media`、`workspace-browser`、`panel-resize`、`workspace-results`、`math-rendering`：
**13 passed / 1.5 min / 0 retry**，输出 `.test-tmp/browser-media-e2e-final0910/`。
前端正式构建随后成功恢复；保留既有 Windows 连接关闭 10054 日志与 NO_COLOR 提示，
不描述为零日志告警。命令示例：

```powershell
$env:CI='1'
$env:E2E_PORT='8227'
$env:E2E_BASE_URL='http://127.0.0.1:8227'
$env:E2E_PYTHON='D:/miniconda/envs/pipy/python.exe'
$env:PI_E2E_BROWSER='1'
npm --prefix tests/e2e run test:e2e -- browser-media.spec.ts workspace-browser.spec.ts panel-resize.spec.ts workspace-results.spec.ts math-rendering.spec.ts --project=chromium --workers=1 --retries=0
```

### 当前边界

上述隔离验收完成时尚未重启生产后端，也未提交或推送；部署需重启后端并刷新 Web，不能以测试进程代替。
不保证恒定 30 fps、网站公网速度或 DRM；60 fps、多路媒体与显式后台音频留在阶段 3。
当前合成 tone 证明链路有声和点击可交互，不等于精密口型同步或各种视频编解码器矩阵验收。
网站网络延迟和不同内容负载未被合成验收覆盖；不将上述数字宣传为普通外网站点的速度保证。
