<script setup lang="ts">
/** 递归树节点(自建,不用 NTree:行内有勾选/徽标/像素条/重试/停止/presence 徽章)。
 * 行为迁移自 legacy tree.js treeRowsHtml + toggleDir。 */
import { computed } from 'vue'
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

const visible = computed(() => tree.viewEntries(props.entries))

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
  const v = ws.videos.find((x) => x.rel === rel)
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

function fractionOf(e: TreeEntry): number | null {
  const job = jobs.latestJobForStem(tree.videoFor(e).stem)
  return job && job.progress ? job.progress.fraction : null
}
</script>

<template>
  <template v-for="e in visible" :key="e.rel">
    <!-- 目录行 + 子层容器(包在同一 v-if 分支,保证 v-else-if 链只按类型分派) -->
    <template v-if="e.type === 'dir'">
      <div
        class="tree-row tree-dir"
        role="button"
        tabindex="0"
        :style="pad(depth)"
        @click="onToggleDir(e.rel)"
        @keydown.enter.prevent="onToggleDir(e.rel)"
        @keydown.space.prevent="onToggleDir(e.rel)"
      >
        <span class="tree-caret" :class="{ open: ws.expanded.has(e.rel) }">▸</span>
        <span class="tree-ico"><UiIcon name="folder" :size="12" /></span>
        <span class="tree-name" :title="e.rel">{{ e.name }}</span>
      </div>
      <div v-if="ws.expanded.has(e.rel)" class="tree-kids">
        <TreeNode
          v-if="ws.children[e.rel] && ws.children[e.rel].length"
          :entries="ws.children[e.rel]"
          :depth="depth + 1"
        />
        <div v-else class="tree-empty" :style="pad(depth + 1)">
          {{ ws.children[e.rel] ? '空目录' : '加载中…' }}
        </div>
      </div>
    </template>

    <!-- 视频行:勾选 + 状态徽标/像素条 + presence 徽章 -->
    <div
      v-else-if="e.is_video"
      class="video-item"
      :class="{ active: ws.currentRel === e.rel }"
      role="button"
      tabindex="0"
      :style="pad(depth)"
      @click="onSelect(e.rel)"
      @keydown.enter.prevent="onSelect(e.rel)"
    >
      <input
        type="checkbox"
        :checked="ws.checked.has(e.rel)"
        @click.stop
        @change="onCheck(e.rel, $event)"
      />
      <span class="tree-ico"><UiIcon name="video" :size="12" /></span>
      <div class="video-meta">
        <div class="video-name file-name" :title="e.rel">{{ e.name }}</div>
        <div class="video-sub">{{ fmtBytes(e.size) }}</div>
      </div>
      <span
        v-for="b in presence.badgesFor(e.rel, app.user)"
        :key="b.kind + b.name"
        class="presence-badge"
        :class="b.kind === 'editing' ? 'presence-editing' : 'presence-viewing'"
        :title="b.name + (b.kind === 'editing' ? ' 正在编辑' : ' 正在查看')"
      >
        <UiIcon v-if="b.kind === 'editing'" name="edit" :size="11" /> {{ b.name }}
      </span>
      <!-- 运行中:迷你像素条 + 行内停止键 -->
      <template v-if="tree.videoStatus(tree.videoFor(e)).cls === 'st-running'">
        <PixelBar :fraction="fractionOf(e)" :running="true" />
        <button class="stop-btn" title="停止推理" @click.stop="onStop(e)">
          <UiIcon name="stop" :size="11" />
        </button>
      </template>
      <template v-else>
        <span class="badge" :class="tree.videoStatus(tree.videoFor(e)).cls">
          <svg
            v-if="tree.videoStatus(tree.videoFor(e)).cls === 'st-done'"
            class="badge-check"
            viewBox="0 0 12 12"
            aria-hidden="true"
          >
            <path d="M2.5 6.4 5 8.9 9.5 3.6" />
          </svg>
          {{ tree.videoStatus(tree.videoFor(e)).text }}
        </span>
        <button
          v-if="tree.videoStatus(tree.videoFor(e)).cls === 'st-failed'"
          class="retry-btn"
          title="重新推理"
          @click.stop="onRetry(e.rel)"
        >
          <UiIcon name="retry" :size="11" />
        </button>
      </template>
    </div>

    <!-- 非视频文件:仅展示 -->
    <div v-else class="tree-row tree-file" :style="pad(depth)" :title="e.rel">
      <span class="tree-caret"></span>
      <span class="tree-ico"><UiIcon name="file" :size="12" /></span>
      <span class="tree-name">{{ e.name }}</span>
    </div>
  </template>
</template>
