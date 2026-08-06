<script setup lang="ts">
/** 精度指标卡:按事件类别的 TP/FP/FN/精确率/召回率/F1 + 宏/微平均行。
 * 迁移自 legacy dashboard.js renderMetrics。Honest copy:只渲染 API 真实返回,
 * per_event 为空 → 空态说明;单个数值缺失 → 「—」,不编造。 */
import { computed } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import type { MetricAvg } from '../../stores/dashboard'

const dash = useDashboardStore()
const per = computed(() => dash.data?.metrics?.per_event || [])
// 宏/微平均行:metrics 缺失时整行数值显「—」(fmtNum 兜底)
const avgRows = computed<[string, MetricAvg][]>(() => [
  ['宏平均', dash.data?.metrics?.macro || {}],
  ['微平均', dash.data?.metrics?.micro || {}],
])

/** 数值格式化(同 legacy fmtNum):缺失/非数 → 「—」;小数保留 3 位。 */
function fmtNum(v: number | null | undefined): string {
  if (v == null || isNaN(v)) return '—'
  return !Number.isInteger(v) ? v.toFixed(3) : String(v)
}
</script>

<template>
  <section class="dash-card">
    <div class="dash-card-head">
      <span class="dash-card-title">精度指标</span>
      <span class="dash-card-sub">按事件类别统计</span>
    </div>
    <div class="dash-card-body">
      <div v-if="dash.summaryError" class="dash-empty">看板数据加载失败:{{ dash.summaryError }}</div>
      <div v-else-if="!per.length" class="dash-empty">尚无精度指标,完成评估后此处展示。</div>
      <div v-else class="dash-table-wrap">
        <table class="dash-table dash-metrics-table">
          <thead>
            <tr>
              <th>事件</th>
              <th>TP</th>
              <th>FP</th>
              <th>FN</th>
              <th>精确率</th>
              <th>召回率</th>
              <th>F1</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="e in per" :key="e.event_id">
              <td>{{ e.name || dash.eventName(e.event_id) }}</td>
              <td class="num">{{ fmtNum(e.tp) }}</td>
              <td class="num">{{ fmtNum(e.fp) }}</td>
              <td class="num">{{ fmtNum(e.fn) }}</td>
              <td class="num">{{ fmtNum(e.precision) }}</td>
              <td class="num">{{ fmtNum(e.recall) }}</td>
              <td class="num">{{ fmtNum(e.f1) }}</td>
            </tr>
            <tr v-for="[label, avg] in avgRows" :key="label" class="total">
              <td>{{ label }}</td>
              <td class="num">{{ fmtNum(avg.tp) }}</td>
              <td class="num">{{ fmtNum(avg.fp) }}</td>
              <td class="num">{{ fmtNum(avg.fn) }}</td>
              <td class="num">{{ fmtNum(avg.precision) }}</td>
              <td class="num">{{ fmtNum(avg.recall) }}</td>
              <td class="num">{{ fmtNum(avg.f1) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>
