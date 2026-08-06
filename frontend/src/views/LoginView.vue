<script setup lang="ts">
/** 登录页(路由 /login,裸布局:无 TopBar/侧栏)。
 * 逐视觉移植 legacy login.html:四角像素方块、缝合像素字体标题、
 * modal-in 入场、focus-within 升影、输入框 focus 光环(样式见 styles/login.css)。 */
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

const router = useRouter()
const username = ref('')
const password = ref('')
const errorMsg = ref('')
const posting = ref(false)

onMounted(async () => {
  // 已登录(me 200;认证关闭时后端按 local 单用户口径也返回 200)→ 直接回首页。
  // 不用 apiFetch:其 401 会整页跳 /login,在本页会造成刷新循环。
  try {
    const res = await fetch('/api/auth/me')
    if (res.ok) router.replace('/')
  } catch {
    // 网络错误:停留在登录页,提交时再报
  }
})

async function onSubmit() {
  errorMsg.value = ''
  posting.value = true
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: username.value.trim(), password: password.value }),
    })
    if (res.ok) {
      router.replace('/')
      return
    }
    if (res.status === 401) {
      errorMsg.value = '用户名或密码错误'
    } else {
      let detail = `登录失败(HTTP ${res.status})`
      try {
        const j = (await res.json()) as { detail?: unknown }
        if (j?.detail != null) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
      } catch {
        // 非 JSON 响应:保留默认文案
      }
      errorMsg.value = detail
    }
  } catch (err) {
    errorMsg.value = '网络错误:' + (err instanceof Error ? err.message : String(err))
  } finally {
    posting.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <span class="px-deco tl" />
      <span class="px-deco tr" />
      <span class="px-deco bl" />
      <span class="px-deco br" />

      <div class="login-logo">
        <svg
          width="28"
          height="28"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.8"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <rect x="3.5" y="3.5" width="17" height="17" rx="2" />
          <path d="M3.5 9.5h17" />
          <path d="M9.5 9.5V20.5" />
          <path d="M15 3.5v6" />
          <path d="M9.5 15H20.5" />
        </svg>
      </div>
      <h1 class="login-title">高速交通事件分析台</h1>
      <p class="login-sub">请登录后继续</p>

      <form @submit.prevent="onSubmit">
        <div v-if="errorMsg" class="login-error" role="alert">{{ errorMsg }}</div>
        <div class="login-field">
          <label for="login-username">用户名</label>
          <input
            id="login-username"
            v-model="username"
            type="text"
            autocomplete="username"
            spellcheck="false"
            required
            autofocus
          />
        </div>
        <div class="login-field">
          <label for="login-password">密码</label>
          <input
            id="login-password"
            v-model="password"
            type="password"
            autocomplete="current-password"
            required
          />
        </div>
        <button class="login-btn" type="submit" :disabled="posting">
          {{ posting ? '登录中…' : '登 录' }}
        </button>
      </form>
    </div>
  </div>
</template>
