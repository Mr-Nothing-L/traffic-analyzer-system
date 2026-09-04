<script setup lang="ts">
/** markdown 正文公共容器:渲染 mdToHtml 产出的 HTML(html prop 一次性渲染,
 * 或经默认 slot 承接 MdStream 的流式增量分块),并统一承载 .md 重置样式
 * (原 ChatEntryAssistant/ChatEntryDetection 的 .bubble-md 与 ChatAnalysisFlow
 * 的 .aflow-text-text 三处 :deep 复制粘贴)。compact=true 用于小字上下文
 * (分析链路说明节点):h1-h4 字号降一档。 */
defineProps<{
  /** 一次性完整渲染的 HTML;不传则渲染默认 slot(流式分块)。 */
  html?: string
  /** true=小字上下文:h1-h4 用 text-sm(原 .aflow-text-text 口径)。 */
  compact?: boolean
}>()
</script>

<template>
  <div v-if="html !== undefined" class="bubble-md" :class="{ compact }" v-html="html" />
  <div v-else class="bubble-md" :class="{ compact }"><slot /></div>
</template>

<style scoped>
/* ---- markdown 正文(mdToHtml 输出的 .md 容器) ---- */
.bubble-md {
  white-space: normal;
}

.bubble-md :deep(.md) > :first-child {
  margin-top: 0;
}

.bubble-md :deep(.md) > :last-child {
  margin-bottom: 0;
}

.bubble-md :deep(.md p),
.bubble-md :deep(.md ul),
.bubble-md :deep(.md ol),
.bubble-md :deep(.md blockquote),
.bubble-md :deep(.md pre),
.bubble-md :deep(.md table) {
  margin: var(--space-xs) 0;
}

.bubble-md :deep(.md h1),
.bubble-md :deep(.md h2),
.bubble-md :deep(.md h3),
.bubble-md :deep(.md h4) {
  margin: var(--space-sm) 0 var(--space-xs);
  font-size: var(--text-md);
}

/* 小字上下文(分析链路说明节点):标题字号降一档 */
.bubble-md.compact :deep(.md h1),
.bubble-md.compact :deep(.md h2),
.bubble-md.compact :deep(.md h3),
.bubble-md.compact :deep(.md h4) {
  font-size: var(--text-sm);
}

.bubble-md :deep(.md ul),
.bubble-md :deep(.md ol) {
  padding-left: var(--space-lg);
}

.bubble-md :deep(.md code) {
  padding: 0 4px;
  border-radius: var(--radius-sm);
  background: var(--color-surface-2);
  border: 1px solid var(--color-border);
  font-size: var(--text-sm);
}

.bubble-md :deep(.md pre) {
  padding: var(--space-sm);
  border-radius: var(--radius-sm);
  background: var(--color-surface-2);
  border: 1px solid var(--color-border);
  overflow-x: auto;
}

.bubble-md :deep(.md pre code) {
  padding: 0;
  border: none;
  background: none;
}

.bubble-md :deep(.md a) {
  color: var(--color-accent);
}

.bubble-md :deep(.md blockquote) {
  padding-left: var(--space-sm);
  border-left: 2px solid var(--color-border);
  color: var(--color-text2);
}

.bubble-md :deep(.md table) {
  border-collapse: collapse;
}

.bubble-md :deep(.md th),
.bubble-md :deep(.md td) {
  padding: 2px var(--space-sm);
  border: 1px solid var(--color-border);
}
</style>
