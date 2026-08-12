<script setup lang="ts">
/** SFT 标注编辑卡(替换阶段 4 的 SftCard 只读占位):元信息 + 按事件分框编辑器 +
 * 未归类原文 + 答案区 + 保存/重置。编排对齐 legacy sft.js renderSftBody/saveSft;
 * 乐观锁 base_sig 取 GET 响应的 file_sig,409 弹「丢弃并刷新/保留我的修改」。 */
import { nextTick, onUnmounted, ref, watch } from 'vue'
import { NButton, NCard, useDialog, useMessage } from 'naive-ui'
import type { SftLabel as ApiSftLabel } from '../../../api/results'
import type { SftLabel } from '../../../sft/types'
import { useEvidenceStore } from '../../../stores/evidence'
import { useSftStore } from '../../../stores/sft'
import SftAnswer from './SftAnswer.vue'
import SftEventCard from './SftEventCard.vue'

const props = defineProps<{ stem: string; sft: ApiSftLabel | null; fileSig: string | null; rawAction: number[] | null }>()
const store = useSftStore()
const evStore = useEvidenceStore()
const message = useMessage()
const dialog = useDialog()
const savedFlash = ref(false) // 保存成功反馈:按钮短暂显示「已保存」(同 legacy flashSaveBtn)
const unmatchedEl = ref<HTMLTextAreaElement | null>(null)

// chunk 时间戳统一一位小数(2.5s → 2.5s,整数秒也补 .0),元信息列宽稳定(同 legacy)
const sec = (v: unknown) => (typeof v === 'number' ? v.toFixed(1) : String(v))

watch(
  () => [props.stem, props.sft, props.fileSig, props.rawAction] as const,
  async ([stem, sft, fileSig, rawAction]) => {
    if (!sft) {
      store.clear() // 无 SFT 标注:清掉上一个视频的草稿,避免幽灵 dirty 态
      return
    }
    await store.init(stem, sft as unknown as SftLabel, fileSig, rawAction)
    nextTick(() => unmatchedEl.value && grow(unmatchedEl.value))
  },
  { immediate: true },
)
onUnmounted(() => store.clear())

// 未归类原文(只读)自适应高度(同 legacy autoGrow)
function grow(ta: HTMLTextAreaElement) {
  ta.style.height = 'auto'
  const need = ta.scrollHeight + (ta.offsetHeight - ta.clientHeight)
  const capped = need > 300
  ta.style.height = (capped ? 300 : need) + 'px'
  ta.style.overflowY = capped ? 'auto' : 'hidden'
}

async function onSave() {
  const r = await store.save()
  if (r.ok) {
    message.success('已保存')
    savedFlash.value = true
    setTimeout(() => (savedFlash.value = false), 1000)
    return
  }
  if (r.conflict) {
    // 乐观锁冲突:他人已修改;重载会丢弃当前未保存的修改,先确认(同 legacy confirm)
    dialog.warning({
      title: '保存冲突',
      content: '该视频的标注已被他人修改。可丢弃当前未保存的修改并刷新为最新版本。',
      positiveText: '丢弃并刷新',
      negativeText: '保留我的修改',
      onPositiveClick: async () => {
        await evStore.load(props.stem) // 重载结果 → watch 重建草稿
        message.warning('他人已修改,已为你刷新')
      },
    })
    return
  }
  message.error(`保存失败(${r.status ?? 0}):${r.message || '未知错误'}`)
}

function onReset() {
  store.resetLocal()
  message.success('已重置为磁盘版本')
}
</script>

<template>
  <n-card class="card-sft">
    <template #header>
      <span class="card-head">SFT 标注详情</span><span class="card-sub">{{ stem }}</span>
    </template>
    <div v-if="!sft" class="empty-note">无 SFT 标注</div>
    <template v-else>
      <div class="sft-meta">
        <span>{{ sft.chunk || '' }}</span>
        <span>idx: {{ sft.idx }}</span>
        <span>{{ sec(sft.start_timestamp) }}s → {{ sec(sft.end_timestamp) }}s</span>
        <span>{{ sft.chunk_name || '' }}</span>
      </div>
      <div v-if="store.configError" class="empty-note">
        事件配置加载失败:{{ store.configError }}
      </div>
      <div v-else-if="!store.events" class="empty-note">加载事件配置…</div>
      <template v-else-if="store.draft">
        <div class="sft-section-title">
          事件思考(按事件编辑;「检出」勾选在保存时联动 action 与结论)
        </div>
        <SftEventCard v-for="ev in store.events" :key="ev.event_id" :ev="ev" />
        <template v-if="store.draft.unmatched.length">
          <div class="sft-section-title">未归类原文(只读,保存时原样附加到思考末尾)</div>
          <textarea
            ref="unmatchedEl"
            class="sft-ev-text sft-unmatched"
            readonly
            rows="2"
            :value="store.draft.unmatched.join('\n\n')"
          />
        </template>
        <SftAnswer />
        <div class="sft-actions">
          <span v-if="store.dirty" class="dirty-flag">● 未保存</span>
          <n-button size="small" quaternary :disabled="!store.dirty" @click="onReset">
            重置
          </n-button>
          <n-button
            size="small"
            type="primary"
            :disabled="!store.dirty"
            :loading="store.saving"
            @click="onSave"
          >
            {{ savedFlash ? '已保存' : '保存' }}
          </n-button>
        </div>
      </template>
    </template>
  </n-card>
</template>
