import { createRouter, createWebHistory } from 'vue-router'
import AgentChatView from '../views/AgentChatView.vue'
import ChatView from '../views/ChatView.vue'
import DashboardView from '../views/DashboardView.vue'
import DetailView from '../views/DetailView.vue'
import LoginView from '../views/LoginView.vue'
import TreeView from '../views/TreeView.vue'
import WelcomeView from '../views/WelcomeView.vue'

// base 与 vite.config.ts 的 base('/')一致:构建后由 FastAPI 挂在 /。
// /login 是裸布局登录页(无 TopBar/侧栏),与工作台平级。
// TreeView 是工作台外壳(侧栏常驻),主区为嵌套 router-view:欢迎卡 / 分析详情 / 数据看板
// (看板与 legacy openDashboard 一致:只替换主区,左侧文件树常驻)。
const router = createRouter({
  history: createWebHistory('/'),
  routes: [
    { path: '/login', name: 'login', component: LoginView },
    {
      path: '/',
      component: TreeView,
      children: [
        { path: '', name: 'tree', component: WelcomeView },
        { path: 'video/:stem', name: 'detail', component: DetailView },
        { path: 'dashboard', name: 'dashboard', component: DashboardView },
        { path: 'chat', name: 'chat', component: ChatView },
        { path: 'agent', name: 'agent', component: AgentChatView },
      ],
    },
  ],
})

export default router
