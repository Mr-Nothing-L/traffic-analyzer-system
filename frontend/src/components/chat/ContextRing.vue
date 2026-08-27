<script setup lang="ts">
/** 上下文用量圆环(参考 kimi-code 的环形进度概念):
 * 小尺寸 SVG 环形进度条填充表示已用占比,平时不显示任何文字;
 * 点击弹 NPopover:百分比 + 「已用 x / y tokens」+ 压缩按钮(canCompact 时)。
 * 后端只有 usedTokens 总量,没有 system/对话/工具结果的分段数据,
 * 故不做分段拆分条(不编造分段数)。
 * 颜色分档:≤60% accent / 60–85% gold / >85% red。 */
import { computed } from 'vue'
import { NPopover } from 'naive-ui'

const props = defineProps<{
  /** 已用 token;null 表示还没有真实用量(按 0 展示)。 */
  used: number | null
  /** 上下文窗口上限(token)。 */
  max: number
  /** 用量超阈值时弹层内显示压缩按钮。 */
  canCompact?: boolean
  /** 压缩在途/对话进行中:禁用压缩按钮。 */
  compacting?: boolean
}>()

const emit = defineEmits<{ compact: [] }>()

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

const usageText = computed(
  () => `已用 ${formatTokens(props.used ?? 0)} / ${formatTokens(props.max)} tokens`,
)
</script>

<template>
  <n-popover trigger="click" placement="top-end">
    <template #trigger>
      <button class="context-ring" :class="level" title="上下文用量">
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
      </button>
    </template>
    <div class="ctx-panel">
      <div class="ctx-percent" :class="level">{{ percentText }}</div>
      <div class="ctx-usage">{{ usageText }}</div>
      <button
        v-if="canCompact"
        class="ctx-compact"
        :disabled="compacting"
        @click="emit('compact')"
      >
        {{ compacting ? '压缩中…' : '压缩上下文' }}
      </button>
    </div>
  </n-popover>
</template>

<style scoped>
.context-ring {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  flex: 0 0 20px;
  padding: 0;
  border: none;
  background: none;
  cursor: pointer;
  border-radius: 50%;
}

.context-ring:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: 1px;
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

/* ---- 点击弹层:百分比 + 总量 + 压缩按钮(无分段数据,不编造分段) ---- */
.ctx-panel {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-xs);
  min-width: 160px;
}

.ctx-percent {
  font-size: var(--text-lg);
  font-weight: 700;
  font-family: var(--font-mono); /* 数值 → 等宽 */
}

.ctx-usage {
  font-size: var(--text-xs);
  color: var(--color-text2);
  font-family: var(--font-mono);
}

.ctx-compact {
  padding: 2px var(--space-sm);
  border: 1px solid var(--color-gold);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--color-gold) 10%, var(--color-card));
  color: var(--color-gold);
  font-size: var(--text-xs);
  font-weight: 600;
  font-family: var(--font-pixel); /* 按钮 → 像素 */
  cursor: pointer;
}

.ctx-compact:hover:not(:disabled) {
  filter: brightness(0.97);
}

.ctx-compact:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
