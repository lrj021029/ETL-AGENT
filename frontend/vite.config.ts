/// <reference types="vitest" />
// Vite 与 Vitest 统一构建及测试配置（Tailwind CSS v4 使用官方 Vite 插件）
import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig(({ mode }) => {
  // FastAPI 代理目标：默认 8000；若 8000 被其它程序占用，可在 .env.development.local 设置 ETL_API_PROXY_TARGET
  const apiTarget = loadEnv(mode, fileURLToPath(new URL('.', import.meta.url)), 'ETL_').ETL_API_PROXY_TARGET || 'http://127.0.0.1:8000'
  const apiProxy = {
    '/api': { target: apiTarget, changeOrigin: false },
    '/health': { target: apiTarget, changeOrigin: false },
  }
  return {
  plugins: [vue(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  // 前端端口 5174（用户要求，dev 与 Docker preview 一致）。后端 CORS 只放行 5173 / 4173 且本次不改后端，
  // 因此浏览器只访问同源 5174，由 Vite 把 /api、/health（含 SSE）代理到 FastAPI。
  // 代理目标：本机默认 http://127.0.0.1:8000；Docker 中由 compose 注入 ETL_API_PROXY_TARGET=http://api:8000。
  server: {
    host: '127.0.0.1',
    port: 5174,
    strictPort: true,
    proxy: apiProxy,
  },
  preview: {
    host: '127.0.0.1',
    port: 5174,
    strictPort: true,
    proxy: apiProxy,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/__tests__/**/*.test.ts'],
    setupFiles: ['src/__tests__/setup.ts'],
  },
}
})
