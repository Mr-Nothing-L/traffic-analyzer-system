<script setup lang="ts">
/** assistant 气泡正文的流式增量 markdown 渲染:
 * streaming 期间走 IncrementalMd——按空行切块,除末尾 2 块外的已完成块冻结
 * (HTML 缓存,key 为块源码起始偏移,Vue 复用 DOM 不重挂),只重渲染尾部;
 * 非 streaming(轮次结束/历史条目)一次性 mdToHtml 完整渲染,自愈增量期偏差。
 * 外壳与 .md 重置样式由 MarkdownBody 承载。 */
import { computed } from 'vue'
import { mdToHtml } from '../../utils/markdown'
import { IncrementalMd } from '../../utils/incrementalMd'
import MarkdownBody from './MarkdownBody.vue'

const props = defineProps<{
  text: string
  /** true=流式中(增量渲染);false=已定格(一次性完整渲染)。 */
  streaming: boolean
  /** true=小字上下文(分析链路说明节点):h1-h4 用 text-sm;透传给 MarkdownBody。 */
  compact?: boolean
}>()

// 组件实例跟随一条消息存活(v-for 按条目 id 复用);消息文本只会追加
const renderer = new IncrementalMd()
const parts = computed(() => (props.streaming ? renderer.update(props.text) : null))
const fullHtml = computed(() => (props.streaming ? '' : mdToHtml(props.text)))
</script>

<template>
  <MarkdownBody v-if="parts" class="bubble-text" :compact="compact">
    <div v-for="b in parts.frozen" :key="b.key" v-html="b.html" />
    <div v-html="parts.tailHtml" />
  </MarkdownBody>
  <MarkdownBody v-else class="bubble-text" :compact="compact" :html="fullHtml" />
</template>
