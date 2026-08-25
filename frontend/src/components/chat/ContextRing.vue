<script setup lang="ts">
/** 上下文用量圆环(参考 kimi-code 的环形进度概念):
 * SVG 环形进度条填充表示已用占比,中心显示百分比;
 * 颜色分档:≤60% accent / 60–85% gold / >85% red;tooltip 显示「已用 x / y」。 */
import { computed } from 'vue'

const props = defineProps<{
  /** 已用 token;null 表示还没有真实用量(按 0 展示)。 */
  used: number | null
  /** 上下文窗口上限(token)。 */
  max: number
}>()

const RADIUS = 15.5
const CIRCUMFERENCE = 2 * Math.PI * RADIUS

const ratio = computed(() => {
  if (props.max <= 0) return 0
  return Math.min(1, Math.max(0, (props.used ?? 0) / props.max))
})

const percentText = computed(() => `${Math.round(ratio.value * 100)}%`)

const dasharray = computed(
  () => `${(ratio.value * CIRCUMFERENCE).toFixed(2)} ${CIRCUMFERENCE.toFixed(2)}`,
)

const level = computed(() => {
  if (ratio.value > 0.85) return 'lvl-danger'
  if (ratio.value > 0.6) return 'lvl-warn'
  return 'lvl-ok'
})

function formatTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

const tooltip = computed(
  () => `已用 ${formatTokens(props.used ?? 0)} / ${formatTokens(props.max)}`,
)
</script>

<template>
  <span class="context-ring" :class="level" :title="tooltip">
    <svg viewBox="0 0 36 36" class="context-ring-svg" aria-hidden="true">
      <circle class="ring-track" cx="18" cy="18" :r="RADIUS" />
      <circle
        class="ring-fill"
        cx="18"
        cy="18"
        :r="RADIUS"
        :stroke-dasharray="dasharray"
        transform="rotate(-90 18 18)"
      />
    </svg>
    <span class="ring-text">{{ percentText }}</span>
  </span>
</template>

<style scoped>
.context-ring {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  flex: 0 0 36px;
}

.context-ring-svg {
  width: 100%;
  height: 100%;
  display: block;
}

.ring-track,
.ring-fill {
  fill: none;
  stroke-width: 3.5;
}

.ring-track {
  stroke: var(--color-border);
}

.ring-fill {
  stroke: currentColor;
  stroke-linecap: round;
  transition: stroke-dasharray 0.3s ease;
}

.ring-text {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 9px;
  font-weight: 600;
  color: var(--color-text2);
}

/* ---- 用量分档(全部走 design token,不写 inline hex) ---- */
.lvl-ok {
  color: var(--color-accent);
}

.lvl-warn {
  color: var(--color-gold);
}

.lvl-danger {
  color: var(--color-red);
}
</style>
