import { createRouter, createWebHistory } from 'vue-router'
import DashboardView from '../views/DashboardView.vue'
import TreeView from '../views/TreeView.vue'

// base 与 vite.config.ts 的 base('/v2/')一致:构建后由 FastAPI 挂在 /v2。
const router = createRouter({
  history: createWebHistory('/v2/'),
  routes: [
    { path: '/', name: 'tree', component: TreeView },
    { path: '/dashboard', name: 'dashboard', component: DashboardView },
  ],
})

export default router
