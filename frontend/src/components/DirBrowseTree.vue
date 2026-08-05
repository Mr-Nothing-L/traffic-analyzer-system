<script setup lang="ts">
/** 「手动浏览」顶部的「默认工作区」下拉:白名单根为一级节点,子目录经 /fs/list 懒加载逐层下钻。
 * 白名单为空(或拉取失败)回退 localStorage 最近列表(不可展开);
 * 选中节点 = 跳到该目录(emit jump,弹窗复用 navDir),用户可继续浏览或点「选择此文件夹」。
 * 节点标签只显示目录名(长名中间省略,复用 utils/text.ts),完整路径放 title。 */
import { h, ref, watch } from 'vue'
import { NTreeSelect } from 'naive-ui'
import type { TreeSelectOption, TreeSelectRenderLabel } from 'naive-ui'
import { apiFetch } from '../api/client'
import { useWorkspaceStore } from '../stores/workspace'
import { ellipsisMiddle } from '../utils/text'
import { rootNameOf } from '../utils/quickPick'
import type { QuickDirRoot } from '../utils/quickPick'

const NAME_MAX = 26 // 树节点标签长度上限(完整路径放 title)
const RECENT_MAX = 40 // 回退项(整路径)显示上限

/** naive-ui 的 TreeSelectOption 类型 Omit 掉了 isLeaf,但运行时(treemate)认它:
 * 官方异步加载示例即靠 isLeaf:false 标记可展开节点,这里把类型补回。 */
type DirOption = TreeSelectOption & {
  isLeaf?: boolean
  children?: DirOption[]
  fullPath?: string
}

const props = defineProps<{ active: boolean; disabled?: boolean }>()
const emit = defineEmits<{ jump: [string] }>()

const ws = useWorkspaceStore()

const options = ref<DirOption[]>([])
const value = ref<string | null>(null)
const loading = ref(false)

// 弹窗打开(或切入手动浏览,组件随 v-else 重挂载)时重建选项
watch(
  () => props.active,
  (on) => {
    if (on) loadRoots()
  },
  { immediate: true },
)

async function loadRoots() {
  loading.value = true
  options.value = []
  value.value = null
  try {
    const data = await apiFetch<{ roots: QuickDirRoot[] }>('/workspace/quick-dirs')
    // 有白名单:根为一级节点(有 subs 才可展开);否则回退最近列表
    options.value = data.roots?.length
      ? data.roots.map((r) => makeDirOption(r.path, r.subs.length > 0))
      : recentOptions()
  } catch {
    options.value = recentOptions() // 拉取失败同空白名单处理,不阻塞浏览
  } finally {
    loading.value = false
  }
}

/** 最近列表回退:平铺、不可展开;无目录名可用,标签显示省略后的整路径。 */
function recentOptions(): DirOption[] {
  return ws.loadRecent().map((p) => ({
    key: p,
    label: ellipsisMiddle(p, RECENT_MAX),
    fullPath: p,
    isLeaf: true,
  }))
}

/** 目录节点:标签只显示目录名(长名中间省略),完整路径经 fullPath 放 title。 */
function makeDirOption(path: string, expandable: boolean): DirOption {
  return {
    key: path,
    label: ellipsisMiddle(rootNameOf(path), NAME_MAX),
    fullPath: path,
    isLeaf: !expandable,
  }
}

/** 懒加载:展开节点时经 /fs/list 拉一层子目录作为子节点,可继续下钻。 */
async function onLoad(node: TreeSelectOption): Promise<void> {
  const opt = node as DirOption
  try {
    const data = await apiFetch<{ dirs: { name: string; path: string }[] }>(
      `/fs/list?path=${encodeURIComponent(String(opt.key))}`,
    )
    opt.children = (data.dirs || []).map((d) => makeDirOption(d.path, true))
  } catch {
    opt.children = [] // 拉取失败:留空不报错,手动浏览主体仍可继续
  }
}

/** 节点渲染:span 带 title=完整路径;样式类见 dirpicker.css(.dbt-label)。 */
const renderLabel: TreeSelectRenderLabel = ({ option }) => {
  const o = option as DirOption
  return h(
    'span',
    { class: 'dbt-label', title: o.fullPath ?? String(o.label ?? '') },
    String(o.label ?? ''),
  )
}

/** 选中 = 跳到该目录继续浏览;跳转后清空选中,下拉可当跳转菜单反复使用。 */
function onSelect(v: string | number | null) {
  if (typeof v === 'string' && v) emit('jump', v)
  value.value = null
}
</script>

<template>
  <div class="dbt">
    <n-tree-select
      :options="options"
      :value="value"
      :on-load="onLoad"
      :render-label="renderLabel"
      :loading="loading"
      :disabled="disabled"
      size="small"
      clearable
      placeholder="默认工作区"
      aria-label="默认工作区"
      @update:value="onSelect"
    />
  </div>
</template>
