<script setup lang="ts">
/** 审批面板:等待审批时接管 composer 输入区(警示色顶条 + 工具/理由/参数
 * 摘要 + 内容预览折叠 + 三键回执);回执后 status 翻回,composer 自动恢复。
 * 自 ChatComposer 拆出,store 直连取「最后一个未决审批」。 */
import { computed, ref } from 'vue'
import { NButton, useMessage } from 'naive-ui'
import type { AgentAccess, AgentApprovalEntry } from '../../stores/agentchat'
import { useAgentChatStore } from '../../stores/agentchat'
import { mdToHtml } from '../../utils/markdown'
import { toolLabel } from '../../utils/chatDisplay'
import UiIcon from '../UiIcon.vue'

const agent = useAgentChatStore()
const message = useMessage()

/* ---- 审批卡:accesses 资源访问摘要(中文操作词) ---- */
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


async function onApprove(requestId: string, decision: 'approved' | 'rejected', scope?: 'session') {
  try {
    await agent.respondApproval(requestId, decision, scope)
  } catch (e) {
    message.error(`审批回执失败:${(e as Error).message}`)
  }
}

/** 待处理的审批请求:仅在等待审批状态下取时间线里最后一个未决审批,
 * composer 据此整体替换为审批面板;回执后状态翻回 running,composer 自动恢复。 */
const pendingApproval = computed<AgentApprovalEntry | null>(() => {
  if (agent.status !== 'awaiting_approval') return null
  for (let i = agent.entries.length - 1; i >= 0; i--) {
    const e = agent.entries[i]
    if (e.kind === 'approval') return !e.decision && !e.stale ? e : null
  }
  return null
})
const approvalPreviewOpen = ref(false)
const approvalPreviewHtml = computed(() => {
  const p = pendingApproval.value?.preview
  if (!p) return ''
  return mdToHtml(`\`\`\`${p.language}\n${p.content}\n\`\`\``)
})
</script>

<template>
      <div v-if="pendingApproval" class="approval-panel">
        <div class="approval-panel-bar">
          <UiIcon name="shield" :size="12" />
          <span>等待审批</span>
        </div>
        <div class="approval-panel-body">
          <div class="approval-panel-tool">
            工具:{{ toolLabel(pendingApproval.toolName) }}
          </div>
          <div class="approval-panel-rule">{{ pendingApproval.approvalRule }}</div>
          <div v-if="pendingApproval.description" class="approval-panel-desc">
            {{ pendingApproval.description }}
          </div>
          <div v-if="pendingApproval.accesses.length" class="approval-accesses">
            <div
              v-for="(a, j) in pendingApproval.accesses"
              :key="j"
              class="approval-access"
            >
              {{ accessLabel(a) }}
            </div>
          </div>
          <div v-if="pendingApproval.preview" class="approval-preview-wrap">
            <button
              class="approval-preview-toggle"
              @click="approvalPreviewOpen = !approvalPreviewOpen"
            >
              <span class="preview-caret" :class="{ open: approvalPreviewOpen }">▸</span>
              <span>内容预览</span>
              <span v-if="pendingApproval.preview.truncated" class="preview-truncated">(已截断)</span>
            </button>
            <div v-if="approvalPreviewOpen" class="approval-preview" v-html="approvalPreviewHtml" />
          </div>
          <div class="approval-panel-actions">
            <n-button
              size="small"
              type="error"
              @click="onApprove(pendingApproval.requestId, 'rejected')"
            >
              拒绝
            </n-button>
            <n-button
              size="small"
              type="primary"
              @click="onApprove(pendingApproval.requestId, 'approved')"
            >
              批准一次
            </n-button>
            <n-button
              size="small"
              @click="onApprove(pendingApproval.requestId, 'approved', 'session')"
            >
              本会话都批准
            </n-button>
          </div>
        </div>
      </div>
</template>

<style scoped>
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

/* ---- composer 审批面板(等待审批时接管输入区;警示色顶条) ---- */
.approval-panel {
  display: flex;
  flex-direction: column;
}

.approval-panel-bar {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  margin: calc(-1 * var(--space-sm)) calc(-1 * var(--space-sm)) 0;
  padding: var(--space-xs) var(--space-sm);
  border-radius: var(--radius) var(--radius) 0 0;
  background: color-mix(in srgb, var(--color-gold) 18%, var(--color-card));
  color: var(--color-gold);
  font-size: var(--text-xs);
  font-weight: 700;
  font-family: var(--font-pixel); /* 状态顶条(chips 同类)→ 像素 */
}

.approval-panel-body {
  padding-top: var(--space-xs);
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
}

.approval-panel-tool {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--color-text);
  font-family: var(--font-pixel); /* 工具条目头 → 像素 */
}

.approval-panel-rule {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--color-text);
  word-break: break-all;
}

.approval-panel-desc {
  font-size: var(--text-xs);
  color: var(--color-text2);
}

.approval-panel-actions {
  display: flex;
  gap: var(--space-sm);
  margin-top: var(--space-xs);
}

.approval-preview-wrap {
  margin-top: var(--space-xs);
}

.approval-preview-toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  padding: 2px var(--space-xs);
  border: none;
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--color-gold) 10%, transparent);
  color: var(--color-gold);
  font-size: var(--text-xs);
  font-weight: 600;
  font-family: var(--font-pixel); /* 按钮 → 像素 */
  cursor: pointer;
}

.approval-preview-toggle:hover {
  background: color-mix(in srgb, var(--color-gold) 18%, transparent);
}

.preview-caret {
  display: inline-block;
  transition: transform var(--dur-fast) var(--ease-out);
}

.preview-caret.open {
  transform: rotate(90deg);
}

.preview-truncated {
  color: var(--color-text2);
  font-weight: 400;
}

.approval-preview {
  margin-top: var(--space-xs);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-surface-2);
  overflow-x: auto;
}

.approval-preview :deep(.md) > pre {
  margin: 0;
  padding: var(--space-sm);
  background: transparent;
  border: none;
}

</style>
