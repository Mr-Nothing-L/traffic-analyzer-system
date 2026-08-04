<script setup lang="ts">
/** 工作台主视图:侧栏文件树 + 拖拽分隔条 + 主区欢迎/状态卡。
 * 迁移自 legacy tree.js renderSidebar + preview.js renderWelcome + main.js 分隔条。 */
import { computed, onMounted } from 'vue'
import { NButton, NCard } from 'naive-ui'
import TreeNode from '../components/tree/TreeNode.vue'
import TreeToolbar from '../components/tree/TreeToolbar.vue'
import { useEvents } from '../composables/useEvents'
import { useSplitter } from '../composables/useSplitter'
import { useTreeView } from '../composables/useTree'
import type { Job } from '../stores/jobs'
import { useJobsStore } from '../stores/jobs'
import { usePresenceStore } from '../stores/presence'
import { useWorkspaceStore } from '../stores/workspace'

const ws = useWorkspaceStore()
const jobs = useJobsStore()
const presence = usePresenceStore()
const { subscribe } = useEvents()
const { width, dragging, onPointerDown, reset } = useSplitter()

// SSE:任务进度/完成增量更新徽标与像素条;presence 事件更新在线徽章
subscribe('job.progress', (d) => jobs.onJobEvent(d as Job))
subscribe('job.done', (d) => jobs.onJobEvent(d as Job))
subscribe('presence', (d) => presence.setRoster(d))

onMounted(async () => {
  await ws.fetchWorkspace()
  if (ws.hasWorkspace) await jobs.pollJobs() // 很轻,照常启动以恢复任务进度(同 legacy)
  // 心跳上报 viewing(当前选中视频);名册由 SSE presence 事件推送
  presence.startHeartbeat(() => ws.currentRel)
})

/** 过滤后整个工作区无匹配视频(勾选状态不受影响)。 */
const { nameMatches } = useTreeView()
const noMatch = computed(
  () => ws.loaded && !!ws.filter && !ws.videos.some((v) => nameMatches(v.name)),
)
</script>

<template>
  <div class="workbench">
    <aside class="app-sidebar tree-sidebar" :style="{ width: width + 'px', flexBasis: width + 'px' }">
      <TreeToolbar />
      <div class="video-list">
        <div v-if="!ws.hasWorkspace" class="side-empty">设置工作区后列出文件</div>
        <div v-else-if="!ws.loaded" class="side-empty">尚未加载:请点击主区「加载工作区」</div>
        <div v-else-if="!ws.root.length" class="side-empty">工作区目录为空</div>
        <div v-else-if="noMatch" class="side-empty">无匹配视频</div>
        <TreeNode v-else :entries="ws.root" :depth="0" />
      </div>
    </aside>
    <div
      class="splitter"
      :class="{ dragging }"
      title="拖动调整侧栏宽度,双击复位"
      @mousedown="onPointerDown"
      @dblclick="reset"
    />
    <main class="app-main">
      <!-- 未选择工作区 -->
      <n-card v-if="!ws.hasWorkspace" class="welcome-card">
        <template #header><span class="card-head">高速交通事件分析台</span></template>
        <p>请先点击顶部「选择工作区…」按钮,选择包含视频文件的目录。</p>
      </n-card>
      <!-- 已选择但未加载:显式「加载工作区」(大工作区加载 >10s,同 legacy) -->
      <n-card v-else-if="!ws.loaded" class="welcome-card">
        <template #header><span class="card-head">高速交通事件分析台</span></template>
        <p>当前工作区:<span class="hint-kbd">{{ ws.path }}</span></p>
        <p>
          <n-button type="primary" class="hero-cta" @click="ws.loadTree()">加载工作区</n-button>
        </p>
        <p class="welcome-hint">大工作区加载需要一些时间,请稍候。</p>
      </n-card>
      <!-- 已加载:真实计数 + 操作提示 -->
      <n-card v-else class="welcome-card">
        <template #header><span class="card-head">高速交通事件分析台</span></template>
        <p>当前工作区:<span class="hint-kbd">{{ ws.path }}</span></p>
        <p>共 {{ ws.videos.length }} 个视频,已勾选 {{ ws.checked.size }} 个。</p>
        <p>在左侧勾选视频后点击顶部「开始推理」;点击视频名选中该视频。</p>
        <p v-if="ws.currentRel" class="welcome-hint">
          当前选中:<span class="hint-kbd">{{ ws.currentRel }}</span
          >(预览与结果视图将在后续包迁移)
        </p>
      </n-card>
    </main>
  </div>
</template>

<style scoped>
.workbench {
  flex: 1;
  display: flex;
  min-height: 0;
}

.tree-sidebar {
  background: var(--color-surface-2);
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.video-list {
  flex: 1;
  overflow-y: auto;
  padding: 0 8px 12px;
}

.side-empty {
  color: var(--color-text2);
  font-size: 12px;
  padding: 18px 10px;
  text-align: center;
}

/* ---------- 分隔条(迁移自 legacy layout.css #splitter) ---------- */
.splitter {
  flex: 0 0 5px;
  width: 5px;
  cursor: col-resize;
  background: transparent;
  border-right: 1px solid var(--color-border);
  margin-right: -1px; /* 与侧栏边框重合,避免双线 */
  transition: background var(--dur-fast) var(--ease-out);
  position: relative;
}

.splitter::after {
  content: '';
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 1px;
  height: 56px;
  border-left: 2px solid var(--color-line-strong);
  border-right: 2px solid var(--color-line-strong);
  transition: border-color var(--dur-fast) var(--ease-out);
}

.splitter:hover,
.splitter.dragging {
  background: var(--color-accent-soft);
}

.splitter:hover::after,
.splitter.dragging::after {
  border-color: var(--color-accent);
}

.app-main {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding: var(--space-lg) var(--space-xl) var(--space-2xl);
}

.welcome-card {
  max-width: 640px;
  box-shadow: var(--shadow);
  transition: box-shadow var(--dur-med) var(--ease-out);
}

.welcome-card:hover {
  box-shadow: var(--shadow-hover);
}

.hint-kbd {
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  background: var(--color-surface-3);
  border-radius: var(--radius-sm);
  padding: 1px 6px;
}

.welcome-hint {
  color: var(--color-text2);
  font-size: var(--text-sm);
}

.hero-cta {
  margin: var(--space-xs) 0;
}
</style>
