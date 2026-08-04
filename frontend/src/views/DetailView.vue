<script setup lang="ts">
/** 分析详情页:上栏视频预览 + 可拖拽分隔条 + 下栏结果卡
 * (SFT 编辑 / 分析报告 / 证据编辑;推理中显示专家泳道面板)。
 * 编排迁移自 legacy preview.js selectVideo + renderResults。 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute } from 'vue-router'
import { NCard, useDialog, useMessage } from 'naive-ui'
import { videoSourceOf } from '../api/results'
import EvidenceCard from '../components/detail/EvidenceCard.vue'
import ExpertPanel from '../components/detail/expert/ExpertPanel.vue'
import ReportCard from '../components/detail/ReportCard.vue'
import SftEditor from '../components/detail/sft/SftEditor.vue'
import VideoPlayer from '../components/detail/VideoPlayer.vue'
import { useVSplitter } from '../composables/useVSplitter'
import { useEvidenceStore } from '../stores/evidence'
import { useJobsStore } from '../stores/jobs'
import { useSftStore } from '../stores/sft'
import { useWorkspaceStore } from '../stores/workspace'

const route = useRoute()
const ws = useWorkspaceStore()
const jobs = useJobsStore()
const ev = useEvidenceStore()
const sft = useSftStore()
const dialog = useDialog()
const message = useMessage()

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
// 推理完成 → 重载结果;有未保存编辑时不自动重载,避免丢弃草稿(同 legacy hasUnsavedEdits)
watch(
  () => job.value?.status,
  (s, prev) => {
    if (!(s === 'done' && prev && prev !== 'done')) return
    if (sft.dirty || ev.dirty) {
      message.warning(
        `「${rel.value || stem.value}」已重新分析完成,但当前有未保存的修改;请先保存或手动重新加载`,
      )
      return
    }
    ev.load(stem.value)
  },
)
onUnmounted(() => ev.clear())

// 离开详情页/切换视频时 dirty 未保存 → 确认后丢弃(legacy 为静默丢弃,v2 按要求加提示)
function confirmDiscardIfDirty(): boolean | Promise<boolean> {
  if (!sft.dirty) return true
  return new Promise<boolean>((resolve) => {
    dialog.warning({
      title: '未保存的修改',
      content: '当前视频的 SFT 标注有未保存的修改,离开将丢弃这些修改。',
      positiveText: '丢弃并离开',
      negativeText: '继续编辑',
      onPositiveClick: () => resolve(true),
      onNegativeClick: () => resolve(false),
      onClose: () => resolve(false),
    })
  })
}
onBeforeRouteUpdate(() => confirmDiscardIfDirty())
onBeforeRouteLeave(() => confirmDiscardIfDirty())

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
          <SftEditor :stem="stem" :sft="results.sft_label" :file-sig="results.file_sig" />
          <ReportCard :stem="stem" :report-md="results.report_md" />
          <EvidenceCard v-if="results.evidence" :stem="stem" :source="source" />
        </template>
        <ExpertPanel v-else-if="job && job.status === 'running'" :stem="stem" />
        <n-card v-else>
          <div class="empty-note">{{ emptyNote }}</div>
        </n-card>
      </div>
    </div>
  </div>
</template>
