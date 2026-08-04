<script setup lang="ts">
/** 答案区(ANSWER):天气/时间单行输入 + 场景自适应文本框 + 只读最终结论预览。
 * 结论与 buildRevision 同一数据来源(conclusionLines),随「检出」勾选实时联动。 */
import { computed, nextTick, onMounted, ref } from 'vue'
import { conclusionLines } from '../../../sft/model'
import { useSftStore } from '../../../stores/sft'

const store = useSftStore()
const ENV_KEYS = ['天气', '时间', '场景'] // 固定顺序(同 legacy)

const sceneEl = ref<HTMLTextAreaElement | null>(null)

// 场景文本框自适应高度(同 legacy autoGrow)
const MAX_H = 300
function autoGrow(ta: HTMLTextAreaElement) {
  ta.style.height = 'auto'
  const border = ta.offsetHeight - ta.clientHeight
  const need = ta.scrollHeight + border
  const capped = need > MAX_H
  ta.style.height = (capped ? MAX_H : need) + 'px'
  ta.style.overflowY = capped ? 'auto' : 'hidden'
}

function onSceneInput(e: Event) {
  if (!store.draft) return
  const ta = e.target as HTMLTextAreaElement
  store.draft.env['场景'] = ta.value
  autoGrow(ta)
}

function onEnvInput(k: string, e: Event) {
  if (!store.draft) return
  store.draft.env[k] = (e.target as HTMLInputElement).value
}

interface ConclusionRow {
  key: string | null // classN(等宽 accent 样式);null 为「最终结论」行
  val: string
}

// 只读的最终结论预览(与保存同一口径;逐行解析同 legacy refreshSftConclusion)
const rows = computed<ConclusionRow[]>(() => {
  if (!store.draft || !store.events) return []
  return conclusionLines(store.events, store.draft.checks).map((line) => {
    const m = line.match(/^(class\d+):\s*(.*)$/)
    if (m) return { key: m[1], val: m[2] }
    const m2 = line.match(/^最终结论：([\s\S]*)$/)
    return { key: null, val: m2 ? m2[1] : line }
  })
})

onMounted(() => {
  if (sceneEl.value) nextTick(() => sceneEl.value && autoGrow(sceneEl.value))
})
</script>

<template>
  <template v-if="store.draft">
    <div class="sft-section-title">答案(ANSWER)</div>
    <div class="answer-block">
      <div v-for="k in ENV_KEYS" :key="k" class="answer-row">
        <span class="answer-key">{{ k }}</span>
        <textarea
          v-if="k === '场景'"
          ref="sceneEl"
          class="sft-ev-text answer-env-text"
          rows="2"
          :value="store.draft.env[k] || ''"
          @input="onSceneInput"
        />
        <input
          v-else
          class="answer-input"
          :value="store.draft.env[k] || ''"
          @input="onEnvInput(k, $event)"
        />
      </div>
    </div>
    <div class="answer-block sft-conclusion">
      <div v-for="(r, i) in rows" :key="i" class="answer-row">
        <span class="answer-key" :class="{ 'answer-class': r.key }">{{ r.key || '最终结论' }}</span>
        <span class="answer-val">{{ r.val }}</span>
      </div>
    </div>
  </template>
</template>
