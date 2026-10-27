<script setup lang="ts">
/** SFT Forge 单轮小节(嵌在 ForgeVideoCard 内,非独立卡片):轮次号 + kind
 * 徽标 + 目标事件标签 + 状态(R0=初始生成;纠错轮=已合并修改/未产生修改;
 * 进行中=推理中)+ prompt 折叠 + 思考过程(reasoning_content;流式时逐字
 * 渲染)+ 响应段落(JSON 主路径:forward 的 event_thoughts 逐事件段、纠错轮
 * 的 thinking+verdict 单段;回退 <think> 文本切分;命中段落按 kind 配色
 * 高亮,即「修复段落 diff 高亮」;流式进行中展示增量原文,完成后替换为
 * 解析段落;均不可解析时展示原文)。
 * 样式类在 styles/sftforge.css(ff-*),颜色全部 token。 */
import { computed, ref, watch } from 'vue'
import type { ForgeRoundEntry } from '../../stores/sftforge'
import { forgeKindCls, forgeKindLabel, responseSegments } from '../../utils/sftforge'

const props = defineProps<{
  round: ForgeRoundEntry
  /** 本轮目标事件中文名集合(段落高亮命中集)。 */
  targetNames: Set<string>
  /** 事件 id → 中文名(标签展示)。 */
  eventNames: Record<number, string>
}>()

const promptOpen = ref(false)
const reasoningOpen = ref(true)
// 轮次小节默认折叠(头部点击展开);推理中的轮次自动展开以观看流式,
// 结束后保持展开态由用户手动折叠。
const open = ref(false)
watch(
  () => props.round.state,
  (s) => {
    if (s === 'running') open.value = true
  },
)

const kindCls = computed(() => forgeKindCls(props.round.kind))
const kindLabel = computed(() => forgeKindLabel(props.round.kind))
const eventTags = computed(() =>
  props.round.event_ids.map((id) => props.eventNames[id] || `事件${id}`),
)
/** 段落视图:JSON 主路径(event_thoughts 逐事件 / thinking+verdict 单段)
 * 优先,回退 <think> 文本切分;均不可用为空(回退原文)。 */
const paragraphs = computed(() =>
  props.round.trace
    ? responseSegments(props.round.trace.response, props.targetNames, props.eventNames)
    : [],
)
/** 流式中的思考/正文(running 态直接展示增量;reset 已由 store 清空)。 */
const liveReasoning = computed(() =>
  props.round.state === 'running' ? props.round.streamReasoning : '',
)
const liveResponse = computed(() =>
  props.round.state === 'running' ? props.round.streamResponse : '',
)
const doneReasoning = computed(() =>
  props.round.state === 'done' ? (props.round.trace?.reasoning || '') : '',
)
</script>

<template>
  <section class="ff-round">
    <button class="ff-round-head" type="button" @click="open = !open">
      <span class="ff-round-no">R{{ round.round_no }}</span>
      <span class="ff-badge" :class="`ff-badge-${kindCls}`">{{ kindLabel }}</span>
      <span v-for="t in eventTags" :key="t" class="ff-tag">{{ t }}</span>
      <span class="ff-spacer" />
      <span v-if="round.state === 'running'" class="ff-badge ff-badge-run">推理中…</span>
      <template v-else-if="round.trace">
        <!-- R0 是初始生成,无「修改」语义;patched 徽标仅对纠错轮展示 -->
        <span v-if="round.round_no === 0" class="ff-badge ff-badge-initial">初始生成</span>
        <span
          v-else
          class="ff-badge"
          :class="round.trace.patched ? 'ff-badge-patched' : 'ff-badge-nopatch'"
        >
          {{ round.trace.patched ? '已合并修改' : '未产生修改' }}
        </span>
      </template>
      <span class="ff-video-toggle">{{ open ? '收起' : '展开' }}</span>
    </button>
    <div v-if="open" class="ff-round-body">
      <template v-if="round.trace">
        <button class="ff-fold-btn" type="button" @click="promptOpen = !promptOpen">
          {{ promptOpen ? '收起 Prompt' : '展开 Prompt' }}
        </button>
        <pre v-if="promptOpen" class="ff-pre">{{ round.trace.prompt }}</pre>
        <template v-if="doneReasoning">
          <button class="ff-fold-btn" type="button" @click="reasoningOpen = !reasoningOpen">
            {{ reasoningOpen ? '收起思考过程' : '展开思考过程' }}
          </button>
          <pre v-if="reasoningOpen" class="ff-pre ff-pre-reasoning">{{ doneReasoning }}</pre>
        </template>
        <template v-if="paragraphs.length">
          <div
            v-for="(p, i) in paragraphs"
            :key="i"
            class="ff-para"
            :class="p.hit ? `ff-para-hit-${kindCls}` : ''"
          >
            <span v-if="p.hit && p.label" class="ff-para-label">修复段落 · {{ p.label }}</span>
            <span v-else-if="p.label" class="ff-para-label">{{ p.label }}</span>
            {{ p.body }}
          </div>
        </template>
        <pre v-else class="ff-pre">{{ round.trace.response }}</pre>
      </template>
      <template v-else>
        <div v-if="liveReasoning" class="ff-stream-block">
          <span class="ff-para-label">思考过程(生成中…)</span>
          <pre class="ff-pre ff-pre-reasoning">{{ liveReasoning }}</pre>
        </div>
        <div v-if="liveResponse" class="ff-stream-block">
          <span class="ff-para-label">响应(生成中…)</span>
          <pre class="ff-pre">{{ liveResponse }}</pre>
        </div>
        <span v-if="!liveReasoning && !liveResponse" class="ff-running">正在等待 VLM 响应…</span>
      </template>
    </div>
  </section>
</template>
