<script setup lang="ts">
/** SFT Forge 单视频卡片:该视频的全部轮次折叠在一张卡片内(头部:文件名
 * + 状态徽标 + 轮数 + 折叠开关;进行中默认展开,video_completed 后自动
 * 折叠,可手动再展开)。卡内为纠错过程:跳过/不一致原因、初稿(批量链路)
 * 步骤进度、逐轮小节(ForgeRoundCard);最终结论移到结果面板
 * (ForgeResultPanel),不在卡内重复。
 * 样式类在 styles/sftforge.css(ff-*),颜色全部 token。 */
import { computed, ref, watch } from 'vue'
import type { ForgeVideoState } from '../../stores/sftforge'
import { forgeOutcomeBadge, videoBaseName } from '../../utils/sftforge'
import ForgeRoundCard from './ForgeRoundCard.vue'

const props = defineProps<{
  videoState: ForgeVideoState
  /** 事件 id → 中文名。 */
  eventNames: Record<number, string>
  /** 轮次目标事件中文名集合(store.targetNamesOf)。 */
  targetNamesOf: (eventIds: number[]) => Set<string>
}>()

// 进行中默认展开;完成(含重进页面恢复的历史状态)默认折叠;
// 运行中 summary 到达时自动折叠(可手动再展开)
const open = ref(!props.videoState.summary)
watch(
  () => props.videoState.summary,
  (s) => {
    if (s) open.value = false
  },
)

const badge = computed(() => {
  const v = props.videoState
  if (v.summary) {
    return forgeOutcomeBadge(
      v.summary,
      v.rounds.map((r) => ({ round_no: r.round_no, patched: !!r.trace?.patched })),
    )
  }
  if (v.rounds.length) return { text: '进行中', cls: 'ff-badge-run' }
  return { text: '等待', cls: 'ff-badge-wait' }
})

/** 初稿(批量链路)步骤行:step→[x/4] 步骤名;lane_done→专家泳道终态;
 * register→泳道注册;其余(phase/start 等高频事件)折叠不展示。 */
const draftLines = computed(() => {
  const lines: { key: string; text: string; tone: string }[] = []
  for (const s of props.videoState.draftSteps) {
    if (s.type === 'register') {
      lines.push({ key: `reg-${s.total}`, text: `初稿链路启动:${s.total} 个泳道(10 事件专家 + 裁决)`, tone: 'info' })
    } else if (s.type === 'step') {
      lines.push({ key: `step-${s.step}-${s.name}`, text: `[${s.step}/${s.total}] ${s.name}`, tone: 'info' })
    } else if (s.type === 'lane_done') {
      const label =
        s.result === 'detected' ? '检出'
        : s.result === 'undetected' ? '未检出'
        : s.result === 'error' ? '出错'
        : '完成'
      const tone =
        s.result === 'detected' ? 'hit' : s.result === 'error' ? 'bad' : 'info'
      lines.push({ key: `lane-${s.lane}-${s.result}`, text: `${s.lane}:${label}`, tone })
    }
  }
  return lines
})
</script>

<template>
  <section class="ff-video-card">
    <button class="ff-video-card-head" type="button" @click="open = !open">
      <span class="ff-video-name" :title="videoState.video">{{ videoBaseName(videoState.video) }}</span>
      <span v-if="videoState.rounds.length" class="ff-video-meta">{{ videoState.rounds.length }} 轮</span>
      <span class="ff-badge" :class="badge.cls">{{ badge.text }}</span>
      <span class="ff-spacer" />
      <span class="ff-video-toggle">{{ open ? '收起' : '展开' }}</span>
    </button>
    <div v-if="open" class="ff-video-card-body">
      <div v-if="videoState.summary?.reason" class="ff-card-sub">
        {{ videoState.summary.skipped ? '跳过原因' : '不一致原因' }}:{{ videoState.summary.reason }}
      </div>
      <div v-if="draftLines.length" class="ff-draft">
        <span class="ff-para-label">初稿(批量链路:抽帧 → 事件专家 → 裁决 → SFT 改写)</span>
        <div
          v-for="l in draftLines"
          :key="l.key"
          class="ff-draft-line"
          :class="`ff-draft-${l.tone}`"
        >
          {{ l.text }}
        </div>
      </div>
      <ForgeRoundCard
        v-for="r in videoState.rounds"
        :key="`${r.round_no}-${r.kind}`"
        :round="r"
        :target-names="targetNamesOf(r.event_ids)"
        :event-names="eventNames"
      />
      <div v-if="!videoState.rounds.length" class="ff-empty">等待轮次事件…</div>
    </div>
  </section>
</template>
