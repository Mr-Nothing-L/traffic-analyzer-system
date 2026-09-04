<script setup lang="ts">
/** 工作台外壳:侧栏文件树 + 拖拽分隔条 + 主区(嵌套路由:欢迎卡 / 分析详情)。
 * 迁移自 legacy tree.js renderSidebar + main.js 分隔条。 */
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import TreeNode from '../components/tree/TreeNode.vue'
import RagResultList from '../components/tree/RagResultList.vue'
import TreeToolbar from '../components/tree/TreeToolbar.vue'
import { useEvents } from '../composables/useEvents'
import { useSplitter } from '../composables/useSplitter'
import { useTreeView } from '../composables/useTree'
import type { Job } from '../stores/jobs'
import { useJobsStore } from '../stores/jobs'
import { usePresenceStore } from '../stores/presence'
import { useRagStore } from '../stores/rag'
import { useWorkspaceStore } from '../stores/workspace'

const ws = useWorkspaceStore()
const jobs = useJobsStore()
const presence = usePresenceStore()
const rag = useRagStore()
const route = useRoute()
const { subscribe } = useEvents()
const { width, dragging, onPointerDown, reset } = useSplitter()

// SSE:任务进度/完成增量更新徽标与像素条;presence 事件更新在线徽章
subscribe('job.progress', (d) => jobs.onJobEvent(d as Job))
subscribe('job.done', (d) => {
  jobs.onJobEvent(d as Job)
  ws.refreshTree() // 任务终态:静默对齐 has_results/徽标(不清空树,见 workspace store)
})
subscribe('presence', (d) => presence.setRoster(d))

onMounted(async () => {
  await ws.fetchWorkspace()
  if (ws.hasWorkspace) {
    // 刷新/重开后自动恢复文件树,不再要手动点「加载工作区」;
    // 大工作区加载久的体验由现有 loading 态承载。
    if (!ws.loaded) void ws.loadTree()
    await jobs.pollJobs() // 很轻,照常启动以恢复任务进度(同 legacy)
  }
  // 心跳上报 viewing(当前选中视频);名册由 SSE presence 事件推送
  presence.startHeartbeat(() => ws.currentRel)
})

/** 过滤后整个工作区无匹配视频(勾选状态不受影响)。 */
const { nameMatches } = useTreeView()
const noMatch = computed(
  () => ws.loaded && !!ws.filter && !ws.videos.some((v) => nameMatches(v.name)),
)

/** 详情路由:主区改为上下分栏(去 padding/滚动,由 split-col 自管)。 */
const isDetail = computed(() => route.name === 'detail')
</script>

<template>
  <div class="workbench">
    <aside class="app-sidebar tree-sidebar" :style="{ width: width + 'px', flexBasis: width + 'px' }">
      <TreeToolbar />
      <div class="video-list">
        <!-- 语义检索态:结果列表接管侧栏;清空查询恢复文件树(stores/rag.ts) -->
        <template v-if="rag.active">
          <div v-if="rag.status === 'loading'" class="side-empty">检索中…</div>
          <div v-else-if="rag.status === 'missing'" class="side-empty">
            {{ rag.error }};可点击工具条「更新向量库」按钮在线构建
          </div>
          <div v-else-if="rag.status === 'error'" class="side-empty">
            检索失败:{{ rag.error }}
          </div>
          <div v-else-if="rag.status === 'empty'" class="side-empty">无匹配视频</div>
          <RagResultList v-else :results="rag.results" />
        </template>
        <template v-else>
          <div v-if="!ws.hasWorkspace" class="side-empty">设置工作区后列出文件</div>
          <div v-else-if="!ws.loaded" class="side-empty">尚未加载:请点击主区「加载工作区」</div>
          <div v-else-if="!ws.root.length" class="side-empty">工作区目录为空</div>
          <div v-else-if="noMatch" class="side-empty">无匹配视频</div>
          <TreeNode v-else :entries="ws.root" :depth="0" />
        </template>
      </div>
    </aside>
    <div
      class="splitter"
      :class="{ dragging }"
      title="拖动调整侧栏宽度,双击复位"
      @mousedown="onPointerDown"
      @dblclick="reset"
    />
    <main class="app-main" :class="{ 'app-main-detail': isDetail }">
      <router-view />
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

/* 详情态(上下分栏)样式在 styles/detail.css(.app-main-detail) */
</style>
