<script setup lang="ts">
/** 思考节点:折叠 = 一句话摘要(ThinkLine,实时态末行跟随),展开 = 全文 +
 * 悬停显现复制;展开态由宿主按 assistant 条目 id 记忆(expandedThinks)。 */
import UiIcon from '../../UiIcon.vue'
import ThinkLine from '../ThinkLine.vue'
import type { Item } from './types'

type ThinkItem = Extract<Item, { t: 'think' }>

const props = defineProps<{ item: ThinkItem; open: boolean; copied: boolean }>()
const emit = defineEmits<{ toggle: []; copy: [] }>()
</script>

<template>
  <div class="aflow-node-wrap aflow-think">
    <div class="aflow-think-row">
      <button
        type="button"
        class="aflow-node-btn aflow-think-head"
        :title="open ? '收起思考全文' : '展开思考全文'"
        @click="emit('toggle')"
      >
        <span class="aflow-caret" :class="{ open }">▸</span>
        <span class="aflow-think-lbl">思考过程:</span>
        <span v-if="item.dur" class="aflow-dur">生成 {{ item.dur }}</span>
        <ThinkLine v-if="!open" :think="item.text" :live="item.live" />
      </button>
      <button type="button" class="aflow-think-copy" title="复制思考内容" @click="emit('copy')">
        <UiIcon :name="copied ? 'check' : 'copy'" :size="11" />
      </button>
    </div>
    <div v-if="open" class="aflow-think-text">{{ item.text }}</div>
  </div>
</template>
