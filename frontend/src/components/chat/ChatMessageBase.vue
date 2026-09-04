<script setup lang="ts">
/** 消息气泡公共外壳:row(assistant 左侧带头像 / user 右对齐)+ msg-col
 * (bubble + msg-meta:时间戳 + hover 显现的操作组)的结构与样式,
 * ChatEntryUser/ChatEntryAssistant 共用;气泡体、meta 附加标记、操作按钮
 * 等差异内容走 slot。slot 内容的样式(img-group 附件、msg-act 按钮)
 * 由本组件以 :deep 承载。 */
import UiIcon from '../UiIcon.vue'

defineProps<{
  /** true=user 气泡(右对齐 + accent 底色);false=assistant(左侧带头像)。 */
  user?: boolean
  /** HH:MM 时间戳(mono)。 */
  time: string
}>()
</script>

<template>
  <div class="row" :class="user ? 'user' : 'assistant'">
    <div v-if="!user" class="avatar"><UiIcon name="chip" :size="18" /></div>
    <div class="msg-col">
      <div class="bubble"><slot /></div>
      <div class="msg-meta">
        <span class="msg-time">{{ time }}</span>
        <slot name="meta" />
        <span class="msg-actions"><slot name="actions" /></span>
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

/* ---- user 气泡内的图片附件(文字上方,点击进画廊) ---- */
.bubble :deep(.img-group) {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
  margin-bottom: var(--space-sm);
}

.bubble :deep(.img-group img) {
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

.msg-actions :deep(.msg-act) {
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

.msg-actions :deep(.msg-act:hover:not(:disabled)) {
  color: var(--color-accent);
  background: var(--color-hover-bg);
}

.msg-actions :deep(.msg-act:active:not(:disabled)) {
  background: var(--color-accent-soft);
}

.msg-actions :deep(.msg-act:focus-visible) {
  outline: 2px solid var(--color-accent);
  outline-offset: 1px;
}

.msg-actions :deep(.msg-act:disabled) {
  opacity: 0.4;
  cursor: default;
}
</style>
