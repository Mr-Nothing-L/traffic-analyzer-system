<script setup lang="ts">
/** SFT 标注详情卡(只读):完整编辑器阶段 5 迁移,本阶段展示标注元信息与正文。 */
import { NCard } from 'naive-ui'
import type { SftLabel } from '../../api/results'

defineProps<{ stem: string; sft: SftLabel | null }>()
</script>

<template>
  <n-card class="card-sft">
    <template #header>
      <span class="card-head">SFT 标注详情</span><span class="card-sub">{{ stem }}</span>
    </template>
    <div v-if="!sft" class="empty-note">无 SFT 标注</div>
    <template v-else>
      <div class="sft-meta">
        <span v-if="sft.chunk">片段 <span class="mono">{{ sft.chunk }}</span></span>
        <span v-if="sft.chunk_name">名称 <span class="mono">{{ sft.chunk_name }}</span></span>
        <span v-if="sft.action && sft.action.length">
          事件 <span class="mono">{{ sft.action.join('、') }}</span>
        </span>
        <span v-if="sft.start_timestamp || sft.end_timestamp">
          时间
          <span class="mono">{{ sft.start_timestamp || '?' }} ~ {{ sft.end_timestamp || '?' }}</span>
        </span>
      </div>
      <div v-if="sft.description" class="sft-desc">{{ sft.description }}</div>
      <div class="sft-ro-note">只读展示:标注编辑功能迁移中(阶段 5 开放)。</div>
    </template>
  </n-card>
</template>
