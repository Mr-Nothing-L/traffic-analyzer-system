<script setup lang="ts">
/** 「快捷选择」模式(弹窗默认):白名单根 + 一层子目录平铺清单。
 * 打开时拉 /api/workspace/quick-dirs;白名单为空回退 最近使用(localStorage)+ 主目录。
 * 交互:单击=填入底部路径栏(emit pick);双击/Enter=直接确认(emit confirm);
 * ↑↓ 移动高亮;顶部过滤框自动聚焦(子串匹配,纯逻辑见 utils/quickPick.ts)。 */
import { computed, nextTick, ref, watch } from 'vue'
import { apiFetch } from '../api/client'
import { useWorkspaceStore } from '../stores/workspace'
import { buildQuickItems, filterQuickItems } from '../utils/quickPick'
import { ellipsisMiddle } from '../utils/text'
import type { QuickDirRoot, QuickItem } from '../utils/quickPick'
import UiIcon from './UiIcon.vue'

const MAX_SUBS = 50 // 单根子目录渲染上限,超出折叠为「还有 N 个」尾行

const props = defineProps<{ active: boolean; applying: boolean }>()
const emit = defineEmits<{ pick: [string]; confirm: [string] }>()

const ws = useWorkspaceStore()

const items = ref<QuickItem[]>([])
const loading = ref(false)
const query = ref('')
const hiPath = ref<string | null>(null) // 键盘高亮项,按 path 锚定(过滤变化不丢)
const filterEl = ref<HTMLInputElement | null>(null)

/** 行模型:组头 / 可选项 / 超上限尾行。 */
type Row =
  | { kind: 'head'; key: string; name: string; path: string }
  | { kind: 'item'; key: string; item: QuickItem }
  | { kind: 'more'; key: string; count: number }

const rows = computed<Row[]>(() => {
  const matched = filterQuickItems(items.value, query.value)
  const groups = new Map<string, QuickItem[]>()
  matched.forEach((it) => {
    const arr = groups.get(it.rootPath) || []
    arr.push(it)
    groups.set(it.rootPath, arr)
  })
  const out: Row[] = []
  const filtering = !!query.value.trim() // 过滤中不截断(关键词本就是为收窄)
  groups.forEach((arr, rootPath) => {
    out.push({ kind: 'head', key: `h:${rootPath}`, name: arr[0].rootName, path: rootPath })
    const limit = filtering ? arr.length : 1 + MAX_SUBS // +1 为根本身项
    arr.slice(0, limit).forEach((item) => out.push({ kind: 'item', key: item.path, item }))
    if (arr.length > limit) out.push({ kind: 'more', key: `m:${rootPath}`, count: arr.length - limit })
  })
  return out
})

/** 可选行(键盘导航的作用域)。 */
const pickable = computed(() =>
  rows.value.filter((r): r is Extract<Row, { kind: 'item' }> => r.kind === 'item'),
)

// 清单/过滤变化后:原高亮仍可见则保留,否则落到第一项
watch(pickable, (list) => {
  if (!list.some((r) => r.item.path === hiPath.value)) hiPath.value = list[0]?.item.path ?? null
})

// 弹窗打开(或切回本模式)时:清空过滤、重拉清单、聚焦过滤框
watch(
  () => props.active,
  async (on) => {
    if (!on) return
    query.value = ''
    await load()
    await nextTick()
    filterEl.value?.focus()
  },
  { immediate: true },
)

async function load() {
  loading.value = true
  items.value = []
  try {
    const data = await apiFetch<{ roots: QuickDirRoot[] }>('/workspace/quick-dirs')
    if (data.roots?.length) items.value = buildQuickItems(data.roots)
    else await loadFallback() // 白名单为空:回退最近列表
  } catch {
    await loadFallback() // 拉取失败同空白名单处理,不阻塞选择
  } finally {
    loading.value = false
  }
}

/** 空白名单回退:localStorage 最近使用 + 主目录(路径由 /fs/list 空参解析)。 */
async function loadFallback() {
  const arr: QuickItem[] = ws.loadRecent().map((p) => ({
    path: p,
    label: p,
    rootPath: '__recent__',
    rootName: '最近使用',
    isRoot: false,
  }))
  try {
    const d = await apiFetch<{ path: string }>('/fs/list')
    arr.push({ path: d.path, label: '主目录', rootPath: '__recent__', rootName: '最近使用', isRoot: false })
  } catch {
    // 主目录解析失败仅省略该项
  }
  items.value = arr
}

function moveHi(step: number) {
  const list = pickable.value
  if (!list.length) return
  const i = list.findIndex((r) => r.item.path === hiPath.value)
  const next = (i + step + list.length) % list.length
  hiPath.value = list[next].item.path
  document.getElementById(`qp-${hiPath.value}`)?.scrollIntoView({ block: 'nearest' })
}

/** 键盘:↑↓ 移动高亮,Enter = 双击等价(直接确认)。挂在根节点,过滤框/行内按键均冒泡至此。 */
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    moveHi(1)
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    moveHi(-1)
  } else if (e.key === 'Enter' && hiPath.value && !props.applying) {
    e.preventDefault()
    emit('confirm', hiPath.value)
  }
}

function onPick(item: QuickItem) {
  hiPath.value = item.path
  emit('pick', item.path)
}

function onConfirm(item: QuickItem) {
  if (props.applying) return
  hiPath.value = item.path
  emit('confirm', item.path)
}
</script>

<template>
  <div class="qp" @keydown="onKeydown">
    <div class="dir-pathbar qp-filter">
      <input
        ref="filterEl"
        v-model="query"
        class="dir-input"
        spellcheck="false"
        placeholder="输入关键词过滤(名称或路径)"
        :disabled="applying"
        aria-label="过滤目录"
      />
    </div>
    <div class="dir-list qp-list">
      <div v-if="loading" class="dir-state">
        <div class="dir-spinner" />
        加载中…
      </div>
      <template v-else-if="rows.length">
        <template v-for="row in rows" :key="row.key">
          <div v-if="row.kind === 'head'" class="qp-head" :title="row.path">{{ row.name }}</div>
          <div v-else-if="row.kind === 'more'" class="qp-more">
            还有 {{ row.count }} 个,输入关键词过滤
          </div>
          <div
            v-else
            :id="`qp-${row.item.path}`"
            class="qp-row"
            :class="{ active: hiPath === row.item.path, root: row.item.isRoot }"
            tabindex="0"
            :title="row.item.path"
            @mousedown.prevent
            @focus="hiPath = row.item.path"
            @click="onPick(row.item)"
            @dblclick="onConfirm(row.item)"
          >
            <span class="dir-ico"><UiIcon :name="row.item.isRoot ? 'home' : 'folder'" :size="13" /></span>
            <span class="qp-name">{{ row.item.label === row.item.path ? ellipsisMiddle(row.item.label) : row.item.label }}</span>
            <span v-if="row.item.label !== row.item.path" class="qp-path">{{ ellipsisMiddle(row.item.path) }}</span>
          </div>
        </template>
      </template>
      <div v-else-if="query.trim()" class="dir-state">没有匹配「{{ query.trim() }}」的目录</div>
      <div v-else class="dir-state">暂无可用目录,请切换到「手动浏览」</div>
    </div>
  </div>
</template>
