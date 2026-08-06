<script setup lang="ts">
/** 数据看板整页:精度指标卡 + 逐视频明细卡(筛选条 + 表格)。
 * 迁移自 legacy openDashboard:进入首拉一次;打开期间订阅 SSE dashboard.changed
 * → 重拉当前页(事件驱动,不轮询);useEvents 在组件卸载时自动退订。
 * 筛选/分页状态在 store 中,离开页面保留(legacy 为模块级全局)。 */
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { NButton } from 'naive-ui'
import DashboardTable from '../components/dashboard/DashboardTable.vue'
import MetricCards from '../components/dashboard/MetricCards.vue'
import UiIcon from '../components/UiIcon.vue'
import { useEvents } from '../composables/useEvents'
import {
  CONSISTENCY_OPTIONS,
  REVIEW_OPTIONS,
  useDashboardStore,
} from '../stores/dashboard'

const dash = useDashboardStore()
const router = useRouter()
const { subscribe } = useEvents()
subscribe('dashboard.changed', () => dash.refresh())

onMounted(() => dash.refresh())

// 页眉:「共 X 个视频 · 第 a-b 条 / 共 N 条」(区间按当前筛选后的 total,同 legacy)
const summaryText = computed(() => {
  const s = dash.summary
  let text = `共 ${s.total != null ? s.total : 0} 个视频`
  const d = dash.rowsData
  if (d) {
    const total = d.total || 0
    const a = total ? (d.page - 1) * d.size + 1 : 0
    const b = Math.min(total, d.page * d.size)
    text += ` · 第 ${a}-${b} 条 / 共 ${total} 条`
  }
  return text
})

function onSearch(ev: Event) {
  dash.setName((ev.target as HTMLInputElement).value)
}
</script>

<template>
  <div class="dash-page">
    <div class="dash-head">
      <h1 class="dash-title">数据看板</h1>
      <n-button size="small" @click="router.push('/')">
        <template #icon><UiIcon name="up" :size="12" style="transform: rotate(-90deg)" /></template>
        返回树视图
      </n-button>
    </div>

    <MetricCards />

    <section class="dash-card">
      <div class="dash-card-head">
        <span class="dash-card-title">逐视频明细</span>
        <span class="dash-card-sub">{{ summaryText }}</span>
        <span class="dash-spacer" />
        <span class="dash-card-sub">点击「打开」进入该视频分析详情</span>
      </div>
      <!-- 筛选条:一致性/审核多值 chip + 人工已改 + 名称搜索,全部走服务端(先过滤后分页) -->
      <div class="dash-filters">
        <span class="dash-filter-label">一致性</span>
        <button
          v-for="c in CONSISTENCY_OPTIONS"
          :key="c.key"
          type="button"
          class="dash-chip"
          :class="[`dash-chip-${c.cls}`, { on: dash.filters.consistency.has(c.key) }]"
          @click="dash.toggleConsistency(c.key)"
        >
          {{ c.label }}<b v-if="dash.summary[c.key] != null">{{ dash.summary[c.key] }}</b>
        </button>
        <span class="dash-filter-sep" />
        <span class="dash-filter-label">审核</span>
        <button
          v-for="r in REVIEW_OPTIONS"
          :key="r.key"
          type="button"
          class="dash-chip"
          :class="[`dash-chip-${r.cls}`, { on: dash.filters.review.has(r.key) }]"
          @click="dash.toggleReview(r.key)"
        >
          {{ r.label }}<b v-if="dash.summary[r.key] != null">{{ dash.summary[r.key] }}</b>
        </button>
        <span class="dash-filter-sep" />
        <button
          type="button"
          class="dash-chip dash-chip-edit"
          :class="{ on: dash.filters.editedOnly }"
          @click="dash.toggleEdited()"
        >
          人工已改<b v-if="dash.summary.edited != null">{{ dash.summary.edited }}</b>
        </button>
        <span class="dash-filter-sep" />
        <input
          class="dash-search"
          type="text"
          spellcheck="false"
          placeholder="搜索名称…"
          :value="dash.filters.name"
          @input="onSearch"
        />
        <button
          v-if="dash.hasFilters"
          type="button"
          class="dash-chip dash-chip-clear"
          @click="dash.clearFilters()"
        >
          清除过滤
        </button>
      </div>
      <div class="dash-card-body dash-body">
        <DashboardTable />
      </div>
    </section>
  </div>
</template>

<style scoped>
/* 看板嵌在 TreeView 主区内:滚动与 padding 由外壳 .app-main 承担,这里不再重复 */
.dash-head {
  max-width: 1280px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-md);
}

.dash-title {
  font-family: var(--font-pixel);
  font-size: var(--text-2xl);
  font-weight: 650;
  font-style: normal; /* 标题一律 roman(design.md §2) */
  margin: 0;
}
</style>
