/** 分析链路流程图推导层(W6):从会话时间线条目(entries)推导出纵向阶段树
 * ——主干 + 并行批 + 一层子代理树。纯函数,vitest 直测;组件(ChatAnalysisFlow)
 * 只渲染,不懂数据。
 *
 * 两种口径由 detectionId 参数区分:
 * - 冻结态:detectionId = 检测条目 id。区间 = 该 detection 之前最近一条 user
 *   消息之后(不含该 user)至该 detection 自身;done=true。
 * - 实时态:detectionId = null。区间 = 最后一条 user 之后(不含)至 entries
 *   末尾,随流式 entries 生长;done=false,最后一个节点标记 active(当前步)。
 *
 * 推导口径(诚实边界):
 * - 每步耗时 = 下一条目 at − 本条目 at(工具只有开始时刻,结果时刻未知,
 *   用「到下一条目发生为止」近似);任一侧缺 at(旧数据/未完成)为 null。
 * - totalMs 仅当区间内每条条目都带 at(链路完整)才给出,否则 null
 *   ——摘要行相应隐藏秒段,不编造数字。
 * - 并行批:相邻两条工具条目之间只隔 approval/system 条目(手动模式审批先入
 *   时间线),且 at 差 <1s 才视为同批并发;逐对链式并入(three 同批全并)。
 * - 步骤状态三元组约定:{ok:true}=成功;{ok:false, active:true}=进行中;
 *   {ok:false, active:false}=失败。渲染层据此着色,不再自行推断。
 * - 「已重试」标记:失败步之后同区间内出现同名工具且最终成功的调用,retried=true。
 */
import type { AgentEntry, AgentSubItem, AgentToolEntry } from '../stores/agentchat'
import { thinkSummaryLine, toolLabel } from './chatDisplay'

/** 主干步骤/并行批内步骤/子代理内嵌子步骤共用同一形状。 */
export interface FlowStep {
  /** 工具原名(draw_boxes 等)。 */
  name: string
  /** 中文标签(toolLabel 格式「中文名(原名)」)。 */
  label: string
  ok: boolean
  durationMs: number | null
  /** 进行中(与 ok:false 组合成「进行中」态,见模块头约定)。 */
  active?: boolean
  /** 失败步专属:同名工具已在后续重试成功。 */
  retried?: boolean
}

/** 同批并发工具(at 差 <1s 且中间只隔 approval/system 条目)。 */
export interface FlowParallel {
  kind: 'parallel'
  steps: FlowStep[]
}

/** spawn_subagent 内嵌子步骤树(一层,分支节点,默认折叠)。 */
export interface FlowSubagent {
  /** 任务描述(args.task,截断展示)。 */
  task: string
  /** 类型上保留 null(无数据占位);运行时恒为 boolean,状态同 FlowStep 约定。 */
  ok: boolean | null
  durationMs: number | null
  /** 子代理迷你时间线里的工具调用(children.kind==='tool');无时间戳,恒 null。 */
  inner: FlowStep[]
  /** 子结论文本(工具结果首个非空行,截 80 字;失败时不给)。 */
  conclusion?: string
  /** 实时态「最新推进位置」高亮(渲染层给头部挂脉冲)。 */
  active?: boolean
}

export type FlowNode = FlowStep | FlowParallel | FlowSubagent

export type FlowPhaseKey = 'probe' | 'locate' | 'forensics' | 'verdict' | 'other'

export interface FlowPhase {
  key: FlowPhaseKey
  title: string
  /** UiIcon 图标名。 */
  icon: string
  nodes: FlowNode[]
  /** 该阶段首个非空 thinking 一句话摘要(thinkSummaryLine 结束态取首行)。 */
  thinkNote?: string
}

export interface AnalysisFlow {
  phases: FlowPhase[]
  /** 区间内 assistant 条目数(轮循环节拍)。 */
  loops: number
  /** 区间内顶层工具调用数(spawn_subagent 计一次,不计子工具)。 */
  toolCalls: number
  subagents: number
  approvals: number
  /** 总耗时;链路上有任何条目缺 at 则 null(诚实降级,渲染层隐藏秒段)。 */
  totalMs: number | null
  fromUserText: string
  /** 冻结态 true;实时态 false。 */
  done: boolean
}

/* ---- 阶段映射 ---- */

interface PhaseMeta {
  key: FlowPhaseKey
  title: string
  icon: string
}

const PHASE_DEFS: Array<PhaseMeta & { tools: readonly string[] }> = [
  { key: 'probe', title: '初步勘察', icon: 'search', tools: ['video_meta', 'extract_frames', 'load_video'] },
  { key: 'locate', title: '目标锁定', icon: 'target', tools: ['draw_boxes'] },
  { key: 'forensics', title: '深度取证', icon: 'branch', tools: ['track_suspects', 'spawn_subagent'] },
  { key: 'verdict', title: '裁决提交', icon: 'send', tools: ['submit_detection'] },
]

/** 未命中任何阶段表的工具(read_file 等)归「其他」,殿后输出。 */
const OTHER_META: PhaseMeta = { key: 'other', title: '其他', icon: 'dash' }

function phaseOf(toolName: string): PhaseMeta {
  return PHASE_DEFS.find((p) => p.tools.includes(toolName)) ?? OTHER_META
}

/** 阶段输出顺序(表顺序 + 其他殿后)。 */
const PHASE_ORDER: readonly FlowPhaseKey[] = [...PHASE_DEFS.map((p) => p.key), OTHER_META.key]

/** 并行批判定阈值:相邻工具 at 差小于该值视为同批并发。 */
const PARALLEL_GAP_MS = 1000

/** from、to 两下标之间的条目是否都「不产节点」(approval/system):
 * 手动模式下审批先于其工具入时间线,不应切断并发批;assistant/user/detection
 * 夹在中间说明模型已推进到别处,不算相邻。 */
function adjacentThroughQuietEntries(
  entries: readonly AgentEntry[],
  from: number,
  to: number,
): boolean {
  for (let i = from + 1; i < to; i++) {
    const k = entries[i]!.kind
    if (k !== 'approval' && k !== 'system') return false
  }
  return true
}

function entryAt(e: AgentEntry | undefined): number | null {
  return typeof e?.at === 'number' ? e.at : null
}

/** 结论/任务的首行截断(无内容返回 undefined)。 */
function firstLine(text: string, max: number): string | undefined {
  const line = (text.split('\n').find((l) => l.trim()) ?? '').trim()
  if (!line) return undefined
  return line.length > max ? `${line.slice(0, max)}…` : line
}

/** spawn_subagent 的任务描述:args JSON 的 task 字段(agent 端 schema required),
 * 解析失败回退 args 原文截断。 */
function subagentTask(args: string): string {
  try {
    const obj = JSON.parse(args) as Record<string, unknown>
    if (typeof obj.task === 'string' && obj.task.trim()) return obj.task.trim()
  } catch {
    // 非 JSON args:回退原文截断
  }
  return firstLine(args, 48) ?? '(无参数)'
}

/** 子代理内嵌工具步骤(children.kind==='tool'):无时间戳,ok=完成与否;
 * 未完成的子工具标 active(进行中脉冲),失败无从判定(children 不含 isError)。 */
function subInnerSteps(children: AgentSubItem[]): FlowStep[] {
  return children
    .filter((c): c is Extract<AgentSubItem, { kind: 'tool' }> => c.kind === 'tool')
    .map((c) => ({
      name: c.name,
      label: toolLabel(c.name),
      ok: c.done,
      durationMs: null,
      ...(!c.done ? { active: true } : {}),
    }))
}

/**
 * 构建分析链路流程图。
 * @param entries 会话时间线条目(store.agentchat.entries,到达序)
 * @param detectionId 冻结态锚定的检测条目 id;null = 实时态(最后一条 user 之后至末尾)
 */
export function buildAnalysisFlow(
  entries: readonly AgentEntry[],
  detectionId: string | null,
): AnalysisFlow {
  /* ---- 区间解析:start..end 为左闭右开的原始下标 ---- */
  // 终点:冻结态为命中的 detection 条目(含);实时态为 entries 末尾。
  // detectionId 给了但没命中(撤回后残留等异常)退化成实时口径,不抛错。
  let end = entries.length
  let anchored = false // 冻结态锚点是否命中
  if (detectionId !== null) {
    const idx = entries.findIndex((e) => e.kind === 'detection' && e.id === detectionId)
    if (idx >= 0) {
      end = idx + 1
      anchored = true
    }
  }
  // 起点:往回找最近一条 user(steer 插话也是新的边界)。
  let userIdx = -1
  for (let i = end - 1; i >= 0; i--) {
    if (entries[i]!.kind === 'user') {
      userIdx = i
      break
    }
  }
  const start = userIdx >= 0 ? userIdx + 1 : 0
  const slice = entries.slice(start, end)

  /* ---- 计数与总耗时 ---- */
  let loops = 0
  let approvals = 0
  let subagents = 0
  for (const e of slice) {
    if (e.kind === 'assistant') loops++
    else if (e.kind === 'approval') approvals++
    else if (e.kind === 'tool') {
      if ((e as AgentToolEntry).name === 'spawn_subagent') subagents++
    }
  }
  const toolCalls = slice.filter((e) => e.kind === 'tool').length
  // 总耗时只在全链有 at 时给出(≥2 条、每条带 at、终点晚于起点)。
  const chainComplete =
    slice.length >= 2 && slice.every((e) => typeof e.at === 'number')
  const totalMs = chainComplete
    ? (slice[slice.length - 1]!.at as number) - (slice[0]!.at as number)
    : null

  /* ---- 工具条目收集(顶层层级,原始下标保留供相邻/时长推导) ---- */
  const toolSlots: Array<{ entry: AgentToolEntry; idx: number }> = []
  for (let i = start; i < end; i++) {
    const e = entries[i]!
    if (e.kind === 'tool') toolSlots.push({ entry: e as AgentToolEntry, idx: i })
  }

  // 时长:下一条目 at − 本条目 at(工具开始 → 下一动作发生);末步或缺 at 为 null。
  const durOf = (idx: number): number | null => {
    const self = entryAt(entries[idx])
    if (self === null) return null
    for (let j = idx + 1; j < end; j++) {
      const next = entryAt(entries[j])
      if (next !== null) return Math.max(0, next - self)
    }
    return null
  }

  /* ---- 节点构建(线性扫描 + 并行批合并) ---- */
  interface NodeWrap {
    node: FlowNode
    /** 来源工具条目的原始下标(相位归属用)。 */
    srcIdx: number
  }
  const wraps: NodeWrap[] = []
  /** 每个步骤对象按来源下标登记(失败重试标记直接改写引用)。 */
  const stepBySrc = new Map<number, FlowStep>()
  /** 上一个产节点的工具槽(null = 尚无可并入对象)。 */
  let prevSlot: { entry: AgentToolEntry; idx: number } | null = null

  for (const slot of toolSlots) {
    const te = slot.entry
    if (te.name === 'spawn_subagent') {
      wraps.push({
        node: {
          task: subagentTask(te.args),
          ok: te.done ? !te.isError : false,
          durationMs: durOf(slot.idx),
          inner: subInnerSteps(te.children),
          ...(te.isError || !te.result.trim() ? {} : { conclusion: firstLine(te.result, 80) }),
          ...(!te.done ? { active: true } : {}),
        },
        srcIdx: slot.idx,
      })
      prevSlot = slot
      continue
    }
    const step: FlowStep = {
      name: te.name,
      label: toolLabel(te.name),
      ok: te.done ? !te.isError : false,
      durationMs: durOf(slot.idx),
      ...(!te.done ? { active: true } : {}),
    }
    stepBySrc.set(slot.idx, step)
    // 并行并入:上一工具与本工具之间只隔 approval/system,且 at 差 <1s;
    // 上一个节点须是纯步骤(单步升批 / 批尾续接),子代理分支节点不并入。
    let merged = false
    const lastWrap = wraps[wraps.length - 1]
    if (prevSlot !== null && lastWrap !== undefined && !('inner' in lastWrap.node)) {
      const prevAt = entryAt(prevSlot.entry)
      const curAt = entryAt(te)
      if (
        prevAt !== null &&
        curAt !== null &&
        curAt - prevAt < PARALLEL_GAP_MS &&
        adjacentThroughQuietEntries(entries, prevSlot.idx, slot.idx)
      ) {
        if ((lastWrap.node as FlowParallel).kind === 'parallel') {
          ;(lastWrap.node as FlowParallel).steps.push(step)
        } else {
          wraps[wraps.length - 1] = {
            node: { kind: 'parallel', steps: [lastWrap.node as FlowStep, step] },
            srcIdx: lastWrap.srcIdx,
          }
        }
        merged = true
      }
    }
    if (!merged) wraps.push({ node: step, srcIdx: slot.idx })
    prevSlot = slot
  }

  /* ---- 失败步的「已重试」标记:其后存在同名工具且最终成功 ---- */
  for (let a = 0; a < toolSlots.length; a++) {
    const ta = toolSlots[a]!
    if (!(ta.entry.done && ta.entry.isError)) continue
    for (let b = a + 1; b < toolSlots.length; b++) {
      const tb = toolSlots[b]!
      if (tb.entry.name === ta.entry.name && tb.entry.done && !tb.entry.isError) {
        const step = stepBySrc.get(ta.idx)
        if (step) step.retried = true
        break
      }
    }
  }

  /* ---- 实时态:最后一个节点标记「当前步」 ---- */
  if (detectionId === null && wraps.length > 0) {
    const last = wraps[wraps.length - 1]!.node
    if ('inner' in last) last.active = true
    else if ('kind' in last) {
      const steps = (last as FlowParallel).steps
      const tail = steps[steps.length - 1]
      if (tail) tail.active = true
    } else last.active = true
  }

  /* ---- 阶段分组 ---- */
  const nodesByKey = new Map<FlowPhaseKey, FlowNode[]>()
  for (const w of wraps) {
    const e = entries[w.srcIdx]!
    const meta = phaseOf(e.kind === 'tool' ? (e as AgentToolEntry).name : '')
    const bucket = nodesByKey.get(meta.key) ?? []
    bucket.push(w.node)
    nodesByKey.set(meta.key, bucket)
  }

  /* ---- thinking 摘要:assistant 归属其后第一条工具的相位,每相位取首个非空摘要 ---- */
  const notesByKey = new Map<FlowPhaseKey, string>()
  for (let i = start; i < end; i++) {
    const e = entries[i]!
    if (e.kind !== 'assistant' || !e.think.trim()) continue
    let target: PhaseMeta | null = null
    for (let j = i + 1; j < end; j++) {
      if (entries[j]!.kind === 'tool') {
        target = phaseOf((entries[j] as AgentToolEntry).name)
        break
      }
    }
    if (!target || notesByKey.has(target.key)) continue
    const note = thinkSummaryLine(e.think, false)
    if (note) notesByKey.set(target.key, note)
  }

  const phases: FlowPhase[] = []
  for (const key of PHASE_ORDER) {
    const pnodes = nodesByKey.get(key)
    if (!pnodes?.length) continue
    const meta = PHASE_DEFS.find((p) => p.key === key) ?? OTHER_META
    const note = notesByKey.get(key)
    phases.push({
      key,
      title: meta.title,
      icon: meta.icon,
      nodes: pnodes,
      ...(note ? { thinkNote: note } : {}),
    })
  }

  return {
    phases,
    loops,
    toolCalls,
    subagents,
    approvals,
    totalMs,
    fromUserText:
      userIdx >= 0 && entries[userIdx]!.kind === 'user'
        ? (entries[userIdx] as Extract<AgentEntry, { kind: 'user' }>).text
        : '',
    done: anchored,
  }
}
