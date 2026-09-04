<script setup lang="ts">
/** 侧栏工具条:标题 + 全选 + 全选待推理 + 两行操作区
 * (第一行:检索模式切换(文件名/语义检索)+过滤/检索框;
 *  第二行:排序下拉 + 更新向量库 + 批量关键帧 + 批量删除报告,按钮固定靠右;
 * 迁移自 legacy index.html side-head/side-filter;
 * 批量关键帧为 v4.6 新增,删除报告同批次新增;语义检索见 stores/rag.ts;
 * 更新向量库走 rag store 的 build 状态机(running 轮询 2s),pending=0 禁用,
 * 取消/冲突由后端契约兜底)。 */
import { computed, h, onMounted, onUnmounted, ref, watch } from 'vue'
import { NButton, NCheckbox, NInput, NSelect, useDialog, useMessage } from 'naive-ui'
import type { SortKey } from '../../stores/workspace'
import { useWorkspaceStore } from '../../stores/workspace'
import type { SideSearchMode } from '../../stores/rag'
import { useRagStore } from '../../stores/rag'
import { useTreeView } from '../../composables/useTree'
import { getKfBatch, startKfBatch } from '../../api/keyframes'
import type { BatchStatus } from '../../api/keyframes'
import { fmtTs } from '../../api/rag'

const ws = useWorkspaceStore()
const rag = useRagStore()
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

const modeOptions: { label: string; value: SideSearchMode }[] = [
  { label: '文件名', value: 'name' },
  { label: '语义检索', value: 'semantic' },
]

/** 语义模式输入草稿:回车才提交检索,不写 ws.filter(文件名模式维持现状本地过滤)。 */
const queryInput = ref('')

function onFilterInput(v: string) {
  if (rag.mode === 'name') {
    ws.setFilter(v)
    return
  }
  queryInput.value = v
  if (!v.trim()) rag.clear() // 清空即恢复文件树
}

/** 语义模式回车提交;文件名模式不响应(本地即时过滤)。 */
function onSearchSubmit() {
  if (rag.mode === 'semantic') void rag.search(queryInput.value)
}

/** 勾选 rel → stem(取 basename 去扩展名,与后端 video.stem 口径一致)。 */
function stemOf(rel: string): string {
  const base = rel.split('/').pop() ?? rel
  return base.replace(/\.[^.]+$/, '')
}

/* ---- 更新向量库:状态机在 rag store(running 轮询);此处只管确认弹窗、取消入口与终态提示 ---- */

const buildRunning = computed(() => rag.buildState === 'running')

/** 无可更新内容(库已建且 pending=0)时禁用:点击必然空转,不如明示已最新;
 * 库未建/pending 未知(扫描失败)时保持可点。 */
const buildUpToDate = computed(
  () => rag.library?.exists === true && rag.buildPending === 0,
)

/** 按钮文案:running 时带进度(如「建库中 120/440」)。 */
const buildBtnText = computed(() =>
  buildRunning.value ? `建库中 ${rag.buildDone}/${rag.buildTotal}` : '更新向量库',
)

/** tooltip:running 给进度与取消入口说明;否则给库概况与待更新数(未建库引导)。 */
const buildBtnTitle = computed(() => {
  if (buildRunning.value)
    return `向量库构建中(${rag.buildDone}/${rag.buildTotal},失败 ${rag.buildFailed});右侧「取消」可中止`
  const lib = rag.library
  if (lib?.exists) {
    if (buildUpToDate.value)
      return `向量库:${lib.count} 条,建于 ${fmtTs(lib.built_at ?? 0)};已是最新(无新视频或标注变更)`
    const pend = rag.buildPending
    return `向量库:${lib.count} 条,建于 ${fmtTs(lib.built_at ?? 0)};待更新 ${pend ?? '?'} 条(新视频/标注变更),点击增量更新`
  }
  return '尚未建库;点击对当前工作区全部视频构建向量库'
})

/** 空闲点击 → 确认后启动;running 时按钮 loading 吞点击,取消走旁边的「取消」按钮。 */
function onBuildRag() {
  if (buildRunning.value || buildUpToDate.value) return
  const lib = rag.library
  dialog.warning({
    title: lib?.exists ? '增量更新向量库' : '构建向量库',
    content: lib?.exists
      ? `将处理 ${rag.buildPending ?? '?'} 条待更新视频(新视频 + 标注变更),耗时取决于数量,期间占用 GPU 推理资源。`
      : '将对当前工作区全部视频构建向量库,耗时较长(数百视频约数十分钟),期间占用 GPU 推理资源。',
    positiveText: lib?.exists ? '开始更新' : '开始构建',
    negativeText: '取消',
    onPositiveClick: runBuild,
  })
}

/** 「取消」入口:再确认后 POST cancel(已建部分保留,可增量续建)。 */
function onCancelBuild() {
  if (!buildRunning.value) return
  dialog.warning({
    title: '取消向量库构建',
    content: `构建进行中(${rag.buildDone}/${rag.buildTotal})。取消后已构建部分保留,可随时重新增量构建。`,
    positiveText: '取消构建',
    negativeText: '继续构建',
    onPositiveClick: runCancelBuild,
  })
}

async function runBuild() {
  try {
    await rag.startBuild()
    message.info('向量库构建已启动')
  } catch (e) {
    message.error(`向量库构建启动失败:${e instanceof Error ? e.message : String(e)}`)
  }
}

async function runCancelBuild() {
  try {
    await rag.cancelBuild()
    message.info('已请求取消,等待当前视频处理完…')
  } catch (e) {
    message.error(`取消向量库构建失败:${e instanceof Error ? e.message : String(e)}`)
  }
}

/** 终态汇总:done 报成功/失败数(partial 注明已取消);error 报 last_error。 */
watch(
  () => rag.buildState,
  (s, prev) => {
    if (prev !== 'running') return
    if (s === 'done') {
      const ok = rag.buildDone - rag.buildFailed
      const summary = `向量库构建完成:成功 ${ok},失败 ${rag.buildFailed}`
      if (rag.buildPartial) message.warning(`${summary}(已取消,部分完成)`)
      else if (rag.buildFailed) message.warning(summary)
      else message.success(summary)
    } else if (s === 'error') {
      message.error(`向量库构建失败:${rag.buildError || '未知原因'}`)
    }
  },
)

onMounted(() => void rag.refreshBuildStatus()) // 进页面拉一次:恢复进行中构建的进度/库概况
onUnmounted(() => rag.stopBuild()) // 页面卸载:退出轮询循环
watch(() => ws.path, () => void rag.resetBuild()) // 切工作区:重置并按新工作区拉 status

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
    <div class="filter-row">
      <n-select
        :value="rag.mode"
        :options="modeOptions"
        size="small"
        class="mode-sel"
        aria-label="检索模式"
        @update:value="rag.setMode"
      />
      <n-input
        :value="rag.mode === 'name' ? ws.filter : queryInput"
        size="small"
        clearable
        spellcheck="false"
        :placeholder="rag.mode === 'name' ? '过滤视频…' : '描述画面或事件…'"
        :aria-label="rag.mode === 'name' ? '过滤视频' : '语义检索'"
        @update:value="onFilterInput"
        @keyup.enter="onSearchSubmit"
      />
    </div>
    <div class="filter-row action-row">
      <n-select
        :value="ws.sort"
        :options="sortOptions"
        size="small"
        class="sort-sel"
        aria-label="排序方式"
        @update:value="ws.setSort"
      />
      <span class="action-spacer" />
      <n-button
        size="small"
        secondary
        class="rag-build-btn"
        :title="buildBtnTitle"
        :disabled="!ws.hasWorkspace || buildUpToDate"
        :loading="buildRunning"
        @click="onBuildRag"
      >
        {{ buildBtnText }}
      </n-button>
      <n-button
        v-if="buildRunning"
        size="small"
        secondary
        class="rag-build-cancel-btn"
        title="取消向量库构建(已构建部分保留)"
        @click="onCancelBuild"
      >
        取消
      </n-button>
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
  flex-direction: column;
  gap: 6px;
  padding: 0 14px 10px;
  margin-bottom: 8px;
  border-bottom: 1px solid var(--color-border);
}

/* 两行布局:第一行=模式切换+检索框(主操作),第二行=排序+批量动作(辅助)。 */
.filter-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.filter-row :deep(.n-input) {
  flex: 1;
  min-width: 0;
}

.sort-sel {
  flex: 0 0 96px;
}

.mode-sel {
  flex: 0 0 96px;
}

.action-spacer {
  flex: 1;
  min-width: 0;
}

.rag-build-btn {
  flex: none;
}

.rag-build-cancel-btn {
  flex: none;
}

.kf-batch-btn {
  flex: none;
}

.report-del-btn {
  flex: none;
}

/* 极窄侧栏:第二行按钮文字截断而非换行挤压。 */
@media (max-width: 260px) {
  .rag-build-btn,
  .kf-batch-btn,
  .report-del-btn {
    max-width: 96px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}
</style>
