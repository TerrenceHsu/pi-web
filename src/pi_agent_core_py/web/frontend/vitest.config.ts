import path from "node:path"

import vue from "@vitejs/plugin-vue"
import { defineConfig } from "vitest/config"

// 独立 vitest 配置——与 vite.config.ts 解耦。
// - jsdom 环境（DOM API 可用）
// - 复用 @vitejs/plugin-vue 处理 SFC
// - test 匹配 tests/unit/**/*.spec.ts
// - 默认不启用真实网络
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  test: {
    environment: "jsdom",
    include: ["tests/unit/**/*.spec.ts"],
    clearMocks: true,
    restoreMocks: true,
  },
})
