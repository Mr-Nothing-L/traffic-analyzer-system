import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './styles/tokens.css'
import './styles/base.css'
import './styles/tree.css'
import './styles/dirpicker.css'
import './styles/quickpick.css'
import './styles/dashboard.css'
import './styles/detail.css'
import './styles/report.css'
import './styles/evidence.css'
import './styles/sft.css'
import './styles/expert.css'
import './styles/login.css'

// 等初始路由解析完成再挂载:/login 裸布局(App.vue 按 route.name 条件渲染 TopBar)
// 依赖首个已解析的路由;否则首帧 route 未就绪会误挂 TopBar。
const app = createApp(App).use(createPinia()).use(router)
router.isReady().then(() => app.mount('#app'))
