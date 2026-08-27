<script setup lang="ts">
/** 检测结果卡:检出事件(标注图/未定位小标) + markdown 报告。
 * 二进制编码保留在 payload 契约中(评估/记录用),卡片与报告均不展示。 */
import { computed } from 'vue'
import type { AgentEntry, AgentDetectionEntry, DetectionPayload } from '../../stores/agentchat'
import { mdToHtml } from '../../utils/markdown'

const props = defineProps<{
  entry: AgentEntry
}>()

const entry = computed(() => props.entry as AgentDetectionEntry)

const emit = defineEmits<{
  preview: [url: string]
}>()

function asPayload(data: unknown): DetectionPayload | null {
  return data && typeof data === 'object' ? (data as DetectionPayload) : null
}

const payload = computed(() => asPayload(entry.value.data))
</script>

<template>
            <div class="detection">
              <template v-if="payload">
                <div class="detection-head">
                  <span class="detection-title">检测结果</span>
                  <span
                    v-if="payload!.normal === true"
                    class="detection-badge normal"
                  >正常</span>
                  <span
                    v-else-if="payload!.normal === false"
                    class="detection-badge abnormal"
                  >检出事件</span>
                </div>
                <div
                  v-if="payload!.events?.some((ev) => ev.detected)"
                  class="detection-events"
                >
                  <div
                    v-for="ev in payload!.events!.filter((ev) => ev.detected)"
                    :key="ev.event_id"
                    class="detection-event-item"
                  >
                    <span class="detection-event" :title="ev.reasoning">
                      事件 {{ ev.event_id }}
                    </span>
                    <!-- 逐事件标注图(点击进画廊);无图时显示低调「未定位」小标 -->
                    <img
                      v-if="ev.annotated_image"
                      class="detection-event-img"
                      :src="ev.annotated_image"
                      :alt="`事件 ${ev.event_id} 标注图`"
                      loading="lazy"
                      @click="emit('preview', ev.annotated_image!)"
                    />
                    <span v-else class="detection-event-note unlocated">未定位</span>
                  </div>
                </div>
                <div
                  v-if="payload!.report_markdown"
                  class="detection-report bubble-md"
                  v-html="mdToHtml(payload!.report_markdown!)"
                />
              </template>
              <pre v-else class="detection-raw">{{ String(entry.data ?? '') }}</pre>
            </div>
</template>

<style scoped>
/* ---- 检测结果卡 ---- */
.detection {
  margin: var(--space-sm) 0;
  padding: var(--space-sm) var(--space-md);
  border: 1px solid var(--color-accent);
  border-radius: var(--radius-sm);
  background: var(--color-card);
  box-shadow: var(--shadow);
}

.detection-head {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
}

.detection-title {
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--color-accent);
}

.detection-badge {
  padding: 2px var(--space-sm);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  font-weight: 600;
}

.detection-badge.normal {
  background: var(--color-sage-soft);
  color: var(--color-sage);
}

.detection-badge.abnormal {
  background: var(--color-accent-soft);
  color: var(--color-accent);
}

.detection-events {
  margin-top: var(--space-sm);
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
}

/* 检出事件单元:chip + 标注图(或降级小字)纵向排列 */
.detection-event-item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-xs);
}

.detection-event {
  padding: 2px var(--space-sm);
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-accent);
  background: var(--color-accent-soft);
  color: var(--color-accent);
  font-size: var(--text-xs);
  font-weight: 600;
}

/* 逐事件标注图(submit_detection 服务端生成,点击进画廊) */
.detection-event-img {
  width: 200px;
  height: 130px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  cursor: zoom-in;
}

/* 标注降级小字基类;「未定位」标记另加虚线边(见下方 unlocated 修饰) */
.detection-event-note {
  font-size: var(--text-xs);
  color: var(--color-text2);
}

/* 「未定位」小标:沿用降级小字底子,加虚线边做成低调但可发现的小标 */
.detection-event-note.unlocated {
  padding: 1px var(--space-sm);
  border: 1px dashed var(--color-dot-muted);
  border-radius: var(--radius-sm);
}

.detection-report {
  margin-top: var(--space-sm);
  font-size: var(--text-md);
  line-height: 1.6;
}

.detection-raw {
  margin: var(--space-xs) 0 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  color: var(--color-text2);
}
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
