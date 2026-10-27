<script setup lang="ts">
/** SFT Forge 创建任务表单:来源(当前工作区勾选视频 / 指定目录)+
 * max_rounds(0-10,默认 2)+ 提交。勾选视频按工作区绝对路径拼 rel
 * 上送(videos[] 需绝对路径,见 web/sftforge/routes.py)。 */
import { computed, ref } from 'vue'
import { NButton, NInputNumber, useMessage } from 'naive-ui'
import { useSftForgeStore } from '../../stores/sftforge'
import { useWorkspaceStore } from '../../stores/workspace'

const forge = useSftForgeStore()
const ws = useWorkspaceStore()
const message = useMessage()

const mode = ref<'checked' | 'dir'>('checked')
const dirPath = ref('')
const maxRounds = ref(2)
const thinking = ref<'off' | 'low' | 'medium' | 'high'>('medium')
const thinkingOptions = [
  { value: 'off', label: '关闭' },
  { value: 'low', label: '低' },
  { value: 'medium', label: '中' },
  { value: 'high', label: '高' },
] as const
const draft = ref<'pipeline' | 'prompt'>('pipeline')
const draftOptions = [
  { value: 'pipeline', label: '批量链路' },
  { value: 'prompt', label: '快速单发' },
] as const

const checkedCount = computed(() => ws.checked.size)
const canSubmit = computed(() => {
  if (forge.posting || forge.running) return false
  if (mode.value === 'checked') return checkedCount.value > 0 && !!ws.path
  return !!dirPath.value.trim()
})

async function submit() {
  const body =
    mode.value === 'checked'
      ? {
          videos: ws.videos
            .filter((v) => ws.checked.has(v.rel))
            .map((v) => `${ws.path}/${v.rel}`),
          max_rounds: maxRounds.value,
          thinking: thinking.value,
          draft: draft.value,
        }
      : {
          dir: dirPath.value.trim(),
          max_rounds: maxRounds.value,
          thinking: thinking.value,
          draft: draft.value,
        }
  const ok = await forge.start(body)
  if (ok) message.success(`已创建 SFT 生成任务 #${forge.jobId}`)
  else if (forge.error) message.error(forge.error)
}
</script>

<template>
  <section class="ff-card">
    <div class="ff-card-head">
      <span class="ff-card-title">创建生成任务</span>
      <span class="ff-card-sub">正向盲推 → 按真值逐轮纠错(漏检补段 / 误报复核)</span>
    </div>
    <div class="ff-card-body ff-form">
      <span class="ff-label">视频来源</span>
      <span class="ff-seg" role="tablist" aria-label="视频来源">
        <button
          class="ff-seg-btn"
          :class="{ on: mode === 'checked' }"
          :disabled="forge.running"
          role="tab"
          @click="mode = 'checked'"
        >
          勾选视频({{ checkedCount }})
        </button>
        <button
          class="ff-seg-btn"
          :class="{ on: mode === 'dir' }"
          :disabled="forge.running"
          role="tab"
          @click="mode = 'dir'"
        >
          指定目录
        </button>
      </span>
      <input
        v-if="mode === 'dir'"
        v-model="dirPath"
        class="ff-dir-input"
        spellcheck="false"
        placeholder="服务器视频目录绝对路径"
        :disabled="forge.running"
        @keydown.enter.prevent="submit"
      />
      <span v-else class="ff-card-sub">
        {{ ws.path ? '使用侧栏勾选的工作区视频' : '请先在侧栏选择工作区并勾选视频' }}
      </span>
      <span class="ff-label">最大纠错轮次</span>
      <n-input-number
        v-model:value="maxRounds"
        :min="0"
        :max="10"
        :disabled="forge.running"
        size="small"
        style="width: 90px"
      />
      <span class="ff-label">思考档位</span>
      <span class="ff-seg" role="tablist" aria-label="思考档位">
        <button
          v-for="opt in thinkingOptions"
          :key="opt.value"
          class="ff-seg-btn"
          :class="{ on: thinking === opt.value }"
          :disabled="forge.running"
          role="tab"
          @click="thinking = opt.value"
        >
          {{ opt.label }}
        </button>
      </span>
      <span class="ff-label">初稿链路</span>
      <span class="ff-seg" role="tablist" aria-label="初稿链路">
        <button
          v-for="opt in draftOptions"
          :key="opt.value"
          class="ff-seg-btn"
          :class="{ on: draft === opt.value }"
          :disabled="forge.running"
          role="tab"
          @click="draft = opt.value"
        >
          {{ opt.label }}
        </button>
      </span>
      <n-button
        type="primary"
        size="small"
        :disabled="!canSubmit"
        :loading="forge.posting"
        @click="submit"
      >
        开始生成
      </n-button>
    </div>
    <div v-if="forge.error && !forge.running" class="ff-form-error">{{ forge.error }}</div>
  </section>
</template>
