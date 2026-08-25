<script setup lang="ts">
/** 思考折叠行的单行摘要:
 * 运行中(live)显示最后一个非空行,并横向跟随滚动到行尾(新进展可见);
 * 结束后显示第一个非空行(思考起点概括),靠 CSS ellipsis 截断。
 * 行内容选取逻辑在 utils/chatDisplay.thinkSummaryLine(纯函数,可直测)。 */
import { computed, nextTick, ref, watch } from 'vue'
import { thinkSummaryLine } from '../../utils/chatDisplay'

const props = defineProps<{
  think: string
  /** true=思考仍在流入(末行 + 横向跟随);false=已结束(首行)。 */
  live: boolean
}>()

const el = ref<HTMLElement | null>(null)
const line = computed(() => thinkSummaryLine(props.think, props.live))

watch(line, async () => {
  if (!props.live) return
  await nextTick()
  // overflow:hidden 下程序滚动仍生效:滚到最右,让最新一行文本的尾部可见
  if (el.value) el.value.scrollLeft = el.value.scrollWidth
})
</script>

<template>
  <div ref="el" class="think-line" :class="{ live }">{{ line }}</div>
</template>

<style scoped>
.think-line {
  width: min(320px, 60vw);
  color: var(--color-text2);
  font-size: var(--text-sm);
  line-height: 1.6;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 运行中:横向程序滚动跟随,不做 ellipsis(滚到行尾时省略号无意义) */
.think-line.live {
  text-overflow: clip;
}
</style>
