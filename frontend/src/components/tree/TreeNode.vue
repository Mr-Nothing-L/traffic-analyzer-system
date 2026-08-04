<script setup lang="ts">
/** 递归树节点(自建,不用 NTree:行内有勾选/徽标/像素条/重试/停止/presence 徽章)。
 * 行为迁移自 legacy tree.js treeRowsHtml + toggleDir。 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
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

/* ---- 目录展开/收起动画(移植自 legacy tree.js:267-290 的 WAAPI 高度+淡入;reduced-motion 折叠为短淡入,design.md §4) ---- */
const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)')

/** 动画参数取自 motion token,与设计系统同源。 */
function motionToken(name: string, fallback: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}
/** token 时长 → ms(兼容 ms/s 写法)。 */
function durMs(name: string, fallback: number) {
  const v = motionToken(name, '')
  return v.endsWith('ms') ? parseFloat(v) : v.endsWith('s') ? parseFloat(v) * 1000 : fallback
}

/** 高度+淡入播放一次;结束或被取消都回调 done。 */
function playKids(kids: HTMLElement, expand: boolean, done: () => void) {
  const h = `${kids.scrollHeight}px`
  const frames = expand
    ? [{ maxHeight: '0px', opacity: 0 }, { maxHeight: h, opacity: 1 }]
    : [{ maxHeight: h, opacity: 1 }, { maxHeight: '0px', opacity: 0 }]
  const opt = { duration: durMs('--dur-med', 200), easing: motionToken('--ease-out', 'ease-out') }
  kids.animate(frames, opt).finished.then(done, done)
}

/** 展开:子层就绪后播高度 0→实际 + 淡入(同 legacy 先渲染后动画;首次展开先等懒加载)。 */
function onKidsEnter(el: Element, done: () => void, rel: string) {
  const kids = el as HTMLElement
  if (reduceMotion.matches) { // 折叠为 ≤150ms 淡入
    kids.animate([{ opacity: 0 }, { opacity: 1 }], { duration: durMs('--dur-fast', 120) })
      .finished.then(done, done)
    return
  }
  if (ws.children[rel]) { playKids(kids, true, done); return }
  const src = () => (ws.expanded.has(rel) ? ws.children[rel] : null)
  const stop = watch(src, async (v) => {
    if (!v) { if (!ws.expanded.has(rel)) { stop(); done() } return } // 等待中被收起/失败:放弃
    stop()
    await nextTick() // 等子层挂载后量真实高度
    playKids(kids, true, done)
  })
}

/** 收起:高度→0 + 淡出;reduced-motion 直接切换(同 legacy 跳过语义)。 */
function onKidsLeave(el: Element, done: () => void) {
  if (reduceMotion.matches) { done(); return }
  playKids(el as HTMLElement, false, done)
}

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
      <!-- 展开/收起:JS hook 驱动 WAAPI 高度+淡入(移植自 legacy tree.js);分片挂载在容器内,不会重复触发 -->
      <Transition :css="false" @enter="(el, done) => onKidsEnter(el, done, row.e.rel)" @leave="onKidsLeave">
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
      </Transition>
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
