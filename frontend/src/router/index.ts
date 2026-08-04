import { createRouter, createWebHistory } from 'vue-router'
import DashboardView from '../views/DashboardView.vue'
import DetailView from '../views/DetailView.vue'
import TreeView from '../views/TreeView.vue'
import WelcomeView from '../views/WelcomeView.vue'

// base 与 vite.config.ts 的 base('/v2/')一致:构建后由 FastAPI 挂在 /v2。
// TreeView 是工作台外壳(侧栏常驻),主区为嵌套 router-view:欢迎卡 / 分析详情。
const router = createRouter({
  history: createWebHistory('/v2/'),
  routes: [
    {
      path: '/',
      component: TreeView,
      children: [
        { path: '', name: 'tree', component: WelcomeView },
        { path: 'video/:stem', name: 'detail', component: DetailView },
      ],
    },
    { path: '/dashboard', name: 'dashboard', component: DashboardView },
  ],
})

export default router
