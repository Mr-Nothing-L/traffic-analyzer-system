<script setup lang="ts">
/** 顶栏:品牌 / 工作区按钮(打开目录弹窗)/ 开始推理 / 用户区。
 * 迁移自 legacy index.html #toolbar + auth.js 用户区。 */
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NPopover, useMessage } from 'naive-ui'
import { apiFetch } from '../api/client'
import { useAppStore } from '../stores/app'
import type { MeInfo } from '../stores/app'
import { useJobsStore } from '../stores/jobs'
import { useWorkspaceStore } from '../stores/workspace'
import DirPickerModal from './DirPickerModal.vue'
import ModelSwitcher from './ModelSwitcher.vue'
import UiIcon from './UiIcon.vue'

const app = useAppStore()
const ws = useWorkspaceStore()
const jobs = useJobsStore()
const message = useMessage()
const router = useRouter()
const dirPickerOpen = ref(false)

onMounted(async () => {
  try {
    app.setMe(await apiFetch<MeInfo>('/auth/me'))
  } catch {
    app.setMe(null) // 401 已由 client 统一跳登录
  }
})

function fmtLoginTime(ts: number | null): string {
  return ts ? new Date(ts * 1000).toLocaleString('zh-CN') : '-'
}

/** 「开始推理」:对勾选视频提交;409(同 stem 已有任务)友好提示。 */
async function onInfer() {
  const rels = ws.videos.filter((v) => ws.checked.has(v.rel)).map((v) => v.rel)
  if (!rels.length) return
  const r = await jobs.startInfer(rels)
  if (r.ok) message.success(`已提交 ${rels.length} 个推理任务`)
  else if (r.status === 409) message.warning('所选视频已有任务在运行或排队中,请等待完成后再试')
  else message.error(`推理提交失败(${r.status}):${r.message}`)
}

async function onLogout() {
  try {
    await apiFetch('/auth/logout', { method: 'POST' })
  } catch {
    // 登出失败也跳登录页,由服务端会话状态兜底(同 legacy)
  }
  window.location.href = '/login'
}
</script>

<template>
  <header class="app-topbar">
    <span class="tb-logo"><UiIcon name="logo" :size="18" /></span>
    <span class="app-title">高速交通事件分析台</span>
    <button class="ws-btn" title="浏览服务器目录选择工作区" @click="dirPickerOpen = true">
      <UiIcon name="home" :size="14" />
      <span class="ws-label">{{ ws.path ? '工作区' : '选择工作区…' }}</span>
      <span v-if="ws.path" class="ws-path" :title="ws.path">{{ ws.path }}</span>
    </button>
    <span class="tb-spacer" />
    <ModelSwitcher />
    <n-button size="small" @click="router.push('/chat')">对话</n-button>
    <n-button size="small" @click="router.push('/dashboard')">数据看板</n-button>
    <n-button
      type="primary"
      size="small"
      :disabled="!ws.hasWorkspace || ws.checked.size === 0 || jobs.hasActiveInfer"
      :loading="jobs.inferPosting"
      @click="onInfer"
    >
      开始推理
    </n-button>
    <n-popover v-if="app.user" trigger="click" placement="bottom-end">
      <template #trigger>
        <button class="user-avatar" title="账号">{{ (app.user || '?')[0].toUpperCase() }}</button>
      </template>
      <div class="user-pop">
        <div class="user-pop-row">
          <span class="user-pop-key">用户名</span><span>{{ app.user }}</span>
        </div>
        <div class="user-pop-row">
          <span class="user-pop-key">登录时间</span><span>{{ fmtLoginTime(app.loginTs) }}</span>
        </div>
        <div class="user-pop-row">
          <span class="user-pop-key">来源 IP</span><span>{{ app.loginIp || '-' }}</span>
        </div>
        <n-button size="small" class="user-pop-logout" @click="onLogout">登出</n-button>
      </div>
    </n-popover>
    <DirPickerModal v-model:show="dirPickerOpen" />
  </header>
</template>

<style scoped>
.app-topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  height: 52px;
  padding: 0 var(--space-md);
  background: var(--color-card);
  border-bottom: 1px solid var(--color-border);
  box-shadow: var(--shadow);
}

.tb-logo {
  color: var(--color-accent);
  display: inline-flex;
}

.app-title {
  font-size: 24px; /* 对齐 legacy layout.css .tb-title;token 体系无 24px 档,直接写值 */
  font-weight: 650;
  letter-spacing: 0.02em;
}

.ws-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--color-border);
  background: var(--color-card);
  color: var(--color-text);
  border-radius: var(--radius-sm);
  padding: 5px 10px;
  cursor: pointer;
  font-family: var(--font-pixel);
  font-size: var(--text-sm);
  transition:
    border-color var(--dur-fast) var(--ease-out),
    background var(--dur-fast) var(--ease-out);
}

.ws-btn:hover {
  border-color: var(--color-accent);
  background: var(--color-hover-bg);
}

.ws-btn:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: 1px;
}

.ws-btn:active {
  background: var(--color-accent-soft);
}

.ws-label {
  font-weight: 600;
}

.ws-path {
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  color: var(--color-text2);
}

.tb-spacer {
  flex: 1;
}

.user-avatar {
  width: 30px;
  height: 30px;
  border-radius: 50%;
  border: 1px solid var(--color-border);
  background: var(--color-accent-soft);
  color: var(--color-accent);
  font-weight: 650;
  cursor: pointer;
}

.user-avatar:hover {
  border-color: var(--color-accent);
}

.user-avatar:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: 1px;
}

.user-pop {
  min-width: 200px;
}

.user-pop-row {
  display: flex;
  justify-content: space-between;
  gap: var(--space-md);
  font-size: var(--text-sm);
  padding: 2px 0;
}

.user-pop-key {
  color: var(--color-text2);
}

.user-pop-logout {
  margin-top: var(--space-sm);
  width: 100%;
}
</style>
