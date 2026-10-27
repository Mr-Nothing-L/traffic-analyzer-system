<script setup lang="ts">
/** SFT Forge 结果面板:训练 JSONL 下载/预览(顶部)+ 删除缓存(工作区持久化
 * 结果,见 web/sftforge/store.py)+ 每视频最终结论折叠卡
 * (默认折叠;ForgeFinalView:逐事件检出块,着色表达 检出/未检出/修订并
 * 标注「第 N 轮修正」)+ quarantine 清单(视频 + 原因 + 真值)。
 * 样式类在 styles/sftforge.css(ff-*),颜色全部 token。 */
import { computed, ref } from 'vue'
import { NButton } from 'naive-ui'
import { useSftForgeStore } from '../../stores/sftforge'
import type { ForgeVideoSummary } from '../../api/sftforge'
import { buildJsonlText, forgeOutcomeBadge, videoBaseName } from '../../utils/sftforge'
import ForgeFinalView from './ForgeFinalView.vue'
import UiIcon from '../UiIcon.vue'

const forge = useSftForgeStore()
const jsonlOpen = ref(false)
/** 展开的视频最终结论卡(默认全部折叠)。 */
const openVideos = ref<Set<string>>(new Set())

const jsonlText = computed(() =>
  forge.result ? buildJsonlText(forge.result.jsonl) : '',
)

/** 有最终结论可展示的视频(final_output 非空;含不一致视频,便于排查差异)。 */
const finalVideos = computed(() =>
  (forge.result?.videos || []).filter((v) => !!v.final_output),
)

function toggleVideo(video: string) {
  const next = new Set(openVideos.value)
  if (next.has(video)) next.delete(video)
  else next.add(video)
  openVideos.value = next
}

/** 视频 → 被纠错修订的事件中文名 → 修订轮次(取自该视频逐轮 trace)。 */
function patchedRoundsOf(video: string): Record<string, number> {
  const v = forge.videos.find((x) => x.video === video)
  const out: Record<string, number> = {}
  if (!v) return out
  for (const r of v.rounds) {
    if (r.state !== 'done' || !r.trace?.patched) continue
    for (const n of forge.targetNamesOf(r.event_ids)) out[n] = r.round_no
  }
  return out
}

/** 结果面板视频徽标(与进度区/视频卡同一判定;历史缺 r0_match 时
 * 用该视频逐轮 trace 的 patched 回退「修改后一致」)。 */
function outcomeBadgeOf(v: ForgeVideoSummary): { text: string; cls: string } {
  const state = forge.videos.find((x) => x.video === v.video)
  return forgeOutcomeBadge(
    v,
    state?.rounds.map((r) => ({ round_no: r.round_no, patched: !!r.trace?.patched })),
  )
}

function gtText(gt: number[]): string {
  if (!gt.length) return '无真值'
  return gt.map((id) => forge.eventNames[id] || `事件${id}`).join('、')
}

/** 触发浏览器下载:训练行逐行 JSON 序列化(与 lora 训练文件同格式)。 */
function downloadJsonl() {
  const blob = new Blob([jsonlText.value], { type: 'application/x-ndjson' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `sft_forge_job${forge.jobId ?? ''}.jsonl`
  a.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <section v-if="forge.result" class="ff-card">
    <div class="ff-jsonl-actions">
      <n-button size="small" type="primary" :disabled="!forge.result.jsonl.length" @click="downloadJsonl">
        <template #icon><UiIcon name="download" :size="13" /></template>
        下载训练 JSONL({{ forge.result.jsonl.length }} 行)
      </n-button>
      <button class="ff-fold-btn" type="button" @click="jsonlOpen = !jsonlOpen">
        {{ jsonlOpen ? '收起预览' : '预览 JSONL' }}
      </button>
      <button class="ff-fold-btn" type="button" @click="forge.clearStored()">
        删除缓存
      </button>
      <span class="ff-spacer" />
      <span v-if="forge.result.error" class="ff-form-error" style="padding: 0">
        {{ forge.result.error }}
      </span>
    </div>
    <div v-if="jsonlOpen" class="ff-card-body">
      <pre class="ff-pre">{{ jsonlText || '(无匹配行)' }}</pre>
    </div>
    <div v-for="v in finalVideos" :key="v.video" class="ff-final-video">
      <button class="ff-final-video-head" type="button" @click="toggleVideo(v.video)">
        <span class="ff-quar-name" :title="v.video">{{ videoBaseName(v.video) }}</span>
        <span class="ff-badge" :class="outcomeBadgeOf(v).cls">{{ outcomeBadgeOf(v).text }}</span>
        <span v-if="!v.match && v.reason" class="ff-quar-reason">{{ v.reason }}</span>
        <span class="ff-spacer" />
        <span class="ff-video-toggle">{{ openVideos.has(v.video) ? '收起' : '展开' }}</span>
      </button>
      <ForgeFinalView
        v-if="openVideos.has(v.video)"
        :output="v.final_output || ''"
        :event-names="forge.eventNames"
        :patched-rounds="patchedRoundsOf(v.video)"
      />
    </div>
    <div v-if="forge.result.quarantine.length" class="ff-card-body">
      <div class="ff-label" style="margin-bottom: 6px">不一致视频(达到最大轮次仍未与真值一致)</div>
      <div v-for="q in forge.result.quarantine" :key="q.video" class="ff-quar-row">
        <span class="ff-quar-name">{{ videoBaseName(q.video) }}</span>
        <span class="ff-quar-reason">{{ q.reason || '未说明' }} · 真值:{{ gtText(q.gt) }}</span>
      </div>
    </div>
  </section>
</template>
