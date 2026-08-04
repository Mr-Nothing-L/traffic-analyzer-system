import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 新前端由 FastAPI 挂在 /v2(绞杀者迁移,旧 SPA 仍在 /)。
// dev 代理指向本地第二个后端实例(8601;8600 是常驻旧实例,勿动)。
export default defineConfig({
  base: '/v2/',
  plugins: [vue()],
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8601', changeOrigin: true },
      '/fonts': { target: 'http://127.0.0.1:8601', changeOrigin: true },
    },
  },
})
