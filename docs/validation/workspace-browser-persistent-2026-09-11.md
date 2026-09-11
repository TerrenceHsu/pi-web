# 常驻 1080p 与 YouTube 声音验证

范围：用户要求先稳定常驻 1080p / 目标 30 fps，并检查指定 YouTube 视频无声。
沿用本机专用 Chromium、账号/Session 隔离、公网代理、原输入白名单；不接管日常浏览器。
本记录是定向验收，不是全仓门禁。

## 实现

- 当前优先可见浏览器默认媒体模式；固定 1920×1080，目标 30 fps，默认静音。
- Sound 真实用户手势同步设置接收端静音状态并调用播放；不改网站播放器或系统音量。
- 隐藏停止，返回标签/Session 自动恢复静音；手动 HD 选择和故障状态保存在有界窗口内存。
- 同窗口串行交接单路媒体；两个浏览器分屏跟随焦点，浏览器与 Markdown 分屏不因 Markdown 焦点重启。
- v2 `started` 先确认 generation；停止只针对已拥有的 generation，不能抢停其他窗口的媒体。
- 导航等待采集启动，避免导航撤销尚未消费的 activeTab 授权；启动/清理均有 10 秒上限。
- 已删除目标仅对带固定错误码的 404 确认清理；鉴权失败、暂时不可用或关闭中不当作已释放。
- 迟到的呈现帧回调、健康检查和请求结果不能更新新 generation。

## 定位证据与修正

1. 原按 30 帧请求关键帧的连续 HTML 视频验收失败：30 秒平均 18.23 fps、末段 6.4 fps、
   最后停帧约 2.51 秒；前端音频 RMS 仍约 0.045。改为按时间请求关键帧后，对照运行达到
   平均 26.28 fps、末段 27.1 fps、最后停帧 87 ms、最大缓冲 4.43 秒，音频持续非零。
   原始输出：`.test-tmp/browser-persistent-e2e-0910a/` 与 `...0910c/`。
2. 新标签立即导航会在 `started` 前收到 `browser_media_capture_failed`；运行轨迹显示 resize 后约
   100 ms 导航，随后采集失败。修复前端启动/导航串行化；不是通过自动重试掩盖错误。
3. 静态页的独立采集测试能连续产生数据，但不能证明 MSE 可播放。实际前端诊断显示
   buffered 为 `[0,0.223]`、`[1.177,1.240]`、`[2.183,2.246]` 等稀疏区间；
   7 块全部 ACK、无裁剪/seek/服务端错误，播放器始终 currentTime=0、readyState=1。
   约 8 秒后健康检查正确终止停滞播放。证据：`.test-tmp/browser-persistent-e2e-0911a/`。
   据此增加 Chromium 原生最小刷新率提示，保持静态页编码时间线连续，不延长超时或缓冲上限。

编码策略：`minFrameRate:30 / maxFrameRate:30`、`videoKeyFrameIntervalDuration:1000`。
这些是采集/编码提示，并非真实输出的保证；静态重复刷新帧不等于网页产生了新内容。
UI 帧率取实际呈现回调，不硬编码为 30。

依据：[MediaRecorder 关键帧规则](https://www.w3.org/TR/mediastream-recording/#dom-mediarecorder-start)、
[Chromium WebM 时间长度估计](https://chromium.googlesource.com/chromium/src/media/+/refs/heads/main/formats/webm/webm_cluster_parser.cc)、
[Chromium 最小采集帧率测试](https://chromium.googlesource.com/chromium/src/+/23cf7ae147076df9b36a866d6bc684ee2c297d4a/chrome/browser/media/webrtc/webrtc_desktop_capture_browsertest.cc)。
稀疏区间是本机实测事实；具体容器估算机制的关联是基于上述源码的解释。

## 指定 YouTube 视频

精确地址：[用户指定的视频](https://www.youtube.com/watch?v=CcMPu4Uj50g)。隔离浏览器中的源视频正常播放，
tabCapture 原始音轨峰值 RMS 约 0.492，证明源端有声音；它不能单独证明前端或用户扬声器已出声。
当次源视频自选 804×480；固定 1080p 指浏览器采集画布，不强制 YouTube 选择 1080p 清晰度。

首次前端测试在媒体回退后测得 0 音频，未算通过。新增可选实测必须由真实 Sound 手势解锁，
在最终 HTML video 接收端检测 PCM 与播放时钟。关闭截图、trace、录像；只记录数值，不保存远程视频。
遇到登录/验证码不绕过；网站自身暂停/静音与本机输出设备状态仍需用户分别确认。

## 最终验收

最终代码定向结果：

| 检查 | 结果 |
| --- | --- |
| 后端浏览器/API/媒体/代理/采集相关测试 | 179 passed；最终原生约束与编码参数的 24 项采集子集复验通过 |
| 前端全量单元测试 | 368 passed / 42 files |
| Ruff、Mypy Linux / Win32 | 通过；Mypy 两平台各 320 files |
| 前端 typecheck、lint、正式构建 | 通过；E2E 结束后已恢复生产构建 |
| 端到端联合回归 | 17 passed / 0 retry / 约 3 分钟 |

最终 E2E 输出：`.test-tmp/browser-persistent-e2e-final0911/`，完整 Chromium 接收端，
覆盖默认媒体、静态→运动、Sound/音量、返回/分屏交接、删除/鉴权、Markdown/浏览器、公式、栏宽及上传。

- 静态页连续 **30.09 秒**，同一流未重启，最大缓冲 4.482 秒；随后运动 **10.01 秒**，
  平均 27.79 fps、末 5 秒 29.8 fps、最后停帧 5.4 ms，最大缓冲 5.188 秒。
- 循环 HTML 音视频连续 **30.02 秒**，平均 24.05 fps、末 10 秒 24.7 fps，
  首帧就绪检查约 143 ms、最后停帧 39 ms、最大缓冲 4.514 秒；音频峰值 RMS 0.04568，末段 0.04546。
  该测试包括源视频循环解码、采集编码、接收端与测试观测的共同开销；不将 30 fps 配置当成实测值。
- 指定 YouTube 视频前端接收端通过：**12.00 秒**内播放时钟前进 11.465 秒，
  1920×1080、实测约 25 fps、音频峰值 RMS **0.33271**，145 次采样超过 0.005，
  muted=false、paused=false。分析器最终输出增益为 0，以免测试向宿主外放；证明解码音轨已到达前端，
  不代表检查过用户当前系统扬声器、系统音量或其他软件的静音状态。

复现联合验收：设置 `CI=1`、独立 `E2E_PORT`/`E2E_BASE_URL`、`E2E_PYTHON` 和 `PI_E2E_BROWSER=1`；
只有需要访问上述真实 URL 时再显式设置 `PI_E2E_YOUTUBE_AUDIO=1`。

```powershell
npm --prefix tests/e2e run test:e2e -- browser-persistent.spec.ts browser-media.spec.ts browser-youtube-audio.spec.ts workspace-browser.spec.ts panel-resize.spec.ts workspace-results.spec.ts math-rendering.spec.ts --project=chromium --workers=1 --retries=0
```

本轮变更整理为本地提交，不推送远程。启用方式：使用配套 v2 后端，重启前后端并刷新页面；
重启会中断临时浏览器和登录会话，需重新登录。默认声音关闭，使用时勾选 Sound；返回标签/Session 后需再次开启。
不是全仓门禁或数小时压力测试，不承诺所有站点恒定 30 fps、站点 1080p 视频源、DRM、多路媒体或后台音频。
