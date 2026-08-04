// laneMath 单元测试:里程碑封顶 / 单帧推进 / 状态类 / 像素格点亮(对齐 legacy 语义)
import { describe, expect, it } from 'vitest'
import type { ExpertLane } from '../../../../stores/jobs'
import {
  EXPERT_CATCH_RATE, LANE_CELLS, advanceLane, expertLaneCls, laneCells,
  nextMilestone, phaseText,
} from '../laneMath'

function lane(partial: Partial<ExpertLane>): ExpertLane {
  return { name: '违停', status: 'running', detected: null, fraction: 0, label: '', ...partial }
}

describe('nextMilestone', () => {
  const phases = { 违停: [{ fraction: 0.2 }, { fraction: 0.6 }, { fraction: 1 }] }
  it('封顶在 displayed 之后的下一个里程碑', () => {
    expect(nextMilestone(phases, '违停', 0.1, 0.2)).toBeCloseTo(0.2)
    expect(nextMilestone(phases, '违停', 0.25, 0.6)).toBeCloseTo(0.6)
    expect(nextMilestone(phases, '违停', 0.95, 1)).toBe(1)
  })
  it('无阶段定义时封顶 target+0.1 或 1.0', () => {
    expect(nextMilestone(null, '违停', 0.5, 0.5)).toBeCloseTo(0.6)
    expect(nextMilestone({}, '违停', 0.95, 0.95)).toBe(1)
  })
})

describe('advanceLane', () => {
  it('queued 恒归零', () => {
    expect(advanceLane(0.7, lane({ status: 'queued' }), 0.1, null, false)).toBe(0)
  })
  it('reduced-motion 直接到位', () => {
    expect(advanceLane(0.1, lane({ fraction: 0.8 }), 0.016, null, true)).toBe(0.8)
  })
  it('未达 target 按 CATCH 速率逼近且不越过', () => {
    expect(advanceLane(0, lane({ fraction: 0.5 }), 0.1, null, false))
      .toBeCloseTo(EXPERT_CATCH_RATE * 0.1)
    expect(advanceLane(0.49, lane({ fraction: 0.5 }), 0.5, null, false)).toBe(0.5)
  })
  it('到达 target 且仍 running 时向里程碑缓行且不越过', () => {
    const phases = { 违停: [{ fraction: 0.6 }] }
    const d = advanceLane(0.5, lane({ fraction: 0.5 }), 10, phases, false) // 超大 dt 也只到封顶
    expect(d).toBeCloseTo(0.6)
    // 里程碑恰好等于 displayed 时按 fallback 继续缓行(displayed+0.1,与 legacy 同口径)
    expect(advanceLane(0.6, lane({ fraction: 0.5 }), 10, phases, false)).toBeCloseTo(0.7)
  })
  it('done 无 fraction 时目标按 1', () => {
    expect(advanceLane(0.99, lane({ status: 'done', fraction: undefined as never }), 1, null, false)).toBe(1)
  })
})

describe('expertLaneCls / phaseText', () => {
  it('状态类五态', () => {
    expect(expertLaneCls(lane({ status: 'queued' }))).toBe('lane-queued')
    expect(expertLaneCls(lane({ status: 'running' }))).toBe('lane-running')
    expect(expertLaneCls(lane({ status: 'done', detected: true }))).toBe('lane-detected')
    expect(expertLaneCls(lane({ status: 'done', detected: false }))).toBe('lane-clear')
    expect(expertLaneCls(lane({ status: 'error' }))).toBe('lane-error')
  })
  it('queued 显示「等待调度」,其余用 label', () => {
    expect(phaseText(lane({ status: 'queued', label: '抽帧' }))).toBe('等待调度')
    expect(phaseText(lane({ status: 'running', label: '抽帧' }))).toBe('抽帧')
  })
})

describe('laneCells', () => {
  it('整列全亮 + frontier 列按小数点亮', () => {
    // displayed=0.5,18 列 → 9 整列全亮,第 10 列为 frontier
    const cells = laneCells(0.5, LANE_CELLS, 3, true)
    expect(cells).toHaveLength(LANE_CELLS)
    expect(cells[8]).toEqual({ lit: 3, frontier: -1 })
    expect(cells[9]).toEqual({ lit: 0, frontier: 0 })
    expect(cells[10]).toEqual({ lit: 0, frontier: -1 })
  })
  it('非 running 无 frontier;满格无 frontier', () => {
    expect(laneCells(0.5, LANE_CELLS, 3, false)[9].frontier).toBe(-1)
    expect(laneCells(1, LANE_CELLS, 3, true).every((c) => c.lit === 3 && c.frontier === -1)).toBe(true)
  })
})
