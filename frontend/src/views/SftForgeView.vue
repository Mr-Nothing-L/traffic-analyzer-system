<script setup lang="ts">
/** SFT 生成面板(右上角独立入口,工作台主区视图):
 * 创建任务卡(勾选视频/指定目录 + max_rounds)→ 连接逐任务 SSE 展示
 * 进度:左侧视频列表(等待/进行中/一致/隔离/跳过徽标 + 轮数),右侧选中
 * 视频的逐轮时间线卡(ForgeRoundCard);终态展示结果面板(统计 + JSONL)。
 * 状态在 stores/sftforge.ts,组件只接线。 */
import { computed, onUnmounted, ref } from 'vue'
import { NButton } from 'naive-ui'
import { useRouter } from 'vue-router'
import ForgeCreateForm from '../components/sftforge/ForgeCreateForm.vue'
import ForgeResultPanel from '../components/sftforge/ForgeResultPanel.vue'
import ForgeRoundCard from '../components/sftforge/ForgeRoundCard.vue'
import UiIcon from '../components/UiIcon.vue'
import { useSftForgeStore } from '../stores/sftforge'
import type { ForgeVideoState } from '../stores/sftforge'
import { videoBaseName } from '../utils/sftforge'

const forge = useSftForgeStore()
const router = useRouter()

const selected = ref<string | null>(null)

/** 选中视频:用户手选优先;默认跟随最新有轮次活动的视频(纠错过程自动呈现)。 */
const current = computed<ForgeVideoState | null>(() => {
  if (selected.value) {
    const v = forge.videos.find((x) => x.video === selected.value)
    if (v) return v
  }
  const active = [...forge.videos].reverse().find((v) => v.rounds.length > 0)
  return active ?? forge.videos[0] ?? null
})

function videoBadge(v: ForgeVideoState): { text: string; cls: string } {
  if (v.summary) {
    if (v.summary.skipped) return { text: '已跳过', cls: 'ff-badge-skip' }
    if (v.summary.match) return { text: '一致', cls: 'ff-badge-match' }
    return { text: '隔离', cls: 'ff-badge-quar' }
  }
  if (v.rounds.length) return { text: '进行中', cls: 'ff-badge-run' }
  return { text: '等待', cls: 'ff-badge-wait' }
}

const statusText = computed(() => {
  if (forge.status === 'running') {
    return `进行中 · 已完成 ${forge.completedCount} / ${forge.videos.length} 个视频`
  }
  if (forge.status === 'done') return '已完成'
  if (forge.status === 'failed') return '失败'
  return ''
})

// 离开面板断开 SSE(服务端任务继续,重进可经新任务重建;事件回放由服务端支持)
onUnmounted(() => forge.reset())
</script>

<template>
  <div class="ff-page">
    <div class="ff-head">
      <h1 class="ff-title">SFT 生成</h1>
      <span v-if="statusText" class="ff-card-sub">{{ statusText }}</span>
      <span class="ff-spacer" />
      <n-button size="small" @click="router.push('/')">
        <template #icon><UiIcon name="up" :size="12" style="transform: rotate(-90deg)" /></template>
        返回树视图
      </n-button>
    </div>

    <ForgeCreateForm />

    <div v-if="forge.videos.length" class="ff-progress">
      <div class="ff-videos" role="listbox" aria-label="任务视频列表">
        <button
          v-for="v in forge.videos"
          :key="v.video"
          class="ff-video-row"
          :class="{ on: current?.video === v.video }"
          role="option"
          :aria-selected="current?.video === v.video"
          :title="v.video"
          @click="selected = v.video"
        >
          <span class="ff-video-name">{{ videoBaseName(v.video) }}</span>
          <span v-if="v.rounds.length" class="ff-video-meta">{{ v.rounds.length }} 轮</span>
          <span class="ff-badge" :class="videoBadge(v).cls">{{ videoBadge(v).text }}</span>
        </button>
      </div>
      <div class="ff-timeline">
        <template v-if="current">
          <div v-if="current.summary?.reason" class="ff-card-sub">
            {{ current.summary.skipped ? '跳过原因' : '隔离原因' }}:{{ current.summary.reason }}
          </div>
          <ForgeRoundCard
            v-for="r in current.rounds"
            :key="`${r.round_no}-${r.kind}`"
            :round="r"
            :target-names="forge.targetNamesOf(r.event_ids)"
            :event-names="forge.eventNames"
          />
          <div v-if="!current.rounds.length" class="ff-empty">等待轮次事件…</div>
        </template>
      </div>
    </div>

    <ForgeResultPanel v-if="forge.result" />
  </div>
</template>

<style scoped>
.ff-head {
  max-width: 1280px;
  display: flex;
  align-items: baseline;
  gap: var(--space-md);
  margin-bottom: var(--space-md);
}

.ff-title {
  font-family: var(--font-pixel);
  font-size: var(--text-2xl);
  font-weight: 650;
  font-style: normal; /* 标题一律 roman(design.md §2) */
  margin: 0;
}

.ff-head .ff-spacer {
  flex: 1;
}
</style>
