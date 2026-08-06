<script setup lang="ts">
/** 侧栏工具条:标题 + 全选 + 全选待推理 + 过滤框 + 排序下拉(迁移自 legacy index.html side-head/side-filter)。 */
import { computed } from 'vue'
import { NCheckbox, NInput, NSelect } from 'naive-ui'
import type { SortKey } from '../../stores/workspace'
import { useWorkspaceStore } from '../../stores/workspace'
import { useTreeView } from '../../composables/useTree'

const ws = useWorkspaceStore()
const { pendingRels, setPendingChecked } = useTreeView()

/** 「待推理」勾选态:全部待推理项已勾 → checked;部分 → 半选。 */
const pendingAllChecked = computed(
  () =>
    pendingRels.value.size > 0 &&
    [...pendingRels.value].every((r) => ws.checked.has(r)),
)
const pendingSomeChecked = computed(
  () => !pendingAllChecked.value && [...pendingRels.value].some((r) => ws.checked.has(r)),
)

const sortOptions: { label: string; value: SortKey }[] = [
  { label: '名称', value: 'name' },
  { label: '修改时间', value: 'mtime' },
  { label: '大小', value: 'size' },
  { label: '状态', value: 'status' },
]
</script>

<template>
  <div class="side-head">
    <span class="side-title">工作区文件</span>
    <span class="side-checks">
      <n-checkbox
        :checked="pendingAllChecked"
        :indeterminate="pendingSomeChecked"
        size="small"
        title="全选未推理与推理失败的视频"
        aria-label="全选未推理与推理失败的视频"
        @update:checked="setPendingChecked"
      >
        <span class="side-check-label">待推理</span>
      </n-checkbox>
      <n-checkbox
        :checked="ws.allChecked"
        :indeterminate="ws.someChecked"
        size="small"
        title="全选/取消全选"
        aria-label="全选/取消全选"
        @update:checked="ws.setAllChecked"
      />
    </span>
  </div>
  <div class="side-filter">
    <n-input
      :value="ws.filter"
      size="small"
      clearable
      spellcheck="false"
      placeholder="过滤视频…"
      aria-label="过滤视频"
      @update:value="ws.setFilter"
    />
    <n-select
      :value="ws.sort"
      :options="sortOptions"
      size="small"
      class="sort-sel"
      aria-label="排序方式"
      @update:value="ws.setSort"
    />
  </div>
</template>

<style scoped>
.side-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px 8px;
}

.side-checks {
  display: flex;
  align-items: center;
  gap: 10px;
}

.side-check-label {
  font-size: 11px;
  color: var(--color-text2);
}

.side-title {
  font-size: 12px;
  font-weight: 650;
  color: var(--color-text2);
  letter-spacing: 0.06em;
}

.side-filter {
  display: flex;
  gap: 6px;
  padding: 0 14px 10px;
  margin-bottom: 8px;
  border-bottom: 1px solid var(--color-border);
}

.side-filter :deep(.n-input) {
  flex: 1;
  min-width: 0;
}

.sort-sel {
  flex: 0 0 96px;
}
</style>
