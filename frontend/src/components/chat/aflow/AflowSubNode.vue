<script setup lang="ts">
/** 子代理分支节点:派生子代理的任务描述 + 结论 + 展开后的迷你时间线与
 * 结果明细(ChatToolDetail);折叠态按来源 spawn_subagent 条目 id 记忆。 */
import UiIcon from '../../UiIcon.vue'
import ChatToolDetail from '../ChatToolDetail.vue'
import type { Item } from './types'
import { detailVisible } from './expand'

type SubItem = Extract<Item, { t: 'sub' }>

const props = defineProps<{ item: SubItem; expandedTools?: Set<string> }>()
const emit = defineEmits<{ toggle: [id: string]; preview: [url: string] }>()
</script>

<template>
  <div class="aflow-node aflow-sub" :class="item.row.state">
    <button type="button" class="aflow-sub-head" @click="item.entry && emit('toggle', item.entry.id)">
      <span class="aflow-caret" :class="{ open: item.entry && expandedTools?.has(item.entry.id) }">▸</span>
      <UiIcon name="branch" :size="11" />
      <span class="aflow-lbl">派生子代理</span>
      <span class="aflow-task" :title="item.task">{{ item.task }}</span>
      <span v-if="item.row.state === 'is-run'" class="aflow-run-word">运行中</span>
      <span v-if="item.row.dur" class="aflow-dur">{{ item.row.dur }}</span>
    </button>
    <p v-if="item.conclusion" class="aflow-sub-concl">{{ item.conclusion }}</p>
    <ChatToolDetail
      v-if="detailVisible(expandedTools, item.entry) && item.entry"
      class="aflow-detail aflow-sub-detail"
      :entry="item.entry"
      @preview="emit('preview', $event)"
    />
  </div>
</template>
