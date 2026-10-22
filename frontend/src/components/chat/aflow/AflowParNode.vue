<script setup lang="ts">
/** 并行批节点:同批并发工具横排 chips(「并发」标签 + 逐 chip 折叠/展开明细);
 * chip 折叠态与主干步骤同一套 expandedTools 记忆。 */
import UiIcon from '../../UiIcon.vue'
import ChatToolDetail from '../ChatToolDetail.vue'
import type { Item } from './types'
import { detailVisible } from './expand'

type ParItem = Extract<Item, { t: 'par' }>

const props = defineProps<{ item: ParItem; expandedTools?: Set<string> }>()
const emit = defineEmits<{ toggle: [id: string]; preview: [url: string] }>()
</script>

<template>
  <div class="aflow-node aflow-par">
    <div class="aflow-par-row">
      <span class="aflow-dot dot-par" />
      <span class="aflow-par-tag">并发</span>
      <button
        v-for="(r, qi) in item.rows"
        :key="qi"
        type="button"
        class="aflow-chip"
        :class="[r.state, { 'is-open': item.entries[qi] && expandedTools?.has(item.entries[qi]!.id) }]"
        :title="item.entries[qi] && expandedTools?.has(item.entries[qi]!.id) ? '收起调用明细' : '展开调用明细'"
        @click="item.entries[qi] && emit('toggle', item.entries[qi]!.id)"
      >
        {{ r.label }}
        <b v-if="r.dur" class="aflow-dur">{{ r.dur }}</b>
        <UiIcon v-if="r.retried" name="retry" :size="9" />
      </button>
    </div>
    <template v-for="(e, qi) in item.entries" :key="`det-${qi}`">
      <ChatToolDetail
        v-if="detailVisible(expandedTools, e) && e"
        class="aflow-detail"
        :entry="e"
        @preview="emit('preview', $event)"
      />
    </template>
  </div>
</template>
