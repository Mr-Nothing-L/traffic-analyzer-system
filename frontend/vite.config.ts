import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 新前端由 FastAPI 挂在 /(绞杀者迁移完成,/login 由 SPA 路由渲染)。
// dev 代理指向本地第二个后端实例(8601;8600 是常驻旧实例,勿动);
// preview 代理用于构建产物的本地端到端验证(后端实例端口按需调整)。
export default defineConfig({
  base: '/',
  plugins: [vue()],
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8601', changeOrigin: true },
      '/fonts': { target: 'http://127.0.0.1:8601', changeOrigin: true },
    },
  },
  preview: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8608', changeOrigin: true },
    },
  },
})
