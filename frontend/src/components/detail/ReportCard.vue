<script setup lang="ts">
/** 分析报告卡:report_md → 微型 markdown 渲染(utils/markdown.ts,XSS 先 esc)。
 * 插图经 /api/results/{stem}/file 解析;图片模糊→清晰加载(同 legacy markImgLoaded,
 * load/error 不冒泡,用捕获阶段监听)。 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { NCard } from 'naive-ui'
import { resultFileUrl } from '../../api/results'
import { mdToHtml } from '../../utils/markdown'

const props = defineProps<{ stem: string; reportMd: string | null }>()

const html = computed(() =>
  props.reportMd ? mdToHtml(props.reportMd, (src) => resultFileUrl(props.stem, src)) : '',
)

const body = ref<HTMLElement | null>(null)
function onImgEvent(e: Event) {
  const t = e.target as HTMLElement | null
  if (t && t.tagName === 'IMG') t.classList.add('loaded')
}
onMounted(() => {
  body.value?.addEventListener('load', onImgEvent, true)
  body.value?.addEventListener('error', onImgEvent, true)
})
onBeforeUnmount(() => {
  body.value?.removeEventListener('load', onImgEvent, true)
  body.value?.removeEventListener('error', onImgEvent, true)
})
</script>

<template>
  <n-card class="card-report">
    <template #header><span class="card-head">分析报告</span></template>
    <div v-if="!reportMd" class="empty-note">无分析报告</div>
    <!-- markdown.ts 输出已整体 esc,仅白名单标签,可安全 v-html -->
    <div v-else ref="body" class="report-body" v-html="html" />
  </n-card>
</template>
