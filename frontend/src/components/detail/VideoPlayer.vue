<script setup lang="ts">
/** 视频预览卡本体:流播放 → 逐帧降级 → 错误重试(降级链见 useVideoPreview)。
 * 迁移自 legacy video_preview.js mountPreview。 */
import { onMounted, watch } from 'vue'
import { NButton } from 'naive-ui'
import type { VideoSource } from '../../api/results'
import { useVideoPreview } from '../../composables/useVideoPreview'
import FrameSlider from './FrameSlider.vue'

const props = defineProps<{ source: VideoSource }>()
const { mode, hint, frameCount, url, mount, onVideoError, retry } = useVideoPreview()

onMounted(() => mount(props.source))
// stem/rel 变化(切换视频)时重新挂载预览
watch(
  () => props.source.stem + '|' + (props.source.rel ?? ''),
  () => mount(props.source),
)
</script>

<template>
  <div class="preview-body">
    <div v-if="mode === 'video'" class="pv-wrap">
      <video :src="url" controls preload="metadata" playsinline @error="onVideoError" />
    </div>
    <FrameSlider
      v-else-if="mode === 'frames'"
      class="pv-stepper"
      :source="source"
      :total="frameCount"
      :hint="hint"
      @retry="retry"
    />
    <div v-else class="pv-stepper">
      <div class="pv-hint">
        <span>无法读取该视频的帧(文件损坏或编码无法识别)</span>
        <n-button size="small" quaternary @click="retry">重试播放</n-button>
      </div>
    </div>
  </div>
</template>
