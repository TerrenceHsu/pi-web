import { createApp } from "vue"
import { createPinia } from "pinia"

import App from "./App.vue"
import "./styles.css"

const app = createApp(App)
const pinia = createPinia()
app.use(pinia)
app.mount("#app")

// P1-B2.1: Expose store factory hooks for devtools debugging + Playwright E2E
// event injection.
//
// **B3 前置 hardening**：仅 DEV 模式或显式 VITE_E2E_HOOKS=true 时 expose——
// 普通 production build 不暴露 store 内部状态。
//
// E2E 构建时显式开启：
//   VITE_E2E_HOOKS=true npm run build
// 或者使用 tests/e2e 专用 build 脚本。
//
// 用法（Playwright / devtools）：
//   await page.evaluate(() => {
//     const store = (window as any).__storeHooks.chatStore()
//     store.handleEvent({ ...envelope... })
//   })
const shouldExposeHooks =
  import.meta.env.DEV || import.meta.env.VITE_E2E_HOOKS === "true"

if (shouldExposeHooks && typeof window !== "undefined") {
  // lazy import 避免循环依赖
  void import("./stores/chatStore").then(({ useChatStore }) => {
    ;(window as any).__storeHooks = {
      chatStore: () => useChatStore(),
    }
  })
}
