import { defineConfig } from "vite"
import vue from "@vitejs/plugin-vue"
import path from "node:path"

// Vite config —— build 到 ../static，让 FastAPI 直接托管。
// 开发模式下 /api/* 走代理到 127.0.0.1:8000（FastAPI demo）。
export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: path.resolve(__dirname, "../static"),
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
})
