<script setup lang="ts">
/** 8 格迷你像素进度条(侧栏 running 行;视觉迁移自 legacy pixel_bar.js + expert.css)。
 * 每格 3 个子像素从上到下点亮;fraction 为 null 时不定态波浪;
 * running 时下一个待点亮子像素带 frontier 明暗脉冲。 */
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{ fraction: number | null; running?: boolean; cells?: number; subs?: number }>(),
  { running: false, cells: 8, subs: 3 },
)

interface Cell {
  lit: number // 已点亮子像素数
  frontier: number // frontier 子像素下标(-1 无)
}

const cellList = computed<Cell[]>(() => {
  const n = props.cells
  const out: Cell[] = []
  if (props.fraction == null) {
    for (let i = 0; i < n; i++) out.push({ lit: 0, frontier: -1 }) // 不定态交给 CSS 波浪
    return out
  }
  const pos = Math.max(0, Math.min(1, props.fraction)) * n
  const full = Math.min(n, Math.floor(pos))
  const litInFrontier = Math.min(props.subs - 1, Math.floor((pos - full) * props.subs))
  for (let i = 0; i < n; i++) {
    const lit = i < full ? props.subs : i === full ? litInFrontier : 0
    const frontier = props.running && i === full && full < n ? lit : -1
    out.push({ lit, frontier })
  }
  return out
})

/** 不定态波浪:逐格 0.12s 延迟(legacy nth-child 规则的同构实现)。 */
function waveDelay(i: number) {
  return props.fraction == null ? { animationDelay: `${i * 0.12}s` } : undefined
}
</script>

<template>
  <span class="pixel-bar mini-prog" :class="{ indet: fraction == null }" title="推理中">
    <span v-for="(cell, i) in cellList" :key="i" class="pixel-cell">
      <span
        v-for="s in subs"
        :key="s"
        class="pixel-sub"
        :class="{ on: s - 1 < cell.lit || s - 1 === cell.frontier, frontier: s - 1 === cell.frontier }"
        :style="waveDelay(i)"
      />
    </span>
  </span>
</template>

<style scoped>
.mini-prog {
  flex: 0 0 auto;
  align-self: center;
  display: flex;
  gap: 2px;
}

.pixel-cell {
  flex: 0 0 auto;
  width: 4px;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.pixel-sub {
  display: block;
  width: 4px;
  height: 4px;
  border-radius: 1px;
  background: var(--color-surface-3); /* 空像素 */
}

.pixel-sub.on {
  background: var(--color-accent);
}

/* frontier:下一个待点亮子像素的明暗脉冲 */
.pixel-sub.frontier {
  animation: pixel-frontier 0.9s var(--ease-in-out) infinite;
}

@keyframes pixel-frontier {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.25;
  }
}

/* 不定态(fraction 为 null):子像素波浪循环点亮 */
.mini-prog.indet .pixel-sub {
  background: var(--color-accent);
  animation: pixel-wave 1.1s linear infinite;
}

@keyframes pixel-wave {
  0%,
  100% {
    opacity: 0.12;
  }
  50% {
    opacity: 1;
  }
}

/* design.md §4:reduced-motion 下折叠为静态 */
@media (prefers-reduced-motion: reduce) {
  .pixel-sub.frontier,
  .mini-prog.indet .pixel-sub {
    animation: none;
    opacity: 0.45;
  }
}
</style>
