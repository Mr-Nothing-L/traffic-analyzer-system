<script setup lang="ts">
/** 递归树节点(自建,不用 NTree:行内有勾选/徽标/像素条/重试/停止/presence 徽章)。
 * 行为迁移自 legacy tree.js treeRowsHtml + toggleDir。 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { useAppStore } from '../../stores/app'
import { useJobsStore } from '../../stores/jobs'
import { usePresenceStore } from '../../stores/presence'
import type { TreeEntry } from '../../stores/workspace'
import { useWorkspaceStore } from '../../stores/workspace'
import { fmtBytes, useTreeView } from '../../composables/useTree'
import PixelBar from './PixelBar.vue'
import UiIcon from '../UiIcon.vue'

const props = defineProps<{ entries: TreeEntry[]; depth: number }>()

const app = useAppStore()
const ws = useWorkspaceStore()
const jobs = useJobsStore()
const presence = usePresenceStore()
const tree = useTreeView()
const message = useMessage()
const router = useRouter()

/** 全量渲染行(过滤+排序+视频行状态/进度预取,每行只算一次);visible 按分片截断后真正挂载。 */
const rows = computed(() => tree.viewRows(props.entries))

/* ---- 大目录分片渲染:单层 >BATCH 行时按批增量挂载,避免一次性渲染卡顿 ---- */
const BATCH = 100
const limit = ref(BATCH) // 当前放行挂载的行数
const visible = computed(() => rows.value.slice(0, limit.value))

let batchTimer: ReturnType<typeof setTimeout> | null = null

/** 每帧放行一批直至覆盖全量(只截断渲染,过滤/排序/勾选语义不受影响)。 */
function scheduleBatch() {
  if (batchTimer || limit.value >= rows.value.length) return
  batchTimer = setTimeout(() => {
    batchTimer = null
    limit.value = Math.min(limit.value + BATCH, rows.value.length)
    scheduleBatch()
  }, 16) // ≈1 帧:小步放行,保持交互响应
}

watch(rows, (r) => {
  if (r.length <= BATCH) limit.value = r.length // 小目录/过滤收窄:直接全量
  else if (limit.value < BATCH) limit.value = BATCH // 从小列表切回大列表:重新爬坡
  else if (limit.value > r.length) limit.value = r.length // 大列表收窄:不隐藏已见行
  scheduleBatch()
})

onMounted(scheduleBatch) // 挂载时 rows 即大目录(如任务完成后保留展开重载)也要爬坡
onBeforeUnmount(() => {
  if (batchTimer) clearTimeout(batchTimer)
})

function pad(d: number) {
  return { paddingLeft: `${8 + d * 14}px` } // 每级 14px 缩进(同 legacy)
}

async function onToggleDir(rel: string) {
  try {
    await ws.toggleDir(rel)
  } catch (e) {
    const err = e as { status?: number; message?: string }
    message.error(`读取目录失败(${err.status ?? '?'}):${err.message ?? e}`)
  }
}

function onCheck(rel: string, ev: Event) {
  ws.setChecked(rel, (ev.target as HTMLInputElement).checked)
}

/** 点视频行:选中(行高亮 + presence viewing)并进分析详情页。 */
function onSelect(rel: string) {
  ws.currentRel = rel
  const v = ws.videoByRel.get(rel) // O(1) 索引
  // 全量列表缺失时由文件名退 stem(同 legacy tree.js 合成逻辑)
  const stem = v ? v.stem : rel.split('/').pop()!.replace(/\.[^.]+$/, '')
  router.push({ name: 'detail', params: { stem }, query: { rel } })
}

/** 行内「■ 停止」:取该视频最新任务的 id(运行中行必有 running job)。 */
async function onStop(e: TreeEntry) {
  const job = jobs.latestJobForStem(tree.videoFor(e).stem)
  if (!job) return
  const r = await jobs.cancelJob(job.id)
  if (r.ok) message.success('已请求停止推理')
  else message.error(`停止推理失败(${r.status}):${r.message}`)
}

/** 失败徽标旁 ↻ 重试:仅对该视频重新提交;409 友好提示。 */
async function onRetry(rel: string) {
  const r = await jobs.retryInfer(rel)
  if (r.ok) message.success(`已重新提交推理:${rel}`)
  else if (r.status === 409) message.warning('该视频已有任务在运行或排队中,请等待完成后再试')
  else message.error(`重试提交失败(${r.status}):${r.message}`)
}
</script>

<template>
  <template v-for="row in visible" :key="row.e.rel">
    <!-- 目录行 + 子层容器(包在同一 v-if 分支,保证 v-else-if 链只按类型分派) -->
    <template v-if="row.e.type === 'dir'">
      <div
        class="tree-row tree-dir"
        role="button"
        tabindex="0"
        :style="pad(depth)"
        @click="onToggleDir(row.e.rel)"
        @keydown.enter.prevent="onToggleDir(row.e.rel)"
        @keydown.space.prevent="onToggleDir(row.e.rel)"
      >
        <span class="tree-caret" :class="{ open: ws.expanded.has(row.e.rel) }">▸</span>
        <span class="tree-ico"><UiIcon name="folder" :size="12" /></span>
        <span class="tree-name" :title="row.e.rel">{{ row.e.name }}</span>
      </div>
      <div v-if="ws.expanded.has(row.e.rel)" class="tree-kids">
        <TreeNode
          v-if="ws.children[row.e.rel] && ws.children[row.e.rel].length"
          :entries="ws.children[row.e.rel]"
          :depth="depth + 1"
        />
        <div v-else class="tree-empty" :style="pad(depth + 1)">
          {{ ws.children[row.e.rel] ? '空目录' : '加载中…' }}
        </div>
      </div>
    </template>

    <!-- 视频行:勾选 + 状态徽标/像素条 + presence 徽章(row.status 已预取,每行只算一次) -->
    <div
      v-else-if="row.e.is_video"
      class="video-item"
      :class="{ active: ws.currentRel === row.e.rel }"
      role="button"
      tabindex="0"
      :style="pad(depth)"
      @click="onSelect(row.e.rel)"
      @keydown.enter.prevent="onSelect(row.e.rel)"
    >
      <input
        type="checkbox"
        :checked="ws.checked.has(row.e.rel)"
        @click.stop
        @change="onCheck(row.e.rel, $event)"
      />
      <span class="tree-ico"><UiIcon name="video" :size="12" /></span>
      <div class="video-meta">
        <div class="video-name file-name" :title="row.e.rel">{{ row.e.name }}</div>
        <div class="video-sub">{{ fmtBytes(row.e.size) }}</div>
      </div>
      <span
        v-for="b in presence.badgesFor(row.e.rel, app.user)"
        :key="b.kind + b.name"
        class="presence-badge"
        :class="b.kind === 'editing' ? 'presence-editing' : 'presence-viewing'"
        :title="b.name + (b.kind === 'editing' ? ' 正在编辑' : ' 正在查看')"
      >
        <UiIcon v-if="b.kind === 'editing'" name="edit" :size="11" /> {{ b.name }}
      </span>
      <!-- 运行中:迷你像素条 + 行内停止键 -->
      <template v-if="row.status?.cls === 'st-running'">
        <PixelBar :fraction="row.fraction" :running="true" />
        <button class="stop-btn" title="停止推理" @click.stop="onStop(row.e)">
          <UiIcon name="stop" :size="11" />
        </button>
      </template>
      <template v-else>
        <span class="badge" :class="row.status?.cls">
          <svg
            v-if="row.status?.cls === 'st-done'"
            class="badge-check"
            viewBox="0 0 12 12"
            aria-hidden="true"
          >
            <path d="M2.5 6.4 5 8.9 9.5 3.6" />
          </svg>
          {{ row.status?.text }}
        </span>
        <button
          v-if="row.status?.cls === 'st-failed'"
          class="retry-btn"
          title="重新推理"
          @click.stop="onRetry(row.e.rel)"
        >
          <UiIcon name="retry" :size="11" />
        </button>
      </template>
    </div>

    <!-- 非视频文件:仅展示 -->
    <div v-else class="tree-row tree-file" :style="pad(depth)" :title="row.e.rel">
      <span class="tree-caret"></span>
      <span class="tree-ico"><UiIcon name="file" :size="12" /></span>
      <span class="tree-name">{{ row.e.name }}</span>
    </div>
  </template>
</template>
