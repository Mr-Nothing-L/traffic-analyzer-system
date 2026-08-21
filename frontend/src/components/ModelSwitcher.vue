<script setup lang="ts">
/** 顶栏「模型管理」按钮 + 居中弹窗:provider 列表(选主用/加入自动切换池/删除)+ 新增编辑区
 * + 底部「失败时自动切换」总开关。数据走 stores/llm(/api/llm/providers*,操作后整体回填)。
 * 触发按钮样式对齐 TopBar 的 .ws-btn。 */
import { ref, watch } from 'vue'
import {
  NButton, NCheckbox, NInput, NModal, NPopconfirm, NRadio, NRadioGroup, NSelect, NSwitch,
  useMessage,
} from 'naive-ui'
import { useLlmStore } from '../stores/llm'
import type { LlmProvider } from '../stores/llm'
import UiIcon from './UiIcon.vue'

const llm = useLlmStore()
const message = useMessage()

const PROVIDER_OPTIONS = [
  { label: 'anthropic', value: 'anthropic' },
  { label: 'google', value: 'google' },
  { label: 'aliyun', value: 'aliyun' },
]

const modalShow = ref(false)
const activeIndex = ref<number | null>(null) // radio 选中 = 主用行 index(主用恒为列表 index 0 行)

/* ---- 各操作忙态(防重复点击) ---- */
const activating = ref(false)
const togglingIndex = ref<number | null>(null)
const deletingIndex = ref<number | null>(null)
const switching = ref(false)

/* ---- 新增编辑区 ---- */
const editOpen = ref(false)
const editProvider = ref<string | null>(null)
const editModel = ref('')
const editBaseUrl = ref('')
const editApiKey = ref('')
const saving = ref(false)

/** 回填 radio 选中:主用永远是响应重排后列表的第一行。 */
function syncActive() {
  activeIndex.value = llm.llmProviders[0]?.index ?? null
}

watch(modalShow, async (open) => {
  if (!open) return
  editOpen.value = false
  if (!llm.llmLoaded) {
    const r = await llm.fetchLlmProviders()
    if (!r.ok) message.error(r.message || '加载模型配置失败')
  }
  syncActive()
})

function fmtLine(p: LlmProvider): string {
  return [p.provider, p.model, p.base_url, p.api_key_masked].filter(Boolean).join(' · ')
}

/** 改 radio → 设为主用(后端把该行移到首位)。 */
async function onSetActive(index: number) {
  if (activating.value || index === activeIndex.value) return
  activating.value = true
  try {
    const r = await llm.setActive(index)
    if (r.ok) message.success('已设为主用')
    else message.error(r.message || '操作失败')
  } finally {
    activating.value = false
    syncActive() // 成功取重排后的 index 0;失败回退到原主用
  }
}

/** 改 checkbox → 该行加入/移出自动切换池(model/base_url/api_key 不传 = 沿用)。 */
async function onToggleEnabled(p: LlmProvider, enabled: boolean) {
  if (togglingIndex.value != null) return
  togglingIndex.value = p.index
  try {
    const r = await llm.saveProvider({ index: p.index, provider: p.provider, enabled })
    if (r.ok) message.success('已更新')
    else message.error(r.message || '操作失败')
  } finally {
    togglingIndex.value = null
    syncActive()
  }
}

async function onDelete(p: LlmProvider) {
  if (deletingIndex.value != null) return
  deletingIndex.value = p.index
  try {
    const r = await llm.deleteProvider(p.index)
    if (r.ok) message.success('已删除')
    else message.error(r.message || '删除失败')
  } finally {
    deletingIndex.value = null
    syncActive()
  }
}

async function onAutoSwitch(v: boolean) {
  if (switching.value) return
  switching.value = true
  try {
    const r = await llm.setAutoSwitch(v)
    if (r.ok) message.success('已更新')
    else message.error(r.message || '操作失败')
  } finally {
    switching.value = false
  }
}

function clearEdit() {
  editProvider.value = null
  editModel.value = ''
  editBaseUrl.value = ''
  editApiKey.value = ''
}

/** 编辑区保存 = 新增(index=null);改已有行的 key = 删除后重新添加(不提供行内编辑)。 */
async function onSaveNew() {
  if (saving.value) return
  if (!editProvider.value) {
    message.warning('请选择 provider')
    return
  }
  saving.value = true
  try {
    const r = await llm.saveProvider({
      index: null,
      provider: editProvider.value,
      enabled: true,
      ...(editModel.value.trim() ? { model: editModel.value.trim() } : {}),
      ...(editBaseUrl.value.trim() ? { base_url: editBaseUrl.value.trim() } : {}),
      ...(editApiKey.value ? { api_key: editApiKey.value } : {}),
    })
    if (r.ok) {
      message.success('已保存')
      editOpen.value = false
      clearEdit()
    } else {
      message.error(r.message || '保存失败')
    }
  } finally {
    saving.value = false
    syncActive()
  }
}
</script>

<template>
  <button class="ms-btn" title="管理 LLM provider" @click="modalShow = true">
    <UiIcon name="chip" :size="14" />
    <span class="ms-label">模型管理</span>
  </button>

  <n-modal
    v-model:show="modalShow"
    preset="card"
    title="模型管理"
    style="width: 680px"
  >
    <n-radio-group
      :value="activeIndex"
      class="ms-list"
      @update:value="onSetActive"
    >
      <div v-for="p in llm.llmProviders" :key="p.index" class="ms-row">
        <n-radio :value="p.index" :disabled="activating" class="ms-radio" />
        <n-checkbox
          :checked="p.enabled"
          :disabled="togglingIndex != null"
          @update:checked="(v: boolean) => onToggleEnabled(p, v)"
        />
        <span class="ms-info" :title="fmtLine(p)">{{ fmtLine(p) }}</span>
        <span v-if="p.index === 0" class="ms-tag">主用</span>
        <n-popconfirm @positive-click="onDelete(p)">
          <template #trigger>
            <n-button
              quaternary
              size="tiny"
              :loading="deletingIndex === p.index"
              :disabled="deletingIndex != null"
              title="删除"
            >
              <UiIcon name="close" :size="12" />
            </n-button>
          </template>
          删除该 provider?
        </n-popconfirm>
      </div>
      <div v-if="!llm.llmProviders.length" class="ms-empty">
        {{ llm.llmLoaded ? '暂无已配置 provider' : '加载中…' }}
      </div>
    </n-radio-group>

    <n-button dashed size="small" class="ms-add" @click="editOpen = !editOpen">+</n-button>

    <div v-show="editOpen" class="ms-edit">
      <n-select
        v-model:value="editProvider"
        :options="PROVIDER_OPTIONS"
        placeholder="provider"
        size="small"
      />
      <n-input v-model:value="editModel" size="small" placeholder="模型名称" />
      <n-input v-model:value="editBaseUrl" size="small" placeholder="Base URL" />
      <n-input
        v-model:value="editApiKey"
        size="small"
        type="password"
        show-password-on="click"
        placeholder="API Key"
      />
      <n-button
        type="primary"
        size="small"
        :loading="saving"
        @click="onSaveNew"
      >
        保存
      </n-button>
    </div>

    <template #footer>
      <div class="ms-footer">
        <span class="ms-hint">所有改动立即写入 .env,对之后新提交的推理任务生效</span>
        <span class="ms-autoswitch">
          <n-switch
            :value="llm.autoSwitch"
            size="small"
            :loading="switching"
            @update:value="onAutoSwitch"
          />
          <span>失败时自动切换</span>
        </span>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
/* 触发按钮:对齐 TopBar .ws-btn(scoped 无法直接复用,复制同套样式)。 */
.ms-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--color-border);
  background: var(--color-card);
  color: var(--color-text);
  border-radius: var(--radius-sm);
  padding: 5px 10px;
  cursor: pointer;
  font-family: var(--font-pixel);
  font-size: var(--text-sm);
  transition:
    border-color var(--dur-fast) var(--ease-out),
    background var(--dur-fast) var(--ease-out);
}

.ms-btn:hover {
  border-color: var(--color-accent);
  background: var(--color-hover-bg);
}

.ms-btn:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: 1px;
}

.ms-btn:active {
  background: var(--color-accent-soft);
}

.ms-label {
  font-weight: 600;
}

.ms-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.ms-row {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
}

.ms-radio {
  margin-right: 0;
}

.ms-info {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: var(--text-sm);
}

.ms-tag {
  flex-shrink: 0;
  padding: 0 6px;
  border-radius: var(--radius-sm);
  background: var(--color-accent-soft);
  color: var(--color-accent);
  font-size: var(--text-sm);
}

.ms-empty {
  color: var(--color-text2);
  font-size: var(--text-sm);
}

.ms-add {
  width: 100%;
  margin-top: var(--space-sm);
}

.ms-edit {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: var(--space-sm);
}

.ms-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-md);
}

.ms-hint {
  color: var(--color-text2);
  font-size: var(--text-sm);
}

.ms-autoswitch {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  font-size: var(--text-sm);
  flex-shrink: 0;
}
</style>
