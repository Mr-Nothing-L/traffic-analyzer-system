/** LLM provider 切换状态:/api/llm/providers 拉取与保存(顶栏 ModelSwitcher 数据源)。 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { ApiError, apiFetch } from '../api/client'

export interface LlmProvider {
  index: number
  provider: string
  model: string | null
  base_url: string | null
  api_key_masked: string | null
  has_api_key: boolean
}

export interface LlmProvidersResp {
  providers: LlmProvider[]
  auto_switch: boolean
  env_path: string | null
}

export interface NewProviderInput {
  provider: string
  model?: string
  base_url?: string
  api_key?: string
}

export interface SaveLlmPayload {
  active_index: number | null
  new_provider: NewProviderInput | null
  auto_switch: boolean
}

/** action 结果:组件据此弹提示(错误 message 为后端 detail)。 */
export interface ActionResult {
  ok: boolean
  message?: string
}

export const useLlmStore = defineStore('llm', () => {
  const llmProviders = ref<LlmProvider[]>([])
  const autoSwitch = ref(true)
  const llmLoaded = ref(false)

  function applyResp(resp: LlmProvidersResp) {
    llmProviders.value = resp.providers || []
    autoSwitch.value = resp.auto_switch
    llmLoaded.value = true
  }

  async function fetchLlmProviders(): Promise<ActionResult> {
    try {
      applyResp(await apiFetch<LlmProvidersResp>('/llm/providers'))
      return { ok: true }
    } catch (e) {
      return {
        ok: false,
        message: e instanceof ApiError ? e.message : '加载失败',
      }
    }
  }

  async function saveLlmProviders(payload: SaveLlmPayload): Promise<ActionResult> {
    try {
      applyResp(await apiFetch<LlmProvidersResp>('/llm/providers', {
        method: 'POST',
        body: JSON.stringify(payload),
      }))
      return { ok: true }
    } catch (e) {
      return {
        ok: false,
        message: e instanceof ApiError ? e.message : '保存失败',
      }
    }
  }

  return { llmProviders, autoSwitch, llmLoaded, fetchLlmProviders, saveLlmProviders }
})
