<script setup lang="ts">
import { computed } from 'vue'
/** user 气泡:图片附件 + 视频预览/路径 chip + 指令文本,底部行复制/撤回。 */
import type { AgentEntry } from '../../stores/agentchat'
import type { AgentUserEntry } from '../../stores/agentchat'
import UiIcon from '../UiIcon.vue'
import ChatMessageBase from './ChatMessageBase.vue'

const props = defineProps<{
  entry: AgentEntry
  copied: boolean
  busy: boolean
  videoSrc?: string
  time: string
}>()

const emit = defineEmits<{
  copy: [text: string]
  recall: []
  preview: [url: string]
}>()

const entry = computed(() => props.entry as AgentUserEntry)
</script>

<template>
            <ChatMessageBase user :time="time">
              <div v-if="entry.images?.length" class="img-group">
                <img
                  v-for="(u, j) in entry.images"
                  :key="`${entry.id}:${j}`"
                  :src="u"
                  alt=""
                  loading="lazy"
                  @click="emit('preview', u)"
                />
              </div>
              <video
                v-if="videoSrc"
                class="bubble-video"
                :src="videoSrc"
                controls
                preload="metadata"
              />
              <div v-else-if="entry.videoPath" class="video-chip" :title="entry.videoPath">
                <UiIcon name="video" :size="12" />
                <span class="video-chip-name">{{ entry.videoPath }}</span>
              </div>
              <div class="bubble-text">{{ entry.text }}</div>
              <template #meta>
                <span v-if="entry.steered" class="steer-tag">已插话</span>
              </template>
              <template #actions>
                <button class="msg-act" title="复制" @click="emit('copy', entry.text)">
                  <UiIcon :name="copied ? 'check' : 'copy'" :size="12" />
                </button>
                <button
                  class="msg-act"
                  title="撤回此条及之后的消息"
                  :disabled="busy"
                  @click="emit('recall')"
                >
                  <UiIcon name="undo" :size="12" />
                </button>
              </template>
            </ChatMessageBase>
</template>

<style scoped>
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

/* 「已插话」小标记(steer 注入的 user 气泡,仅本地流式期间存在) */
.steer-tag {
  font-size: var(--text-xs);
  color: var(--color-blue);
  background: var(--color-blue-soft);
  border-radius: var(--radius-sm);
  padding: 0 var(--space-xs);
  font-family: var(--font-pixel); /* 小标 chip → 像素 */
}
/* ---- user 气泡内视频小播放器(上传附件,src=/api/agent/uploads/{name}) ---- */
.bubble-video {
  display: block;
  width: min(320px, 100%);
  max-height: 180px;
  margin-bottom: var(--space-sm);
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  background: var(--color-stage-bg);
}
</style>
