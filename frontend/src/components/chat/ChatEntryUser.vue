<script setup lang="ts">
import { computed } from 'vue'
/** user 气泡:图片附件 + 视频预览/路径 chip + 指令文本,底部行复制/撤回。 */
import type { AgentEntry } from '../../stores/agentchat'
import type { AgentUserEntry } from '../../stores/agentchat'
import UiIcon from '../UiIcon.vue'

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
            <div class="row user">
              <div class="msg-col">
                <div class="bubble">
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
                </div>
                <div class="msg-meta">
                  <span class="msg-time">{{ time }}</span>
                  <span v-if="entry.steered" class="steer-tag">已插话</span>
                  <span class="msg-actions">
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
