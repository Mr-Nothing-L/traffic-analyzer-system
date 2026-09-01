<script setup lang="ts">
/** 侧栏工具条:标题 + 全选 + 全选待推理 + 过滤框 + 排序下拉 + 批量关键帧
 * + 批量删除报告(迁移自 legacy index.html side-head/side-filter;
 * 批量关键帧为 v4.6 新增,删除报告同批次新增)。 */
import { computed, h, ref } from 'vue'
import { NButton, NCheckbox, NInput, NSelect, useDialog, useMessage } from 'naive-ui'
import type { SortKey } from '../../stores/workspace'
import { useWorkspaceStore } from '../../stores/workspace'
import { useTreeView } from '../../composables/useTree'
import { getKfBatch, startKfBatch } from '../../api/keyframes'
import type { BatchStatus } from '../../api/keyframes'

const ws = useWorkspaceStore()
const { pendingRels, setPendingChecked } = useTreeView()
const message = useMessage()
const dialog = useDialog()

/** 批量关键帧状态(轮询进行中按钮转圈防重复提交)。 */
const batchRunning = ref(false)
const kfOverwrite = ref(false)

/** 批量删除报告进行中(防重复提交)。 */
const deleteRunning = ref(false)

/** 「待推理」勾选态:选中集恰好等于待推理集合时才点亮(精确匹配,
 * 与「全选」解耦——点全选不会连带点亮它);不做半选态。 */
const pendingActive = computed(
  () =>
    pendingRels.value.size > 0 &&
    ws.checked.size === pendingRels.value.size &&
    [...pendingRels.value].every((r) => ws.checked.has(r)),
)

const sortOptions: { label: string; value: SortKey }[] = [
  { label: '名称', value: 'name' },
  { label: '修改时间', value: 'mtime' },
  { label: '大小', value: 'size' },
  { label: '状态', value: 'status' },
]

/** 勾选 rel → stem(取 basename 去扩展名,与后端 video.stem 口径一致)。 */
function stemOf(rel: string): string {
  const base = rel.split('/').pop() ?? rel
  return base.replace(/\.[^.]+$/, '')
}

function onBatchKeyframes() {
  if (!ws.checked.size || batchRunning.value) return
  dialog.warning({
    title: '批量智能挑选关键帧',
    content: () =>
      h('div', null, [
        h(
          'div',
          { style: 'margin-bottom:8px;font-size:13px;line-height:1.6' },
          `将对 ${ws.checked.size} 个选中视频用主用 LLM 挑选 2–5 张关键帧(无标注/未推理的自动跳过,不影响文本标注)。`,
        ),
        h(
          NCheckbox,
          {
            checked: kfOverwrite.value,
            'onUpdate:checked': (v: boolean) => (kfOverwrite.value = v),
          },
          { default: () => '覆盖已有关键帧(默认跳过已挑过的视频)' },
        ),
      ]),
    positiveText: '开始挑选',
    negativeText: '取消',
    onPositiveClick: runBatch,
  })
}

async function runBatch() {
  const stems = [...ws.checked].map(stemOf)
  if (!stems.length) return
  batchRunning.value = true
  try {
    const { id } = await startKfBatch(stems, kfOverwrite.value)
    message.info(`批量智能挑选已启动(${stems.length} 个视频)`)
    await pollBatch(id)
  } catch (e) {
    message.error(`批量智能挑选启动失败:${e instanceof Error ? e.message : String(e)}`)
  } finally {
    batchRunning.value = false
  }
}

/** 轮询 batch 进度直到终态;后端批次丢失(重启/过期)则静默结束。 */
async function pollBatch(id: string) {
  for (let i = 0; i < 1200; i++) {
    let st: BatchStatus
    try {
      st = await getKfBatch(id)
    } catch {
      return // 批次不存在(web 重启):静默放弃轮询
    }
    if (st.running) {
      await new Promise((r) => setTimeout(r, 1500))
      continue
    }
    const items = Object.values(st.items)
    const count = (s: string) => items.filter((it) => it.status === s).length
    const ok = count('ok')
    const skip = count('skipped')
    const failed = items.find((it) => it.status === 'failed')
    if (failed) {
      message.error(
        `批量完成:成功 ${ok},跳过 ${skip},失败 ${count('failed')};首个失败:${failed.message || '未知原因'}`,
      )
    } else {
      message.success(`批量关键帧完成:成功 ${ok},跳过 ${skip}`)
    }
    return
  }
}

/* ---- 批量删除报告:仅对已选中且有报告的条目生效,一次确认,失败逐项提示 ---- */

/** 勾选中「有报告」的 rel(视频列表缺失该条目时不视为有报告,不参与删除)。 */
const checkedWithResults = computed(() =>
  [...ws.checked].filter((rel) => ws.videoByRel.get(rel)?.has_results),
)

function onDeleteReports() {
  const rels = checkedWithResults.value
  if (!rels.length || deleteRunning.value) return
  dialog.warning({
    title: '批量删除分析报告',
    content: `将删除 ${rels.length} 个视频的分析报告(analysis/<stem>/ 整目录,含报告、SFT 标注、证据与图片),不可恢复。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: () => runDeleteReports(rels),
  })
}

async function runDeleteReports(rels: string[]) {
  deleteRunning.value = true
  try {
    const results = await ws.deleteReports(rels)
    // 已回滚徽标的失败项逐项提示;成功项按实际删掉的目录数汇总。
    results.filter((r) => !r.ok).forEach((r) =>
      message.error(`删除 ${r.stem} 的分析报告失败:${r.error || '未知原因'}`),
    )
    const removed = results.filter((r) => r.ok && r.existed).length
    if (removed > 0) message.success(`已删除 ${removed} 个分析报告`)
    else if (results.every((r) => r.ok)) message.info('选中的条目均无报告目录')
  } catch (e) {
    message.error(`批量删除报告失败:${e instanceof Error ? e.message : String(e)}`)
  } finally {
    deleteRunning.value = false
  }
}
</script>

<template>
  <div class="side-head">
    <span class="side-title">工作区文件</span>
    <span class="side-checks">
      <n-checkbox
        :checked="pendingActive"
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
      >
        <span class="side-check-label">全选</span>
      </n-checkbox>
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
    <n-button
      size="small"
      secondary
      class="kf-batch-btn"
      title="对选中的视频运行智能挑选关键帧(无标注的跳过)"
      :disabled="!ws.checked.size || batchRunning"
      :loading="batchRunning"
      @click="onBatchKeyframes"
    >
      批量关键帧
    </n-button>
    <n-button
      size="small"
      secondary
      class="report-del-btn"
      title="删除选中视频的分析报告(仅有报告的条目会删除)"
      :disabled="!checkedWithResults.length || deleteRunning"
      :loading="deleteRunning"
      @click="onDeleteReports"
    >
      删除报告
    </n-button>
  </div>
</template>

<style scoped>
.side-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap; /* 窄栏时整组换行,标题/勾选组各自保持完整 */
  row-gap: 4px;
  padding: 12px 14px 8px;
}

.side-checks {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 0 0 auto; /* 中文 min-content 仅一字宽,不锁死会被逐字挤成竖排 */
  white-space: nowrap;
  margin-left: auto; /* 换行后勾选组仍靠右 */
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
  flex: 0 0 auto; /* 窄栏不逐字换行,整体换到上一行 */
  white-space: nowrap;
}

.side-filter {
  display: flex;
  flex-wrap: wrap; /* 窄栏时按钮/下拉整项换行,不互相挤压 */
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

.kf-batch-btn {
  flex: none;
}

.report-del-btn {
  flex: none;
}
</style>
