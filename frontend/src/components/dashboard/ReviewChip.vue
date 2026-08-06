<script setup lang="ts">
/** 行内审核三 chip:当前态实心,非当前 ghost;点击乐观更新,失败回滚 + 错误提示。
 * 迁移自 legacy dashboard.js reviewChips/setReview;状态集合以 review.py REVIEW_STATUSES 为准。 */
import { computed } from 'vue'
import { useMessage } from 'naive-ui'
import { REVIEW_OPTIONS, useDashboardStore } from '../../stores/dashboard'
import type { ReviewStatus } from '../../stores/dashboard'

const props = defineProps<{ stem: string; review: ReviewStatus }>()

const dash = useDashboardStore()
const message = useMessage()
// 提交中:整组禁用(loading 态),防连点;重拉回包不回滚本行(见 store pendingReviews)
const pending = computed(() => dash.pendingReviews.has(props.stem))

async function pick(status: ReviewStatus) {
  if (pending.value || status === props.review) return
  try {
    await dash.setReview(props.stem, status)
  } catch (e) {
    message.error(`审核状态保存失败:${(e as Error).message}`) // store 已回滚
  }
}
</script>

<template>
  <!-- @click.stop:审核点击不触发行跳转(同 legacy) -->
  <span class="dash-review-group" @click.stop>
    <button
      v-for="v in REVIEW_OPTIONS"
      :key="v.key"
      type="button"
      class="dash-review-chip"
      :class="[`dash-chip-${v.cls}`, { on: review === v.key }]"
      :disabled="pending"
      :title="`标记为「${v.label}」`"
      @click="pick(v.key)"
    >
      {{ v.label }}
    </button>
  </span>
</template>
