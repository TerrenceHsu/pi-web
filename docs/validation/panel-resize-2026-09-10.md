# 中栏与聊天栏宽度调节

日期：2026-09-10。本轮沿用既有三栏布局，只调整前端交互，不修改业务 Session、Provider、审批或执行权限。

## 使用方式与边界

- 在桌面三栏视图中，拖动 Workspace 与聊天之间的竖向分隔线；中栏变宽时聊天栏同步缩窄，反之亦然。
- 分隔线宽 8px，含两侧扩展命中区和可见手柄；双击恢复该列默认宽度。
- 聚焦分隔线后，左右键每次调整 16px，Shift + 左右键调整 64px，Home / End 到达最小 / 最大宽度。
- Session 侧栏仍可单独调整（180–440px、默认 260px）；Workspace 默认 380px、最小 280px，
  上限随可用窗口空间变化，不再固定为 760px；聊天至少保留 320px。
- 宽度偏好保存在原有浏览器 localStorage 键中。临时缩小窗口只限制当前显示宽度，不覆盖用户偏好；
  拉大窗口可恢复。存储被禁用或内容非法时仍可使用拖动功能。
- 浏览器视口不超过 1050px 时，继续使用原 Results 抽屉；不强行把三栏挤入窄屏。
- 输入区按聊天栏自身宽度（600px 以下）换成全宽文本框和按钮行；顶部模型/状态区在 800px 以下换行，
  不只依赖浏览器窗口断点，防止宽屏里的窄栏发生遮挡。

## 实现

- `AppShell.vue` 分离偏好尺寸与实际适配尺寸，使用动态边界、Pointer 捕获及指针 ID 匹配。
- 取消、失焦、丢失捕获、视口改变和组件卸载均结束拖动并释放资源；只接受主指针主键。
- 分隔线提供可访问的最小/最大/当前数值与关联面板标识。
- `ChatPanel.vue` / `ChatInput.vue` / `ProviderSelector.vue` 增加局部容器查询，不改变其他面板的全局样式。

## 验证

- 前端 Vitest：40 files / **307 passed**；其中 AppShell 8 项覆盖键盘、存储、指针隔离与卸载清理。
- ESLint、Vue 类型检查、生产构建通过；本机 5173 页面与 AppShell 模块均返回 200。
- 最终布局 / Markdown / Workspace E2E：**8 passed、零重试**。验证真实指针拖动、880px 中栏、
  320px / 640px 聊天栏控件不遮挡、1051px 三栏极限、持久化、900px Results 抽屉和恢复桌面宽度。
  另包含公式与上传/结果的关联回归；生产构建已恢复。
- 截图：`.test-tmp/panel-resize-e2e-final/panel-resize-middle-and-ch-d7ae4-r-keyboard-limits-and-reset-chromium/`
  下的 `workspace-880.png` 与 `chat-minimum-320.png`；已人工检查输入框与顶部布局。
- 额外的真实内嵌 Chromium 关联回归第一批 11 项通过；第二批 10 passed / 1 failed，
  失败发生在弹出标签调宽后的新尺寸高清帧等待，不能把第一批通过当成该问题已解决。
  trace 记录 resize 已返回新尺寸/version 2，但 WebSocket 后续只有新版本心跳、没有对应像素帧；
  问题不在栏宽数值或 ResizeObserver 未发送尺寸。
- 补充 `test_popup_resize_keeps_publishing_current_hd_frames` 隔离 Chromium 回归：**1 passed**，
  16 次 320×712 / 380×726 的 DPR 2 切换均收到精确尺寸 PNG。失败时会记录实际 PNG 尺寸与帧任务状态。
  该次无法复现 Web E2E 停帧，不能认定已修复；保留为已知浏览器问题，不猜测性修改后端或放宽像素检查。

验收只使用临时测试会话和合成网页，不操作用户的日常浏览器或 Google 验证码。
未运行全仓库 Python 门禁或远端 CI，不提交/推送 Git。
