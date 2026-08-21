/** LLM provider 管理状态:/api/llm/providers* 拉取与各项操作(顶栏 ModelSwitcher 数据源)。
 * 所有 POST 响应与 GET 同构,成功后整体回填;index 0 = 主用 provider。 */
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
  enabled: boolean
}

export interface LlmProvidersResp {
  providers: LlmProvider[]
  auto_switch: boolean
  env_path: string | null
}

/** save 载荷:index=null 追加,index=i 覆盖;model/base_url/api_key 不传 = 沿用现有值。 */
export interface SaveProviderPayload {
  index: number | null
  provider: string
  model?: string
  base_url?: string
  api_key?: string
  enabled: boolean
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

  async function post(path: string, body: unknown): Promise<ActionResult> {
    try {
      applyResp(await apiFetch<LlmProvidersResp>(path, {
        method: 'POST',
        body: JSON.stringify(body),
      }))
      return { ok: true }
    } catch (e) {
      return {
        ok: false,
        message: e instanceof ApiError ? e.message : '操作失败',
      }
    }
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

  function saveProvider(payload: SaveProviderPayload): Promise<ActionResult> {
    return post('/llm/providers/save', payload)
  }

  function deleteProvider(index: number): Promise<ActionResult> {
    return post('/llm/providers/delete', { index })
  }

  function setActive(index: number): Promise<ActionResult> {
    return post('/llm/providers/active', { index })
  }

  function setAutoSwitch(v: boolean): Promise<ActionResult> {
    return post('/llm/providers/settings', { auto_switch: v })
  }

  return {
    llmProviders, autoSwitch, llmLoaded,
    fetchLlmProviders, saveProvider, deleteProvider, setActive, setAutoSwitch,
  }
})
