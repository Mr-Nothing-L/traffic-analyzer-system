<script setup lang="ts">
/** 证据表格:当前事件的证据框列表(标签/帧可编辑,坐标只读)。
 * 编辑即改 draft 并标 dirty,由证据卡统一保存。 */
import { computed } from 'vue'
import type { EvidenceEvent, EvidenceRegion } from '../../api/results'
import { useEvidenceStore } from '../../stores/evidence'

const props = defineProps<{ ev: EvidenceEvent }>()
const store = useEvidenceStore()

const regions = computed<EvidenceRegion[]>(() =>
  Array.isArray(props.ev.evidence_regions) ? props.ev.evidence_regions : [],
)

function fmtBox(r: EvidenceRegion): string {
  return Array.isArray(r.box_rel) ? r.box_rel.map((v) => v.toFixed(3)).join(', ') : '-'
}

function onLabel(r: EvidenceRegion, e: Event) {
  r.label = (e.target as HTMLInputElement).value
  store.markDirty()
}

function onFrame(r: EvidenceRegion, e: Event) {
  const n = parseInt((e.target as HTMLInputElement).value, 10)
  r.frame_index = isNaN(n) ? undefined : n
  store.markDirty()
}
</script>

<template>
  <table v-if="regions.length" class="ev-table">
    <thead>
      <tr>
        <th>标签</th>
        <th class="ev-table-frame">帧</th>
        <th>证据框(归一化 x1, y1, x2, y2)</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="(r, i) in regions" :key="i">
        <td><input type="text" :value="r.label || ''" @input="onLabel(r, $event)" /></td>
        <td class="ev-table-frame">
          <input type="number" min="0" :value="r.frame_index ?? ''" @change="onFrame(r, $event)" />
        </td>
        <td class="mono">{{ fmtBox(r) }}</td>
      </tr>
    </tbody>
  </table>
</template>

<style scoped>

.ev-table {
  border-collapse: collapse;
  margin-top: 12px;
  width: 100%;
  font-size: var(--text-sm);
}

.ev-table th,
.ev-table td {
  border: 1px solid var(--color-border);
  padding: 5px 8px;
  text-align: left;
}

.ev-table th {
  background: var(--color-surface-2);
  font-weight: 650;
  white-space: nowrap;
}

.ev-table .mono {
  font-size: var(--text-xs);
}

.ev-table input {
  width: 100%;
  border: 1px solid transparent;
  border-radius: 4px;
  padding: 2px 4px;
  font-size: var(--text-sm);
  background: transparent;
  color: var(--color-text);
}

.ev-table input:hover {
  border-color: var(--color-border);
}

.ev-table input:focus {
  border-color: var(--color-accent);
  outline: none;
  background: var(--color-card);
}
.ev-table-frame {
  width: 90px;
}
</style>
