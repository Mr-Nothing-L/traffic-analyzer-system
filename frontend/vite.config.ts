import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 新前端由 FastAPI 挂在 /(绞杀者迁移完成,/login 由 SPA 路由渲染)。
//
// 本地端口角色:
//   - 8600: FastAPI web 默认端口(常驻旧实例,勿动)。
//   - 8601: Python toolserver 默认端口(视频工具服务,agent 内部访问,勿作为前端代理目标)。
//   - 8602: TS agent server 默认端口(由 web 层 /api/agent/* 反向代理)。
//   - 8608: 前端 preview / e2e 常用后端端口。
//
// dev 代理原指向 8601,会与默认 toolserver 冲突,故改为 8608。
// 若你习惯在 8600 起第二个 web 实例做 dev 后端,请把下面 target 改成该实例端口,
// 但务必避开 8601(toolserver)。
export default defineConfig({
  base: '/',
  plugins: [vue()],
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8608', changeOrigin: true },
      '/fonts': { target: 'http://127.0.0.1:8608', changeOrigin: true },
    },
  },
  preview: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8608', changeOrigin: true },
    },
  },
})
