<script setup lang="ts">
/** 单事件 SFT 编辑卡:事件头(名称/未激活标/检出勾选)+ chips + 富文本框。
 * chip 变更编排:改草稿(纯函数)→ 重渲染文本框(pulse 该组);hover 双向联动在此桥接。 */
import { computed, ref } from 'vue'
import { applyChipChange } from '../../../sft/chips'
import { evOptions, extractGtFromFilename } from '../../../sft/model'
import type { AttrGroup, EventDef } from '../../../sft/types'
import { useSftStore } from '../../../stores/sft'
import { useWorkspaceStore } from '../../../stores/workspace'
import ChipGroup from './ChipGroup.vue'
import TokenText from './TokenText.vue'

const props = defineProps<{ ev: EventDef }>()
const store = useSftStore()
const ws = useWorkspaceStore()

// 标注(GT):从视频文件名解析的事件 ID 集,该事件在集中 → ✓
const gtSet = computed(() => {
  const rel = ws.currentRel
  if (!rel) return new Set<number>()
  const v = ws.videoByRel.get(rel)
  return v ? extractGtFromFilename(v.name) : new Set<number>()
})
const gtHas = computed(() => gtSet.value.has(props.ev.event_id))

// 推理:优先使用模型原始推理(rawAction,保存后仍不变),回退到当前 sftLabel.action
const infHas = computed(() => {
  const action = store.rawAction ?? store.sftLabel?.action
  return Array.isArray(action) && action.includes(props.ev.event_id)
})

// 人工修正:当前草稿检出勾选与原始推理态不一致 → 推理标记变琥珀色(保存后仍保持)
const corrected = computed(() => {
  const draftChecked = !!store.draft?.checks[props.ev.event_id]
  return draftChecked !== infHas.value
})

const root = ref<HTMLElement | null>(null)
const textRef = ref<InstanceType<typeof TokenText> | null>(null)

const opts = computed(() => (props.ev.is_active ? evOptions(props.ev) : []))
// chips 仅在该事件有声明提及(attr_mentions)时渲染;无声明退化为纯文本卡(同 legacy)
const hasDecl = computed(() => {
  const m = store.draft?.mentions
  return !!(m && m[props.ev.event_id])
})
const attrs = computed(() => store.draft?.attrs[props.ev.event_id])

function onChipChange(group: AttrGroup, value: string) {
  const d = store.draft
  if (!d || !store.events) return
  applyChipChange(d, store.events, props.ev, group, value)
  textRef.value?.refresh(group.key) // 重渲染 token + 该组脉冲(同 legacy renderTokens)
}

// chip hover ↔ token 联动:on 时同事件卡内同组 token 加深高亮,异组不变(仅切 class)
function onChipHover(group: string | null) {
  root.value?.querySelectorAll('.sft-tok').forEach((tok) => {
    tok.classList.toggle('sft-tok-link', !!group && (tok as HTMLElement).dataset.attr === group)
  })
}

// token hover 反向联动:同事件卡内同组 chips 加描边提示
function onTokHover(group: string | null) {
  root.value?.querySelectorAll('.sft-chip').forEach((c) => {
    c.classList.toggle('sft-chip-link', !!group && (c as HTMLElement).dataset.attr === group)
  })
}

function onCheck(e: Event) {
  if (!store.draft) return
  store.draft.checks[props.ev.event_id] = (e.target as HTMLInputElement).checked
}
</script>

<template>
  <div ref="root" class="sft-ev" :class="{ inactive: !ev.is_active }">
    <div class="sft-ev-head">
      <span class="sft-ev-name">{{ ev.name_zh }}</span>
      <span class="sft-mark sft-mark-gt" :class="{ 'mark-yes': gtHas, 'mark-no': !gtHas }">
        标注{{ gtHas ? '✓' : '✗' }}
      </span>
      <span
        class="sft-mark sft-mark-inf"
        :class="{ 'mark-yes': infHas && !corrected, 'mark-amber': corrected, 'mark-no': !infHas && !corrected }"
      >
        推理{{ infHas ? '✓' : '✗' }}
      </span>
      <span v-if="!ev.is_active" class="sft-ev-tag">未激活</span>
      <label class="sft-ev-check">
        <input
          type="checkbox"
          :checked="!!store.draft?.checks[ev.event_id]"
          @change="onCheck"
        />检出
      </label>
    </div>
    <ChipGroup
      v-if="opts.length && hasDecl"
      :groups="opts"
      :attrs="attrs"
      @change="onChipChange"
      @chip-hover="onChipHover"
    />
    <TokenText ref="textRef" :ev="ev" :has-decl="hasDecl" @tok-hover="onTokHover" />
  </div>
</template>
