<script setup lang="ts">
/** 结构化选项 chips(封闭枚举,只读选项集):选中态随草稿 attrs 实时联动;
 * 点击/悬停只抛事件,文本同步与 token 联动由父组件编排(同 legacy 分工)。 */
import type { AttrGroup, EventAttrs } from '../../../sft/types'

const props = defineProps<{ groups: AttrGroup[]; attrs: EventAttrs | undefined }>()
const emit = defineEmits<{
  change: [group: AttrGroup, value: string]
  'chip-hover': [group: string | null]
}>()

/** chip 选中态:多选按数组包含,单选按等值(同 legacy applyChipChange 后的重渲染)。 */
function selected(group: AttrGroup, opt: string): boolean {
  const cur = (props.attrs || {})[group.key]
  return Array.isArray(cur) ? cur.indexOf(opt) >= 0 : cur === opt
}
</script>

<template>
  <div class="sft-attrs">
    <div v-for="g in groups" :key="g.key" class="sft-attr-row">
      <span class="answer-key">{{ g.label }}</span>
      <span class="sft-chips">
        <button
          v-for="opt in g.options"
          :key="opt"
          type="button"
          class="sft-chip"
          :class="{ selected: selected(g, opt) }"
          :data-attr="g.key"
          :data-value="opt"
          @click="emit('change', g, opt)"
          @mouseenter="emit('chip-hover', g.key)"
          @mouseleave="emit('chip-hover', null)"
        >
          {{ opt }}
        </button>
      </span>
    </div>
  </div>
</template>
