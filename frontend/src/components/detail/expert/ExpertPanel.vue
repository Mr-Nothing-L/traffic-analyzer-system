<script setup lang="ts">
/** 专家工作间泳道面板(迁移自 legacy expert_panel.js):泳道 = 专家名 + 阶段标签 +
 * 像素格进度(rAF 驱动逼近/缓行,GET /api/expert-phases 里程碑封顶)。
 * 数据源:SSE 写入的 jobs store(job.progress 快照含泳道,见 web/progress.py)。 */
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { NCard } from 'naive-ui'
import { getExpertPhases } from '../../../api/results'
import { useJobsStore } from '../../../stores/jobs'
import PixelBar from '../../tree/PixelBar.vue'
import UiIcon from '../../UiIcon.vue'
import {
  LANE_CELLS, advanceLane, expertLaneCls, laneCells, phaseText,
} from './laneMath'
import type { ExpertPhases } from './laneMath'

const props = defineProps<{ stem: string }>()
const jobs = useJobsStore()

const job = computed(() => jobs.latestJobForStem(props.stem))
const lanes = computed(() => {
  const ex = job.value?.progress?.experts
  return Array.isArray(ex) ? ex : []
})
const fraction = computed(() => job.value?.progress?.fraction ?? null)
const stepLabel = computed(() => job.value?.progress?.step_label || '')

// 泳道 displayed 值(非 props,按名保留;泳道追加/重建不归零重爬,同 legacy)
const displayed = reactive<Record<string, number>>({})
const shown = ref(false) // 初次插入淡入(CSS opacity 0 → 1,同 legacy)

// 阶段定义缓存(每类别 [{fraction, label}]);404 时记 null,走内置 fallback 封顶(同 legacy)
let phasesPromise: Promise<ExpertPhases> | null = null
function loadPhases(): Promise<ExpertPhases> {
  if (!phasesPromise) {
    phasesPromise = getExpertPhases<{ categories?: ExpertPhases } & ExpertPhases>()
      .then((d) => ((d && d.categories) || d || null) as ExpertPhases)
      .catch(() => null)
  }
  return phasesPromise
}

let phases: ExpertPhases = null
let reduced = false
let lastT = 0
let rafId = 0

function frame(now: number) {
  const dt = Math.min(0.1, (now - lastT) / 1000)
  lastT = now
  const cur = jobs.latestJobForStem(props.stem) // SSE 写入的 jobs 是进度唯一来源
  if (!cur || cur.status !== 'running') return // 终态由 DetailView 切走分支,面板卸载
  lanes.value.forEach((ex) => {
    displayed[ex.name] = advanceLane(displayed[ex.name] ?? 0, ex, dt, phases, reduced)
  })
  rafId = requestAnimationFrame(frame)
}

function onStop() {
  if (job.value) jobs.cancelJob(job.value.id)
}

onMounted(() => {
  reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  loadPhases().then((d) => (phases = d))
  lastT = performance.now()
  rafId = requestAnimationFrame(frame)
  requestAnimationFrame(() => (shown.value = true))
})
onUnmounted(() => {
  if (rafId) cancelAnimationFrame(rafId)
})
</script>

<template>
  <n-card class="card-running">
    <template #header>
      <span class="card-head">推理进行中</span><span class="card-sub">{{ stem }}</span>
    </template>
    <div class="expert-panel" :class="{ shown }">
      <div class="expert-head">
        <span class="expert-step">{{ stepLabel }}</span>
        <span class="mini-wrap" title="总进度">
          <PixelBar :fraction="fraction" :running="true" :cells="8" />
        </span>
        <button type="button" class="stop-btn" title="停止推理" @click="onStop">
          <UiIcon name="stop" /> 停止推理
        </button>
      </div>
      <div v-if="!lanes.length" class="empty-note">等待后端推送专家进度…</div>
      <div v-else class="expert-lanes">
        <div
          v-for="ex in lanes"
          :key="ex.name"
          class="expert-lane"
          :class="[expertLaneCls(ex), { 'lane-judge': ex.name === '裁决' }]"
          :data-lane="ex.name"
        >
          <div class="lane-top">
            <span class="lane-dot" />
            <span class="expert-name" :title="ex.name">{{ ex.name }}</span>
            <span class="expert-phase" :title="phaseText(ex)">{{ phaseText(ex) }}</span>
          </div>
          <div class="pixel-bar">
            <span
              v-for="(cell, i) in laneCells(displayed[ex.name] ?? 0, LANE_CELLS, 3, ex.status === 'running')"
              :key="i"
              class="pixel-cell"
            >
              <span
                v-for="s in 3"
                :key="s"
                class="pixel-sub"
                :class="{ on: s - 1 < cell.lit || s - 1 === cell.frontier, frontier: s - 1 === cell.frontier }"
              />
            </span>
          </div>
        </div>
      </div>
    </div>
  </n-card>
</template>
