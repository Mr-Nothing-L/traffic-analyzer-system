<script setup lang="ts">
/** SFT Forge 结果面板:汇总统计(match/quarantine/skipped)+ quarantine
 * 清单(视频 + 原因 + 真值)+ 训练 JSONL 预览(折叠)与下载(.jsonl)。 */
import { computed, ref } from 'vue'
import { NButton } from 'naive-ui'
import { useSftForgeStore } from '../../stores/sftforge'
import { buildJsonlText, videoBaseName } from '../../utils/sftforge'
import UiIcon from '../UiIcon.vue'

const forge = useSftForgeStore()
const jsonlOpen = ref(false)

const jsonlText = computed(() =>
  forge.result ? buildJsonlText(forge.result.jsonl) : '',
)

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
    <div class="ff-card-head">
      <span class="ff-card-title">生成结果</span>
      <span class="ff-card-sub">任务 #{{ forge.result.job_id }}</span>
      <span class="ff-spacer" />
      <span v-if="forge.result.error" class="ff-form-error" style="padding: 0">
        {{ forge.result.error }}
      </span>
    </div>
    <div class="ff-stats">
      <span class="ff-stat ff-stat-match">匹配<b>{{ forge.result.stats.match }}</b></span>
      <span class="ff-stat ff-stat-quar">隔离<b>{{ forge.result.stats.quarantine }}</b></span>
      <span class="ff-stat ff-stat-skip">跳过<b>{{ forge.result.stats.skipped }}</b></span>
    </div>
    <div v-if="forge.result.quarantine.length" class="ff-card-body">
      <div class="ff-label" style="margin-bottom: 6px">隔离视频(达到最大轮次仍未与真值一致)</div>
      <div v-for="q in forge.result.quarantine" :key="q.video" class="ff-quar-row">
        <span class="ff-quar-name">{{ videoBaseName(q.video) }}</span>
        <span class="ff-quar-reason">{{ q.reason || '未说明' }} · 真值:{{ gtText(q.gt) }}</span>
      </div>
    </div>
    <div class="ff-jsonl-actions">
      <n-button size="small" type="primary" :disabled="!forge.result.jsonl.length" @click="downloadJsonl">
        <template #icon><UiIcon name="download" :size="13" /></template>
        下载训练 JSONL({{ forge.result.jsonl.length }} 行)
      </n-button>
      <button class="ff-fold-btn" type="button" @click="jsonlOpen = !jsonlOpen">
        {{ jsonlOpen ? '收起预览' : '预览 JSONL' }}
      </button>
    </div>
    <div v-if="jsonlOpen" class="ff-card-body">
      <pre class="ff-pre">{{ jsonlText || '(无匹配行)' }}</pre>
    </div>
    <div v-else style="padding-bottom: 14px" />
  </section>
</template>
