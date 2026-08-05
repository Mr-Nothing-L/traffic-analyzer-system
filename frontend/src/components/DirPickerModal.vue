<script setup lang="ts">
/** 工作区目录选择弹窗:「快捷选择 | 手动浏览」双模式。
 * 快捷选择 = 白名单平铺清单(DirQuickPick);手动浏览 = 「默认工作区」树下拉(DirBrowseTree,
 * 选中即跳目录)+ 面包屑浏览器(进入/回上级/手动输入)。
 * 上次模式记 localStorage(ta_dirpicker_mode);两模式共用底部路径栏 + 取消/选择按钮;
 * 确认期间锁定弹窗(applying),禁止任何途径关闭。 */
import { computed, ref, watch } from 'vue'
import { NButton, NModal, useMessage } from 'naive-ui'
import { ApiError, apiFetch } from '../api/client'
import { useWorkspaceStore } from '../stores/workspace'
import DirQuickPick from './DirQuickPick.vue'
import DirBrowseTree from './DirBrowseTree.vue'
import UiIcon from './UiIcon.vue'

const MODE_KEY = 'ta_dirpicker_mode' // 上次使用的模式:quick=快捷选择 / browse=手动浏览

const props = defineProps<{ show: boolean }>()
const emit = defineEmits<{ 'update:show': [boolean] }>()

const ws = useWorkspaceStore()
const message = useMessage()

const mode = ref<'quick' | 'browse'>(
  localStorage.getItem(MODE_KEY) === 'browse' ? 'browse' : 'quick',
)
const cwd = ref<string | null>(null)
const parent = ref<string | null>(null)
const dirs = ref<{ name: string; path: string }[]>([])
const selected = ref<string | null>(null)
const loading = ref(false)
const applying = ref(false) // confirm 后 applyWorkspace 期间锁定弹窗,禁止任何途径关闭
const showInput = ref(false)
const inputValue = ref('')

let navSeq = 0 // 导航竞态防护:只落地最后一次响应(同 legacy)

watch(mode, (m) => {
  try {
    localStorage.setItem(MODE_KEY, m)
  } catch {
    // 存储不可用时静默忽略(模式记忆仅是体验增强)
  }
  if (m === 'browse' && !cwd.value) navDir(ws.path || null) // 首次切入手动浏览才拉目录
})

watch(
  () => props.show,
  (open) => {
    if (!open) return
    showInput.value = false
    selected.value = null
    if (mode.value === 'browse') navDir(ws.path || null) // 无 path 时后端回退到当前工作区或主目录
  },
)

async function navDir(path: string | null) {
  const seq = ++navSeq
  loading.value = true
  let data: { path: string; parent: string | null; dirs: { name: string; path: string }[] } | null =
    null
  try {
    data = await apiFetch(path ? `/fs/list?path=${encodeURIComponent(path)}` : '/fs/list')
  } catch (e) {
    if (seq === navSeq) {
      const err = e as ApiError
      message.error(`读取目录失败(${err.status ?? '?'}):${err.message ?? e}`)
    }
  }
  if (seq !== navSeq) return // 已有更新的导航请求:过期响应丢弃
  loading.value = false
  if (data) {
    cwd.value = data.path
    parent.value = data.parent
    dirs.value = data.dirs || []
    selected.value = data.path // 当前目录即默认选择
    showInput.value = false
  }
}

/** 面包屑段:{label, path} 序列,根为 "/"。 */
const crumbs = computed(() => {
  const parts = (cwd.value || '/').split('/').filter(Boolean)
  const out: { label: string; path: string }[] = [{ label: '/', path: '/' }]
  let acc = ''
  parts.forEach((p) => {
    acc += '/' + p
    out.push({ label: p, path: acc })
  })
  return out
})

function onInputEnter() {
  const p = inputValue.value.trim()
  if (p) navDir(p)
}

function showManualInput() {
  showInput.value = true
  inputValue.value = cwd.value || ''
}

/** 快捷选择:单击填入底部路径栏。 */
function onQuickPick(p: string) {
  selected.value = p
}

/** 快捷选择:双击/Enter 立即确认(与底部「选择此文件夹」同路径)。 */
function onQuickConfirm(p: string) {
  selected.value = p
  confirmDir()
}

function tryClose() {
  if (applying.value) return // 加载期间禁止关闭(✕/Esc/遮罩统一拦截)
  emit('update:show', false)
}

async function confirmDir() {
  const p = selected.value || cwd.value
  if (!p || applying.value) return
  applying.value = true
  try {
    const res = await ws.applyWorkspace(p)
    ws.pushRecent(res.path)
    message.success(`已选择工作区:${res.path}`)
    emit('update:show', false)
  } catch (e) {
    const err = e as ApiError
    message.error(`设置工作区失败(${err.status ?? '?'}):${err.message ?? e}`)
  } finally {
    applying.value = false
  }
}
</script>

<template>
  <n-modal
    :show="show"
    :mask-closable="!applying"
    :close-on-esc="!applying"
    @update:show="tryClose"
  >
    <div class="dir-dialog" role="dialog" aria-modal="true" aria-label="选择工作区">
      <div class="dir-head">
        <span class="dir-title">选择工作区</span>
        <button class="dir-close" title="关闭 (Esc)" :disabled="applying" @click="tryClose">
          <UiIcon name="close" :size="13" />
        </button>
      </div>
      <div class="dir-seg" role="tablist" aria-label="选择方式">
        <button
          class="dir-seg-btn"
          :class="{ on: mode === 'quick' }"
          role="tab"
          :aria-selected="mode === 'quick'"
          :disabled="applying"
          @click="mode = 'quick'"
        >
          快捷选择
        </button>
        <button
          class="dir-seg-btn"
          :class="{ on: mode === 'browse' }"
          role="tab"
          :aria-selected="mode === 'browse'"
          :disabled="applying"
          @click="mode = 'browse'"
        >
          手动浏览
        </button>
      </div>
      <DirQuickPick
        v-if="mode === 'quick'"
        :active="show"
        :applying="applying"
        @pick="onQuickPick"
        @confirm="onQuickConfirm"
      />
      <template v-else>
        <DirBrowseTree :active="show" :disabled="applying" @jump="navDir" />
        <div class="dir-pathbar">
          <div v-if="!showInput" class="dir-crumbs">
            <template v-for="(c, i) in crumbs" :key="c.path">
              <span v-if="i > 0" class="dir-crumb-sep">›</span>
              <span
                class="dir-crumb"
                :class="{ current: i === crumbs.length - 1 }"
                :title="c.path"
                @click="navDir(c.path)"
                >{{ c.label }}</span
              >
            </template>
          </div>
          <input
            v-else
            v-model="inputValue"
            class="dir-input"
            spellcheck="false"
            placeholder="输入绝对路径,回车跳转"
            @keydown.enter.prevent="onInputEnter"
            @keydown.esc.prevent="showInput = false"
          />
          <button class="dir-edit" title="手动输入路径" @click="showManualInput">
            <UiIcon name="edit" :size="13" />
          </button>
        </div>
        <div class="dir-list">
          <!-- 加载 spinner(legacy tree.css:175-185)替换原纯文字 -->
          <div v-if="loading" class="dir-state">
            <div class="dir-spinner" />
            加载中…
          </div>
          <template v-else-if="cwd">
            <div v-if="parent" class="dir-row dir-up" @click="navDir(parent)">
              <span class="dir-ico"><UiIcon name="up" :size="13" /></span>
              <span class="dir-name">..</span>
            </div>
            <div
              v-for="d in dirs"
              :key="d.path"
              class="dir-row"
              :class="{ selected: selected === d.path }"
              @click="selected = d.path"
              @dblclick="navDir(d.path)"
            >
              <span class="dir-ico"><UiIcon name="folder" :size="13" /></span>
              <span class="dir-name">{{ d.name }}</span>
            </div>
            <div v-if="!dirs.length" class="dir-state">此目录没有子文件夹</div>
          </template>
          <div v-else class="dir-state">无法读取目录,可点击右侧按钮手动输入路径</div>
        </div>
      </template>
      <div v-if="applying" class="dir-state">正在加载工作区,请稍候…</div>
      <div class="dir-foot">
        <span class="dir-selected" :title="selected || cwd || ''">{{ selected || cwd || '' }}</span>
        <n-button size="small" :disabled="applying" @click="tryClose">取消</n-button>
        <n-button type="primary" size="small" :loading="applying" @click="confirmDir">
          选择此文件夹
        </n-button>
      </div>
    </div>
  </n-modal>
</template>
