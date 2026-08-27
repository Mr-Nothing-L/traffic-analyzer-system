<script setup lang="ts">
/** assistant 气泡:思考折叠 + markdown 正文,底部行复制。 */
import { computed } from 'vue'
import type { AgentEntry } from '../../stores/agentchat'
import type { AgentAssistantEntry } from '../../stores/agentchat'
import UiIcon from '../UiIcon.vue'
import MdStream from './MdStream.vue'
import ThinkLine from './ThinkLine.vue'

const props = defineProps<{
  entry: AgentEntry
  copied: boolean
  streaming: boolean
  thinkOpen: boolean
  time: string
}>()

const emit = defineEmits<{
  copy: [text: string]
  'toggle-think': []
}>()

const entry = computed(() => props.entry as AgentAssistantEntry)
const isThinkLive = computed(() => props.streaming && !entry.value.text)
</script>

<template>
            <div class="row assistant">
              <div class="avatar"><UiIcon name="chip" :size="18" /></div>
              <div class="msg-col">
                <div class="bubble">
                  <div v-if="entry.think" class="think">
                    <button class="think-head" @click="emit('toggle-think')">
                      <UiIcon
                        name="up"
                        :size="10"
                        class="think-caret"
                        :class="{ open: thinkOpen }"
                      />
                      <span>思考过程</span>
                    </button>
                    <div v-if="thinkOpen" class="think-text">{{ entry.think }}</div>
                    <!-- 折叠态摘要:运行中(思考仍在流入)显示末行并横向跟随滚动,结束后显示首行 -->
                    <ThinkLine v-else :think="entry.think" :live="isThinkLive" />
                  </div>
                  <!-- 正文:流式期间增量渲染(冻结已完成块),定格后一次性完整渲染 -->
                  <MdStream v-if="entry.text" :text="entry.text" :streaming="streaming" />
                </div>
                <div class="msg-meta">
                  <span class="msg-time">{{ time }}</span>
                  <span class="msg-actions">
                    <button class="msg-act" title="复制" @click="emit('copy', entry.text)">
                      <UiIcon :name="copied ? 'check' : 'copy'" :size="12" />
                    </button>
                  </span>
                </div>
              </div>
            </div>
</template>

<style scoped>
.row {
  display: flex;
  align-items: flex-start;
  gap: var(--space-sm);
  margin: var(--space-sm) 0;
}

.row.user {
  justify-content: flex-end;
}

.avatar {
  width: 36px;
  height: 36px;
  flex: 0 0 36px;
  border-radius: 50%;
  border: 1px solid var(--color-border);
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-accent-soft);
  color: var(--color-accent);
}

.bubble {
  max-width: 65%;
  padding: var(--space-sm) var(--space-md);
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  background: var(--color-surface-2);
}

.row.user .bubble {
  background: var(--color-accent-soft);
  border-color: var(--color-accent-deep);
}

.bubble-text {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: var(--text-md);
  line-height: 1.6;
}

.video-chip {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  max-width: 320px;
  margin-bottom: var(--space-xs);
  color: var(--color-text2);
  font-size: var(--text-xs);
}

.video-chip-name {
  font-family: var(--font-mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---- user 气泡内的图片附件(文字上方,点击进画廊) ---- */
.img-group {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
  margin-bottom: var(--space-sm);
}

.img-group img {
  width: 160px;
  height: 107px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  cursor: zoom-in;
}
/* ---- 思考过程折叠 ---- */
.think {
  margin-bottom: var(--space-xs);
}

.think-head {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  padding: 0;
  border: none;
  background: none;
  color: var(--color-text2);
  font-size: var(--text-sm);
  font-family: var(--font-pixel); /* 折叠头(按钮)→ 像素 */
  cursor: pointer;
}

.think-head:hover {
  color: var(--color-accent);
}

.think-caret {
  transform: rotate(180deg); /* 收起:向下 */
  transition: transform 0.15s ease;
}

.think-caret.open {
  transform: rotate(0deg); /* 展开:向上 */
}

.think-text {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--color-text2);
  font-size: var(--text-sm);
  line-height: 1.6;
}
/* ---- 消息底部行:HH:MM + hover 显现的操作按钮组(参考 kimi-code) ---- */
.msg-col {
  display: flex;
  flex-direction: column;
  min-width: 0;
  max-width: 65%;
}

.row.user .msg-col {
  align-items: flex-end;
}

.msg-col .bubble {
  max-width: 100%;
}

.msg-meta {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: 2px var(--space-xs) 0;
}

.msg-time {
  font-size: var(--text-xs);
  color: var(--color-text2);
  font-family: var(--font-mono); /* 时间戳 → 等宽 */
}

.msg-actions {
  display: inline-flex;
  gap: 2px;
  opacity: 0;
  transition: opacity var(--dur-fast) var(--ease-out);
}

.msg-col:hover .msg-actions,
.msg-col:focus-within .msg-actions {
  opacity: 1;
}

.msg-act {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  padding: 0;
  border: none;
  border-radius: var(--radius-sm);
  background: none;
  color: var(--color-text2);
  cursor: pointer;
  transition:
    color var(--dur-fast) var(--ease-out),
    background var(--dur-fast) var(--ease-out);
}

.msg-act:hover:not(:disabled) {
  color: var(--color-accent);
  background: var(--color-hover-bg);
}

.msg-act:active:not(:disabled) {
  background: var(--color-accent-soft);
}

.msg-act:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: 1px;
}

.msg-act:disabled {
  opacity: 0.4;
  cursor: default;
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
