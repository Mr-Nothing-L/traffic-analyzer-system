<script setup lang="ts">
/** 顶栏「模型切换」按钮 + 面板:查看/切换 LLM provider,或手动输入新 provider。
 * 数据走 stores/llm(GET/POST /api/llm/providers);触发按钮样式对齐 TopBar 的 .ws-btn。 */
import { computed, ref, watch } from 'vue'
import { NButton, NInput, NPopover, NRadio, NRadioGroup, NSwitch, useMessage } from 'naive-ui'
import { useLlmStore } from '../stores/llm'
import UiIcon from './UiIcon.vue'

const llm = useLlmStore()
const message = useMessage()

const popoverShow = ref(false)
const selectedIndex = ref<number | null>(null)
const manualMode = ref(false)
const manualProvider = ref('')
const manualModel = ref('')
const manualBaseUrl = ref('')
const manualApiKey = ref('')
const saving = ref(false)

/** 当前激活 provider(index 0);按钮文案据此显示。 */
const activeProvider = computed(() => llm.llmProviders[0] ?? null)
const triggerLabel = computed(() => {
  const p = activeProvider.value
  if (!p) return '模型…'
  return p.model ? `${p.provider} / ${p.model}` : p.provider
})

function fmtProviderLine(p: { provider: string; model: string | null; api_key_masked: string | null }) {
  return [p.provider, p.model, p.api_key_masked].filter(Boolean).join(' · ')
}

function clearManual() {
  manualProvider.value = ''
  manualModel.value = ''
  manualBaseUrl.value = ''
  manualApiKey.value = ''
}

/** 打开面板:首次拉取;每次打开按最新状态回填选择与开关。 */
async function onShowChange(show: boolean) {
  popoverShow.value = show
  if (!show) return
  if (!llm.llmLoaded) {
    const r = await llm.fetchLlmProviders()
    if (!r.ok) message.error(r.message || '加载模型配置失败')
  }
  selectedIndex.value = activeProvider.value?.index ?? null
  manualMode.value = false
  clearManual()
}

/** 手动模式开关:进手动清 radio,退手动清输入并回到当前激活项。 */
watch(manualMode, (on) => {
  if (on) selectedIndex.value = null
  else {
    clearManual()
    selectedIndex.value = activeProvider.value?.index ?? null
  }
})

/** 选 radio 即退出手动模式(互斥)。 */
watch(selectedIndex, (v) => {
  if (v != null && manualMode.value) manualMode.value = false
})

async function onSave() {
  if (saving.value) return
  let payload
  if (manualMode.value) {
    const provider = manualProvider.value.trim()
    if (!provider) {
      message.warning('请填写 provider 名称')
      return
    }
    payload = {
      active_index: null,
      new_provider: {
        provider,
        ...(manualModel.value.trim() ? { model: manualModel.value.trim() } : {}),
        ...(manualBaseUrl.value.trim() ? { base_url: manualBaseUrl.value.trim() } : {}),
        ...(manualApiKey.value ? { api_key: manualApiKey.value } : {}),
      },
      auto_switch: llm.autoSwitch,
    }
  } else {
    if (selectedIndex.value == null) {
      message.warning('请选择一个 provider')
      return
    }
    payload = { active_index: selectedIndex.value, new_provider: null, auto_switch: llm.autoSwitch }
  }
  saving.value = true
  try {
    const r = await llm.saveLlmProviders(payload)
    if (r.ok) {
      message.success('已保存')
      popoverShow.value = false
    } else {
      message.error(r.message || '保存失败')
    }
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <n-popover
    :show="popoverShow"
    trigger="click"
    placement="bottom-end"
    @update:show="onShowChange"
  >
    <template #trigger>
      <button class="ms-btn" title="切换 LLM provider">
        <UiIcon name="chip" :size="14" />
        <span class="ms-label">{{ triggerLabel }}</span>
      </button>
    </template>
    <div class="ms-pop">
      <div class="ms-section-title">选择 provider(当前为第一项)</div>
      <n-radio-group v-model:value="selectedIndex" class="ms-radios">
        <n-radio
          v-for="p in llm.llmProviders"
          :key="p.index"
          :value="p.index"
          :disabled="manualMode"
          class="ms-radio"
        >
          {{ fmtProviderLine(p) }}
        </n-radio>
        <div v-if="!llm.llmProviders.length" class="ms-empty">
          {{ llm.llmLoaded ? '暂无已配置 provider' : '加载中…' }}
        </div>
      </n-radio-group>

      <div class="ms-manual-toggle">
        <n-switch v-model:value="manualMode" size="small" />
        <span>手动输入 provider</span>
      </div>
      <div v-show="manualMode" class="ms-manual">
        <n-input v-model:value="manualProvider" size="small" placeholder="provider 名称(必填)" />
        <n-input v-model:value="manualModel" size="small" placeholder="model(可选)" />
        <n-input v-model:value="manualBaseUrl" size="small" placeholder="base_url(可选)" />
        <n-input
          v-model:value="manualApiKey"
          size="small"
          type="password"
          show-password-on="click"
          placeholder="留空则沿用 .env 已保存密钥"
        />
      </div>

      <div class="ms-autoswitch">
        <n-switch v-model:value="llm.autoSwitch" size="small" />
        <span>失败时自动切换到其他 provider</span>
      </div>

      <div class="ms-hint">保存后对之后新提交的推理任务生效,运行中任务不受影响</div>

      <n-button
        type="primary"
        size="small"
        class="ms-save"
        :loading="saving"
        @click="onSave"
      >
        保存
      </n-button>
    </div>
  </n-popover>
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
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ms-pop {
  width: 300px;
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
}

.ms-section-title {
  font-size: var(--text-sm);
  font-weight: 600;
}

.ms-radios {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.ms-radio {
  font-family: var(--font-mono);
  font-size: var(--text-sm);
}

.ms-empty {
  color: var(--color-text2);
  font-size: var(--text-sm);
}

.ms-manual-toggle,
.ms-autoswitch {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  font-size: var(--text-sm);
}

.ms-manual {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.ms-hint {
  color: var(--color-text2);
  font-size: var(--text-sm);
}

.ms-save {
  align-self: flex-end;
}
</style>
