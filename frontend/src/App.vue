<script setup lang="ts">
import { NConfigProvider, NDialogProvider, NMessageProvider } from 'naive-ui'
import { useRoute } from 'vue-router'
import TopBar from './components/TopBar.vue'
import { themeOverrides } from './theme'

// 登录页为裸布局:不渲染 TopBar(也避免其 /auth/me 轮询在未登录时触发 401 跳转)
const route = useRoute()
</script>

<template>
  <n-config-provider :theme-overrides="themeOverrides">
    <n-message-provider>
      <n-dialog-provider>
        <div class="app-shell">
          <TopBar v-if="route.name !== 'login'" />
          <router-view />
        </div>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<style scoped>
.app-shell {
  height: 100vh;
  display: flex;
  flex-direction: column;
}
</style>
