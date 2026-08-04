<script setup lang="ts">
/** 分析详情页:上栏视频预览 + 可拖拽分隔条 + 下栏结果卡
 * (SFT 只读 / 分析报告 / 证据编辑;推理中显示简单进度)。
 * 编排迁移自 legacy preview.js selectVideo + renderResults。 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { NCard } from 'naive-ui'
import { videoSourceOf } from '../api/results'
import EvidenceCard from '../components/detail/EvidenceCard.vue'
import ReportCard from '../components/detail/ReportCard.vue'
import SftCard from '../components/detail/SftCard.vue'
import VideoPlayer from '../components/detail/VideoPlayer.vue'
import PixelBar from '../components/tree/PixelBar.vue'
import { useVSplitter } from '../composables/useVSplitter'
import { useEvidenceStore } from '../stores/evidence'
import { useJobsStore } from '../stores/jobs'
import { useWorkspaceStore } from '../stores/workspace'

const route = useRoute()
const ws = useWorkspaceStore()
const jobs = useJobsStore()
const ev = useEvidenceStore()

const stem = computed(() => String(route.params.stem || ''))
const rel = computed(() => (route.query.rel ? String(route.query.rel) : null))
const source = computed(() => videoSourceOf(stem.value, rel.value))

const colRef = ref<HTMLElement | null>(null)
const { ratio, dragging, onPointerDown, reset } = useVSplitter(colRef)

const results = computed(() => ev.results)
const hasResults = computed(() => {
  const r = results.value
  return !!(r && (r.sft_label || r.report_md || r.evidence))
})
const job = computed(() => jobs.latestJobForStem(stem.value))
const jobFraction = computed(() => job.value?.progress?.fraction ?? null)

async function loadAll() {
  if (!stem.value) return
  if (rel.value) ws.currentRel = rel.value // 行高亮 + presence viewing(深链同样生效)
  ev.clear() // 丢弃上一个视频的草稿,避免幽灵 dirty 态(同 legacy)
  await ev.load(stem.value)
}

onMounted(async () => {
  await jobs.pollJobs() // 很轻,恢复任务进度(同 legacy)
  await loadAll()
})
watch(stem, loadAll)
// 推理完成 → 重载结果(同 legacy 任务完成后重进详情)
watch(
  () => job.value?.status,
  (s, prev) => {
    if (s === 'done' && prev && prev !== 'done') ev.load(stem.value)
  },
)
onUnmounted(() => ev.clear())

const emptyNote = computed(() => {
  const j = job.value
  if (j && j.status === 'queued')
    return '该视频正在推理队列中,完成后此处将展示 SFT 标注、分析报告与证据。'
  if (j && j.status === 'failed')
    return '该视频上次推理未完成(已停止或失败),暂无分析结果。可在左侧点击「重试」,或重新勾选后点击「开始推理」。'
  return '该视频尚未推理,暂无分析结果。在左侧勾选后点击「开始推理」即可分析。'
})
</script>

<template>
  <div ref="colRef" class="split-col">
    <div class="pane-top" :style="{ height: (ratio * 100).toFixed(2) + '%' }">
      <n-card class="card-preview">
        <template #header>
          <span class="card-head">视频预览</span><span class="card-sub">{{ stem }}</span>
        </template>
        <VideoPlayer :source="source" />
      </n-card>
    </div>
    <div
      class="hsplit"
      :class="{ dragging }"
      title="拖动调整预览高度,双击复位"
      @mousedown="onPointerDown"
      @dblclick="reset"
    >
      <span />
    </div>
    <div class="pane-bottom">
      <div class="detail-cards">
        <n-card v-if="ev.loading && !results">
          <div class="empty-note">正在加载结果…</div>
        </n-card>
        <n-card v-else-if="ev.loadError">
          <div class="empty-note">加载结果失败:{{ ev.loadError }}</div>
        </n-card>
        <template v-else-if="hasResults && results">
          <SftCard :stem="stem" :sft="results.sft_label" />
          <ReportCard :stem="stem" :report-md="results.report_md" />
          <EvidenceCard v-if="results.evidence" :stem="stem" :source="source" />
        </template>
        <n-card v-else-if="job && job.status === 'running'" class="card-running">
          <template #header>
            <span class="card-head">推理进行中</span><span class="card-sub">{{ stem }}</span>
          </template>
          <div class="running-card-body">
            <PixelBar :fraction="jobFraction" :running="true" :cells="24" />
            <span class="running-step">{{ job.progress?.step_label || '推理中' }}</span>
          </div>
          <div class="empty-note">完整专家泳道面板迁移中(阶段 5 开放)。</div>
        </n-card>
        <n-card v-else>
          <div class="empty-note">{{ emptyNote }}</div>
        </n-card>
      </div>
    </div>
  </div>
</template>
