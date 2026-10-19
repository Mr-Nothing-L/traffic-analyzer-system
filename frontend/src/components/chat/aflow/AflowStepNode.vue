<script setup lang="ts">
/** 主干步骤节点:label + 耗时 + 状态点(点击整行展开/收起调用明细
 * ChatToolDetail);折叠态按来源工具条目 id 记忆(宿主 expandedTools)。 */
import UiIcon from '../../UiIcon.vue'
import ChatToolDetail from '../ChatToolDetail.vue'
import type { Item } from './types'
import { detailVisible } from './expand'

type StepItem = Extract<Item, { t: 'step' }>

const props = defineProps<{ item: StepItem; expandedTools?: Set<string> }>()
const emit = defineEmits<{ toggle: [id: string]; preview: [url: string] }>()
</script>

<template>
  <div class="aflow-node-wrap">
    <button
      type="button"
      class="aflow-node aflow-node-btn"
      :class="[item.row.state, { 'is-open': item.entry && expandedTools?.has(item.entry.id) }]"
      :title="item.entry && expandedTools?.has(item.entry.id) ? '收起调用明细' : '展开调用明细'"
      @click="item.entry && emit('toggle', item.entry.id)"
    >
      <span class="aflow-dot" />
      <span class="aflow-lbl">{{ item.row.label }}</span>
      <span v-if="item.row.retried" class="aflow-retried" title="同名工具已在后续重试">
        <UiIcon name="retry" :size="10" />
        已重试
      </span>
      <span v-if="item.row.dur" class="aflow-dur">{{ item.row.dur }}</span>
      <span class="aflow-caret" :class="{ open: item.entry && expandedTools?.has(item.entry.id) }">▸</span>
    </button>
    <ChatToolDetail
      v-if="detailVisible(expandedTools, item.entry) && item.entry"
      class="aflow-detail"
      :entry="item.entry"
      @preview="emit('preview', $event)"
    />
  </div>
</template>
