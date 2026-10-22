<script setup lang="ts">
/** 事件文本富文本框(contenteditable):仅声明提及渲染为 token 分段,可自由编辑。
 * spike 结论(phase5):输入期间只回写纯文本、不更新分段,Vue 声明式渲染不丢光标;
 * 分段仅在挂载 / blur / chip 变更(refresh)时重算,与 legacy renderTokens 时机一致。
 * spike 结论的失效条件(2026-09-08 修复):人工编辑让浏览器直接删除 token span
 * 后,vdom 与真实 DOM 结构性分歧,blur 时的增量 patch(patchKeyedChildren)会把
 * 新分段写进游离节点、卸载存活文本节点,导致文本框视觉清空(草稿数据完好)。
 * 修复:refresh 一律整段重挂载(renderKey 自增驱动 div 重建),不与浏览器改过的
 * DOM 做增量对账;refresh 只发生在 blur/chip 变更/外部重建等本就该丢光标的时机。
 * TODO(后续重构):长期方案是 textarea + 高亮 overlay,彻底移除 contenteditable。 */
import { h, nextTick, onMounted, ref, watch } from 'vue'
import type { FunctionalComponent } from 'vue'
import { useMessage } from 'naive-ui'
import { declaredSpans, groupMentionStrings, tokenizeSpans } from '../../../sft/spans'
import type { EventDef, TokenSegment } from '../../../sft/types'
import { useSftStore } from '../../../stores/sft'

const props = defineProps<{ ev: EventDef; hasDecl: boolean }>()
const emit = defineEmits<{ 'tok-hover': [group: string | null] }>()
const store = useSftStore()
const message = useMessage()

const el = ref<HTMLElement | null>(null)
const segs = ref<TokenSegment[]>([])
const renderKey = ref(0) // 整段重挂载键:refresh 自增,强制重建 contenteditable(见文件头)
let goneSig = '' // 已提醒过的「提及被删」签名(同 legacy mentionGoneSig 去重)

// 文本框自适应高度:随内容增长,超过上限后出现滚动条(同 legacy autoGrow)
const MAX_H = 300
function autoGrow() {
  const t = el.value
  if (!t) return
  t.style.height = 'auto'
  const border = t.offsetHeight - t.clientHeight // border-box 下高度需含边框
  const need = t.scrollHeight + border
  const capped = need > MAX_H
  t.style.height = (capped ? MAX_H : need) + 'px'
  t.style.overflowY = capped ? 'auto' : 'hidden'
}

/** 按当前草稿文本重算 token 分段;无声明提及(纯文本卡)整体一段。 */
function computeSegs(): TokenSegment[] {
  const d = store.draft
  const text = String(d?.texts[props.ev.event_id] || '')
  if (!text) return [] // 空内容交由 :empty 占位
  if (!props.hasDecl || !d) return [{ text, group: null }]
  return tokenizeSpans(declaredSpans(d, props.ev, text), text)
}

// 声明式 token 分段渲染:h() 数组子节点,无模板空白注入。
// 注意:不做增量 patch——contenteditable 可能被浏览器人工编辑改结构,
// 重渲染一律靠 renderKey 整段重建(见文件头修复说明)。
const TokenSegs: FunctionalComponent<{ segs: TokenSegment[] }> = (p) =>
  p.segs.map((s, i) =>
    s.group
      ? h('span', { key: i, class: 'sft-tok', 'data-attr': s.group }, s.text)
      : s.text,
  )
TokenSegs.props = ['segs']

function onInput() {
  // 输入只回写草稿纯文本(innerText),不重分词、不动光标(同 legacy)
  if (!store.draft || !el.value) return
  store.draft.texts[props.ev.event_id] = el.value.innerText
  autoGrow()
}

function onPaste(e: ClipboardEvent) {
  e.preventDefault()
  const t = e.clipboardData?.getData('text/plain') || ''
  document.execCommand('insertText', false, t) // 粘贴净化为纯文本(同 legacy)
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter') {
    e.preventDefault()
    document.execCommand('insertText', false, '\n') // Enter 与 textarea 一致插入换行
  }
}

function onBlur() {
  refresh()
  // 人工编辑把声明提及改没时提醒:保存时这些提及会被自动丢弃(见 model.buildRevision)
  const d = store.draft
  const decl = d?.mentions ? d.mentions[props.ev.event_id] : null
  if (!d || !decl) return
  const t = String(d.texts[props.ev.event_id] || '')
  const gone: string[] = []
  Object.keys(decl).forEach((gk) =>
    groupMentionStrings(decl[gk]).forEach((s) => {
      if (s && t.indexOf(s) < 0 && gone.indexOf(s) < 0) gone.push(s)
    }),
  )
  const sig = gone.join('')
  if (gone.length && goneSig !== sig) {
    message.warning(`声明提及「${gone.join('」、「')}」已不在文本中,保存时将自动移除`)
  }
  goneSig = sig
}

/** 外部重渲染(chip 变更 / blur / 外部重建):整段重挂载;pulseGroup 非空时该组 token 短暂脉冲。 */
function refresh(pulseGroup?: string) {
  segs.value = computeSegs()
  renderKey.value += 1 // 弃用旧 DOM(可能被浏览器人工编辑过),整棵重建
  nextTick(() => {
    autoGrow()
    const t = el.value
    if (!t) return
    if (pulseGroup) {
      t.querySelectorAll(`.sft-tok[data-attr="${pulseGroup}"]`).forEach((s) =>
        s.classList.add('sft-tok-pulse'),
      )
    }
    t.classList.remove('sft-fade') // 重新触发淡入(同 legacy)
    void t.offsetWidth
    t.classList.add('sft-fade')
  })
}

// token hover 反向联动:同事件卡内同组 chips 加描边提示(由父组件执行)
function onMouseover(e: MouseEvent) {
  const tok = (e.target as HTMLElement).closest?.('.sft-tok') as HTMLElement | null
  emit('tok-hover', tok ? tok.dataset.attr || null : null)
}

onMounted(() => {
  segs.value = computeSegs()
  nextTick(autoGrow)
})

// 外部变更重算分段:草稿被整体替换(重置/切换视频/保存重建)或声明通道开合
// (勾选/取消检出)。输入期间草稿引用与 hasDecl 均不变,不会触发(spike 光标
// 安全结论不受影响);此前缺失此监听导致重置后文本框显示陈旧内容,继续输入
// 会把旧文本写回草稿(假复活)。
watch([() => store.draft, () => props.hasDecl], () => refresh())

defineExpose({ refresh })
</script>

<template>
  <div
    :key="renderKey"
    ref="el"
    class="sft-ev-text sft-richtext"
    contenteditable="true"
    spellcheck="false"
    :data-placeholder="ev.is_active ? undefined : '未激活事件类别,可人工修改'"
    @input="onInput"
    @blur="onBlur"
    @paste="onPaste"
    @keydown="onKeydown"
    @mouseover="onMouseover"
    @mouseleave="emit('tok-hover', null)"
  >
    <TokenSegs :segs="segs" />
  </div>
</template>
