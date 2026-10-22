<script setup lang="ts">
/** SFT Forge 单轮卡片:轮次号 + kind 徽标 + 目标事件标签 + 状态徽标
 * (进行中/已合并修改/未产生修改)+ prompt 折叠 + 响应段落(纠错目标事件的
 * 段落按 kind 配色高亮,即「修复段落 diff 高亮」;无 <think> 结构时回退原文)。
 * 样式类在 styles/sftforge.css(ff-*),颜色全部 token。 */
import { computed, ref } from 'vue'
import type { ForgeRoundEntry } from '../../stores/sftforge'
import { forgeKindCls, forgeKindLabel, thinkParagraphs } from '../../utils/sftforge'

const props = defineProps<{
  round: ForgeRoundEntry
  /** 本轮目标事件中文名集合(段落高亮命中集)。 */
  targetNames: Set<string>
  /** 事件 id → 中文名(标签展示)。 */
  eventNames: Record<number, string>
}>()

const promptOpen = ref(false)

const kindCls = computed(() => forgeKindCls(props.round.kind))
const kindLabel = computed(() => forgeKindLabel(props.round.kind))
const eventTags = computed(() =>
  props.round.event_ids.map((id) => props.eventNames[id] || `事件${id}`),
)
/** 段落视图:有 <think> 结构时按段落渲染并高亮目标事件段;否则空(回退原文)。 */
const paragraphs = computed(() =>
  props.round.trace ? thinkParagraphs(props.round.trace.response, props.targetNames) : [],
)
</script>

<template>
  <section class="ff-round">
    <div class="ff-round-head">
      <span class="ff-round-no">R{{ round.round_no }}</span>
      <span class="ff-badge" :class="`ff-badge-${kindCls}`">{{ kindLabel }}</span>
      <span v-for="t in eventTags" :key="t" class="ff-tag">{{ t }}</span>
      <span class="ff-spacer" />
      <span v-if="round.state === 'running'" class="ff-badge ff-badge-run">推理中…</span>
      <template v-else-if="round.trace">
        <span class="ff-badge" :class="round.trace.patched ? 'ff-badge-patched' : 'ff-badge-nopatch'">
          {{ round.trace.patched ? '已合并修改' : '未产生修改' }}
        </span>
      </template>
    </div>
    <div class="ff-round-body">
      <template v-if="round.trace">
        <button class="ff-fold-btn" type="button" @click="promptOpen = !promptOpen">
          {{ promptOpen ? '收起 Prompt' : '展开 Prompt' }}
        </button>
        <pre v-if="promptOpen" class="ff-pre">{{ round.trace.prompt }}</pre>
        <template v-if="paragraphs.length">
          <div
            v-for="(p, i) in paragraphs"
            :key="i"
            class="ff-para"
            :class="p.hit ? `ff-para-hit-${kindCls}` : ''"
          >
            <span v-if="p.hit" class="ff-para-label">修复段落 · {{ p.prefix }}</span>
            {{ p.body }}
          </div>
        </template>
        <pre v-else class="ff-pre">{{ round.trace.response }}</pre>
      </template>
      <span v-else class="ff-running">正在等待 VLM 响应…</span>
    </div>
  </section>
</template>
