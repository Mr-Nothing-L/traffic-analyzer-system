<script setup lang="ts">
import { computed } from 'vue'
/** 系统提示条目(压缩/截断警示等),不进历史。 */
import type { AgentEntry, AgentSystemEntry } from '../../stores/agentchat'

const props = defineProps<{
  entry: AgentEntry
}>()

const entry = computed(() => props.entry as AgentSystemEntry)
</script>

<template>
            <div
              class="system-note"
              :class="{ warn: entry.tone === 'warn' }"
            >
              {{ entry.text }}
            </div>
</template>

<style scoped>
/* ---- 系统提示条目(自动压缩等) ---- */
.system-note {
  margin: var(--space-sm) 0;
  text-align: center;
  font-size: var(--text-xs);
  color: var(--color-text2);
}

/* 警示级系统提示(输出截断等):gold 警示色系 */
.system-note.warn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-xs);
  padding: var(--space-xs) var(--space-sm);
  border: 1px solid var(--color-gold);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--color-gold) 10%, var(--color-card));
  color: var(--color-gold);
}
</style>
