<script setup lang="ts">
/** 说明节点:折叠 = 前两行预览(textPreviewLines),展开 = markdown 全文
 * (MdStream,实时态增量)+ 悬停显现复制;展开态由宿主按 assistant 条目 id
 * 记忆(expandedTexts),与思考节点同机制但各自独立。 */
import UiIcon from '../../UiIcon.vue'
import MdStream from '../MdStream.vue'
import { textPreviewLines } from '../../../utils/chatDisplay'
import type { Item } from './types'

type TextItem = Extract<Item, { t: 'text' }>

const props = defineProps<{ item: TextItem; open: boolean; copied: boolean }>()
const emit = defineEmits<{ toggle: []; copy: [] }>()
</script>

<template>
  <div class="aflow-node-wrap aflow-text">
    <div class="aflow-text-row">
      <button
        type="button"
        class="aflow-node-btn aflow-text-head"
        :title="open ? '收起说明全文' : '展开说明全文'"
        @click="emit('toggle')"
      >
        <span class="aflow-caret" :class="{ open }">▸</span>
        <span class="aflow-text-lbl">说明:</span>
        <span v-if="!open" class="aflow-text-preview">{{ textPreviewLines(item.text) }}</span>
      </button>
      <button type="button" class="aflow-text-copy" title="复制说明内容" @click="emit('copy')">
        <UiIcon :name="copied ? 'check' : 'copy'" :size="11" />
      </button>
    </div>
    <div v-if="open" class="aflow-text-text">
      <MdStream :text="item.text" :streaming="item.live" />
    </div>
  </div>
</template>
