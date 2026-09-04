<script setup lang="ts">
/** 详情页「相似视频」入口(同桩位/相似场景 top-5,mode=related&video=当前文件名)。
 * 点击条目切换到该视频详情(同路由参数变化触发重载);加载/空/404(库未建)三态。
 * 竞态:每次加载自增 seq,晚到的过期响应丢弃(同 stores/rag.ts 口径)。 */
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NPopover } from 'naive-ui'
import { ApiError } from '../../api/client'
import { fmtTs, searchRag, stemOfVideoPath } from '../../api/rag'
import type { RagResult } from '../../api/rag'

type Status = 'idle' | 'loading' | 'done' | 'empty' | 'missing' | 'error'

const props = defineProps<{ video: string }>() // 当前视频文件名(带扩展名)

const router = useRouter()
const open = ref(false)
const status = ref<Status>('idle')
const items = ref<RagResult[]>([])
const error = ref('') // missing/error 态的展示文案
let seq = 0

async function load() {
  const my = ++seq
  status.value = 'loading'
  try {
    const resp = await searchRag({ mode: 'related', video: props.video, k: 5 })
    if (my !== seq) return
    items.value = resp.results
    status.value = items.value.length ? 'done' : 'empty'
  } catch (e) {
    if (my !== seq) return
    items.value = []
    if (e instanceof ApiError && e.status === 404) {
      status.value = 'missing' // 检索库未建:detail 即引导文案
      error.value = e.message
    } else {
      status.value = 'error'
      error.value = e instanceof Error ? e.message : String(e)
    }
  }
}

// 每次展开重新加载;切换视频后旧结果作废,重开时再拉
watch(open, (v) => {
  if (v) void load()
})
watch(
  () => props.video,
  () => {
    seq += 1
    items.value = []
    status.value = 'idle'
    if (open.value) void load()
  },
)

function onPick(r: RagResult) {
  open.value = false
  router.push({
    name: 'detail',
    params: { stem: stemOfVideoPath(r.video_path) },
    query: { rel: r.video_path },
  })
}
</script>

<template>
  <n-popover v-model:show="open" trigger="click" placement="bottom-end" class="similar-pop">
    <template #trigger>
      <n-button size="tiny" secondary class="similar-btn" title="同桩位/相似场景 top-5">
        相似视频
      </n-button>
    </template>
    <div v-if="status === 'loading'" class="similar-note">检索中…</div>
    <div v-else-if="status === 'empty'" class="similar-note">无相似视频</div>
    <div v-else-if="status === 'missing'" class="similar-note">
      {{ error }};可在侧栏工具条点「更新向量库」在线构建
    </div>
    <div v-else-if="status === 'error'" class="similar-note">{{ error }}</div>
    <div v-else class="similar-list">
      <div
        v-for="r in items"
        :key="r.video_path + ':' + r.start_ts"
        class="similar-row"
        role="button"
        tabindex="0"
        @click="onPick(r)"
        @keydown.enter.prevent="onPick(r)"
      >
        <span class="similar-score" :title="'相关度 ' + r.score.toFixed(2)">
          {{ r.score.toFixed(2) }}
        </span>
        <div class="similar-meta">
          <div class="similar-name" :title="r.video_path">{{ stemOfVideoPath(r.video_path) }}</div>
          <div class="similar-sub">事件 {{ r.events.join('、') }} · {{ fmtTs(r.start_ts) }}</div>
        </div>
      </div>
    </div>
  </n-popover>
</template>

<style scoped>
.similar-btn {
  margin-left: var(--space-sm);
}

.similar-note {
  max-width: 260px;
  font-size: 12px;
  color: var(--color-text2);
  line-height: 1.6;
}

.similar-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 240px;
}

.similar-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 6px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background var(--dur-fast) var(--ease-out);
}

.similar-row:hover {
  background: var(--color-hover-bg);
}

/* score 徽标:造型对齐 tree.css .badge */
.similar-score {
  flex: 0 0 auto;
  font-size: 12px;
  font-weight: 600;
  padding: 2px 7px;
  border-radius: 999px;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
  background: var(--color-accent-soft);
  color: var(--color-accent);
}

.similar-meta {
  flex: 1;
  min-width: 0;
}

.similar-name {
  font-size: 12.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.similar-sub {
  font-size: 12px;
  color: var(--color-text2);
  font-variant-numeric: tabular-nums;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
