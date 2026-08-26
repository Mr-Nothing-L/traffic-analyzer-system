<script setup lang="ts">
import { computed } from 'vue'
/** 审批卡片(只读留档):工具/规则/资源访问/结果状态。 */
import type { AgentEntry, AgentApprovalEntry, AgentAccess } from '../../stores/agentchat'
import { toolLabel } from '../../utils/chatDisplay'

const props = defineProps<{
  entry: AgentEntry
}>()

const entry = computed(() => props.entry as AgentApprovalEntry)

const OP_LABEL: Record<string, string> = {
  read: '读取',
  write: '写入',
  readwrite: '读写',
  search: '搜索',
}

function accessLabel(a: AgentAccess): string {
  if (a.kind === 'all') return '全部资源'
  const op = OP_LABEL[a.operation ?? ''] ?? a.operation ?? ''
  return `${op} ${a.path ?? ''}${a.recursive ? '(递归)' : ''}`.trim()
}

const DECISION_LABEL: Record<string, string> = {
  approved: '已批准',
  rejected: '已拒绝',
  approved_session: '本会话已批准',
  cancelled: '已取消',
}
</script>

<template>
            <div class="approval">
              <div class="approval-head">
                <span class="approval-title">审批请求</span>
                <span class="tool-name">{{ entry.toolName }}</span>
              </div>
              <div class="approval-rule">{{ entry.approvalRule }}</div>
              <div v-if="entry.description" class="approval-desc">{{ entry.description }}</div>
              <div v-if="entry.accesses.length" class="approval-accesses">
                <div v-for="(a, j) in entry.accesses" :key="j" class="approval-access">
                  {{ accessLabel(a) }}
                </div>
              </div>
              <div v-if="!entry.decision && !entry.stale" class="approval-decided">
                等待审批(请在下方输入区处理)
              </div>
              <div v-else-if="entry.decision" class="approval-decided">
                {{ DECISION_LABEL[entry.decision] ?? entry.decision }}
              </div>
              <div v-else class="approval-decided">已失效</div>
            </div>
</template>

<style scoped>
/* ---- 审批卡片 ---- */
.approval {
  margin: var(--space-sm) 0;
  padding: var(--space-sm) var(--space-md);
  border: 1px solid var(--color-gold);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--color-gold) 10%, var(--color-card));
}

.approval-head {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
}

.approval-title {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--color-gold);
}

.approval-rule {
  margin-top: var(--space-xs);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  color: var(--color-text);
  word-break: break-all;
}

.approval-desc {
  margin-top: var(--space-xs);
  font-size: var(--text-sm);
  color: var(--color-text2);
}

.approval-accesses {
  margin-top: var(--space-xs);
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.approval-access {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--color-text2);
  word-break: break-all;
}

.approval-decided {
  margin-top: var(--space-sm);
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--color-text2);
}
</style>
