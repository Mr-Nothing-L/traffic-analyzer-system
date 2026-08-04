<script setup lang="ts">
/** 逐帧滑块预览:滑块高频 input 用 rAF 节流换帧(序号即时更新,帧图每帧最多换一次)。
 * 迁移自 legacy video_preview.js buildStepper。 */
import { onUnmounted, ref } from 'vue'
import { NButton } from 'naive-ui'
import type { VideoSource } from '../../api/results'
import { frameUrl } from '../../api/results'

const props = defineProps<{ source: VideoSource; total: number; hint: string }>()
const emit = defineEmits<{ retry: [] }>()

const idx = ref(0)
const imgSrc = ref(frameUrl(props.source, 0))
const frameErr = ref(false)

let raf = 0
function onInput(e: Event) {
  idx.value = +(e.target as HTMLInputElement).value
  if (raf) return
  raf = requestAnimationFrame(() => {
    raf = 0
    imgSrc.value = frameUrl(props.source, idx.value)
  })
}
onUnmounted(() => {
  if (raf) cancelAnimationFrame(raf)
})
</script>

<template>
  <div>
    <div class="pv-hint">
      <span>{{ hint }}</span>
      <n-button size="small" quaternary @click="emit('retry')">重试播放</n-button>
    </div>
    <div class="pv-stage">
      <img v-show="!frameErr" :src="imgSrc" alt="帧预览" @load="frameErr = false" @error="frameErr = true" />
      <span v-if="frameErr" class="pv-frame-err">帧读取失败</span>
    </div>
    <div class="pv-slider-row">
      <input type="range" min="0" :max="total - 1" :value="idx" step="1" @input="onInput" />
      <span class="pv-idx">{{ idx }} / {{ total - 1 }}</span>
    </div>
  </div>
</template>
