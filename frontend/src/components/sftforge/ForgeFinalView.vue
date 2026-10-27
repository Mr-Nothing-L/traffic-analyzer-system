<script setup lang="ts">
/** 最终结论视图(图 2 的 SFT 标注风格,只读):逐事件块(事件名 + 检出/
 * 未检出 + 第 N 轮修正标记 + 段落正文)+ ANSWER 区(天气/时间/场景 +
 * 最终结论 class 列表)。事件块按状态着色:检出=sage 绿、纠错修订=
 * accent 橙、未检出=灰。数据源为 video_completed 载荷的 final_output
 * (merger 代码重建的规范 <think>/<answer> 文本)。
 * 样式类在 styles/sftforge.css(ff-final-*),颜色全部 token。 */
import { computed } from 'vue'
import { parseFinalOutput } from '../../utils/sftforge'

const props = defineProps<{
  /** 最终 <think>/<answer> 规范文本。 */
  output: string
  /** 事件 id → 中文名。 */
  eventNames: Record<number, string>
  /** 被纠错修订的事件中文名 → 修订发生的轮次号(标注「第 N 轮修正」)。 */
  patchedRounds: Record<string, number>
}>()

const view = computed(() => parseFinalOutput(props.output, props.eventNames))
const hasAnswer = computed(
  () => !!(view.value.weather || view.value.conclusion || view.value.classes.length),
)

/** 事件块着色:修订 > 检出 > 未检出。 */
function blockCls(name: string, present: boolean): string {
  if (props.patchedRounds[name] != null) return 'fixed'
  return present ? 'on' : 'off'
}
</script>

<template>
  <div class="ff-final">
    <div class="ff-final-title">最终结论</div>
    <div
      v-for="(ev, i) in view.events"
      :key="i"
      class="ff-final-event"
      :class="blockCls(ev.name, ev.present)"
    >
      <div class="ff-final-event-head">
        <span class="ff-final-event-name">{{ ev.name }}</span>
        <span v-if="patchedRounds[ev.name] != null" class="ff-final-fix-tag">
          第 {{ patchedRounds[ev.name] }} 轮修正
        </span>
        <span class="ff-spacer" />
        <span class="ff-final-check" :class="{ on: ev.present }">
          {{ ev.present ? '✓ 检出' : '未检出' }}
        </span>
      </div>
      <div class="ff-final-event-body">{{ ev.body }}</div>
    </div>
    <template v-if="hasAnswer">
      <div class="ff-final-title">答案(ANSWER)</div>
      <div class="ff-final-field"><span class="ff-final-label">天气</span>{{ view.weather || '—' }}</div>
      <div class="ff-final-field"><span class="ff-final-label">时间</span>{{ view.timeOfDay || '—' }}</div>
      <div class="ff-final-field"><span class="ff-final-label">场景</span>{{ view.scene || '—' }}</div>
      <div class="ff-final-field">
        <span class="ff-final-label">最终结论</span>{{ view.conclusion || '—' }}
      </div>
      <div v-for="(c, i) in view.classes" :key="i" class="ff-final-class">
        <span class="ff-final-class-id">class{{ c.id ?? '?' }}</span>
        {{ c.name }}
      </div>
    </template>
  </div>
</template>
