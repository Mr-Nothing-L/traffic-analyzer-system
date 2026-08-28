// 分析链路流程图推导层测试(W6):
// - 冻结态区间(最近 user 之后至锚定 detection)与实时态区间(detectionId=null);
// - 阶段映射/顺序、并行批(at 差 <1s 且只隔 approval/system)、子代理内嵌子步骤;
// - 失败步 ok=false + 同名后续成功 → retried;耗时 = 下一条目 at − 本条目 at;
// - 计数(loops/toolCalls/subagents/approvals)与 totalMs 全链有 at 才给出。
import { describe, it, expect } from 'vitest';
import {
  buildAnalysisFlow,
  type FlowParallel,
  type FlowStep,
  type FlowSubagent,
  type FlowText,
  type FlowThink,
} from '../analysisFlow';
import type { AgentEntry, AgentSubItem } from '../../stores/agentchat';

let seq = 0;
function user(text: string, at: number): AgentEntry {
  seq += 1;
  return { id: `u${seq}`, kind: 'user', text, at };
}
function assistant(think: string, at: number, text = ''): AgentEntry {
  seq += 1;
  return { id: `a${seq}`, kind: 'assistant', text, think, at };
}
function tool(
  name: string,
  at: number,
  opts: {
    isError?: boolean;
    done?: boolean;
    args?: string;
    result?: string;
    children?: AgentSubItem[];
  } = {},
): AgentEntry {
  seq += 1;
  return {
    id: `t${seq}`,
    kind: 'tool',
    callId: `call-${seq}`,
    name,
    args: opts.args ?? '{}',
    result: opts.result ?? '',
    images: [],
    hasVideo: false,
    children: opts.children ?? [],
    isError: opts.isError === true,
    done: opts.done !== false,
    at,
  };
}
function approval(toolName: string, at: number): AgentEntry {
  seq += 1;
  return {
    id: `p${seq}`,
    kind: 'approval',
    requestId: `req-${seq}`,
    toolName,
    approvalRule: 'manual',
    accesses: [],
    decision: 'approved',
    at,
  };
}
function detection(at: number): AgentEntry {
  seq += 1;
  return { id: `d${seq}`, kind: 'detection', data: { normal: true }, at };
}

/** 节点种类名:thinking/说明/并发批给自身 kind,其余(步骤/子代理)归 'step'。 */
function kindOf(n: unknown): string {
  const node = n as { kind?: string };
  return node.kind === 'think' || node.kind === 'text' || node.kind === 'parallel'
    ? node.kind
    : 'step';
}

/** 线性全链冻结态:勘察→锁定→取证→提交。 */
function linearEntries(): AgentEntry[] {
  return [
    user('检测这段视频的交通事件', 1000),
    assistant('先看视频元信息', 2000),
    tool('video_meta', 3000),
    tool('extract_frames', 6000),
    tool('draw_boxes', 9000),
    tool('track_suspects', 12000),
    tool('submit_detection', 15000),
    detection(16000),
  ];
}

describe('buildAnalysisFlow(冻结态线性链)', () => {
  const entries = linearEntries();
  const detId = entries.find((e) => e.kind === 'detection')!.id;
  const flow = buildAnalysisFlow(entries, detId);

  it('阶段按 勘察→锁定→取证→提交 输出,节点归位正确', () => {
    expect(flow.phases.map((p) => p.key)).toEqual(['probe', 'locate', 'forensics', 'verdict']);
    const probe = flow.phases[0]!;
    // video_meta/extract_frames 归初步勘察(前面的 thinking 节点除外,另测)
    const probeSteps = probe.nodes.filter((n) => !('kind' in n)) as FlowStep[];
    expect(probeSteps.map((n) => n.label)).toEqual([
      '视频元信息(video_meta)',
      '抽帧(extract_frames)',
    ]);
    expect(flow.phases[1]!.nodes).toHaveLength(1); // draw_boxes
    expect(flow.phases[2]!.nodes).toHaveLength(1); // track_suspects
    expect(flow.phases[3]!.nodes).toHaveLength(1); // submit_detection
  });

  it('每步耗时=下一条目 at − 本条目 at;末步到检测卡收口', () => {
    const probeSteps = flow.phases[0]!.nodes.filter((n) => !('kind' in n)) as Array<{
      durationMs: number | null;
    }>;
    expect(probeSteps[0]!.durationMs).toBe(3000); // 6000-3000
    expect(probeSteps[1]!.durationMs).toBe(3000);
    // submit_detection(15000)→ detection(16000)
    const verdictNode = flow.phases[3]!.nodes[0] as { durationMs: number | null };
    expect(verdictNode.durationMs).toBe(1000);
  });

  it('计数与总耗时/fromUserText/done', () => {
    expect(flow.loops).toBe(1);
    expect(flow.toolCalls).toBe(5);
    expect(flow.subagents).toBe(0);
    expect(flow.approvals).toBe(0);
    expect(flow.totalMs).toBe(14000); // 2000(assistant 起)→ 16000
    expect(flow.fromUserText).toBe('检测这段视频的交通事件');
    expect(flow.done).toBe(true);
  });

  it('thinking 按位置插为节点:首段思考进首个相位、排在首个工具之前', () => {
    // linearEntries 的 assistant 在 video_meta 之前 → probe 相位首个节点
    expect(kindOf(flow.phases[0]!.nodes[0])).toBe('think');
    const think = flow.phases[0]!.nodes[0] as FlowThink;
    expect(think.id).toBe(
      entries.find((e) => e.kind === 'assistant')!.id,
    );
    expect(think.text).toBe('先看视频元信息');
  });
});

describe('buildAnalysisFlow(thinking 节点归属)', () => {
  it('首段多段思考按时间序排列,都进首个相位', () => {
    const e2 = [
      user('q', 1000),
      assistant('第一步先确认编码\n\n再决定抽帧密度', 2000),
      assistant('补充:帧率不足\n需要重抽', 2500),
      tool('video_meta', 3000),
      detection(4000),
    ];
    const f = buildAnalysisFlow(e2, e2.find((e) => e.kind === 'detection')!.id);
    expect(f.phases[0]!.nodes.map(kindOf)).toEqual(['think', 'think', 'step']);
    const [t1, t2] = f.phases[0]!.nodes as Array<FlowThink | FlowStep>;
    expect((t1 as FlowThink).text).toBe('第一步先确认编码\n\n再决定抽帧密度');
    expect((t2 as FlowThink).text).toBe('补充:帧率不足\n需要重抽');
  });

  it('工具间的思考归其后最近工具的相位(阶段边界思考是下一阶段的开场白)', () => {
    const e3 = [
      user('q', 1000),
      tool('video_meta', 2000), // probe
      tool('extract_frames', 3000), // probe
      assistant('框已定,开始深挖', 3500), // 位于 extract_frames 与 track_suspects 之间
      tool('track_suspects', 4000), // forensics
      detection(5000),
    ];
    const f = buildAnalysisFlow(e3, e3.find((e) => e.kind === 'detection')!.id);
    expect(f.phases.map((p) => p.key)).toEqual(['probe', 'forensics']);
    // 思考进 forensics(其后工具的相位),排在该相位节点序列最前
    expect(f.phases[1]!.nodes.map(kindOf)).toEqual(['think', 'step']);
    expect((f.phases[1]!.nodes[0] as FlowThink).text).toBe('框已定,开始深挖');
  });

  it('末段思考(其后无工具)归前一工具相位殿后', () => {
    const e4 = [
      user('q', 1000),
      tool('draw_boxes', 2000),
      assistant('提交前最后核对一遍编码', 2500),
      detection(3000),
    ];
    const f = buildAnalysisFlow(e4, e4.find((e) => e.kind === 'detection')!.id);
    expect(f.phases.map((p) => p.key)).toEqual(['locate']);
    expect(f.phases[0]!.nodes.map(kindOf)).toEqual(['step', 'think']);
    expect((f.phases[0]!.nodes[1] as FlowThink).text).toBe('提交前最后核对一遍编码');
  });

  it('实时态末尾 assistant 的思考标 live(仍在流入);随工具推进复位', () => {
    // 末段思考在其后无工具 → 归前一工具相位殿后,是该相位最后一个节点
    const live = buildAnalysisFlow(
      [
        user('q', 1000),
        tool('video_meta', 2000),
        assistant('思考流入中…', 2500),
      ],
      null,
    );
    const liveNodes = live.phases[0]!.nodes;
    expect(liveNodes.map(kindOf)).toEqual(['step', 'think']);
    expect((liveNodes[1] as FlowThink).live).toBe(true);
    const settled = buildAnalysisFlow(
      [
        user('q', 1000),
        tool('video_meta', 2000),
        assistant('已定格', 2500),
        tool('extract_frames', 3000),
      ],
      null,
    );
    expect((settled.phases[0]!.nodes[0] as FlowThink).live).toBeUndefined();
  });

  it('纯问答轮次(区间无工具)不产任何节点,thinking 不出面板', () => {
    const f = buildAnalysisFlow([user('q', 1000), assistant('直接回答', 1500)], null);
    expect(f.phases).toHaveLength(0);
    expect(f.loops).toBe(1);
  });
});

describe('buildAnalysisFlow(说明节点)', () => {
  const detId = (entries: AgentEntry[]) =>
    entries.find((e) => e.kind === 'detection')!.id;

  it('正文按位置插为 text 节点:归属其后最近工具的相位,同一条目思考在前正文在后', () => {
    const es = [
      user('q', 1000),
      assistant('先勘察', 1500, '我先看一下视频基本信息。'),
      tool('video_meta', 2000),
      detection(3000),
    ];
    const f = buildAnalysisFlow(es, detId(es));
    const nodes = f.phases[0]!.nodes;
    expect(nodes.map(kindOf)).toEqual(['think', 'text', 'step']);
    const text = nodes[1] as FlowText;
    expect(text.id).toBe(es[1]!.id);
    expect(text.text).toBe('我先看一下视频基本信息。');
  });

  it('工具间的说明归其后最近工具的相位;末段说明(其后无工具)归前一工具相位殿后', () => {
    const es = [
      user('q', 1000),
      tool('video_meta', 2000),
      assistant('', 2500, '元信息已确认,继续抽帧分析。'), // 其后最近工具 draw_boxes → locate 开场
      tool('draw_boxes', 3000),
      assistant('', 3500, '框已画好,准备提交。'), // 其后无工具 → 前一工具相位殿后
      detection(4000),
    ];
    const f = buildAnalysisFlow(es, detId(es));
    expect(f.phases.map((p) => p.key)).toEqual(['probe', 'locate']);
    expect(f.phases[0]!.nodes.map(kindOf)).toEqual(['step']);
    expect(f.phases[1]!.nodes.map(kindOf)).toEqual(['text', 'step', 'text']);
    expect((f.phases[1]!.nodes[0] as FlowText).text).toBe('元信息已确认,继续抽帧分析。');
    expect((f.phases[1]!.nodes[2] as FlowText).text).toBe('框已画好,准备提交。');
  });

  it('思考/说明/工具严格按原始顺序交错(想→说→做)', () => {
    const es = [
      user('q', 1000),
      assistant('想:先勘察', 1200, '说:开始勘察'),
      tool('video_meta', 2000),
      assistant('想:定抽帧密度', 2500, '说:按 2fps 抽帧'),
      tool('extract_frames', 3000),
      detection(4000),
    ];
    const f = buildAnalysisFlow(es, detId(es));
    // 两段思考/说明的后继工具都在 probe 相位:同相位内按原始顺序交错
    expect(f.phases).toHaveLength(1);
    expect(f.phases[0]!.nodes.map(kindOf)).toEqual([
      'think',
      'text',
      'step',
      'think',
      'text',
      'step',
    ]);
  });

  it('detection 之后收尾文本不进面板(冻结态在区间外,实时态显式排除)', () => {
    const es = [
      user('q', 1000),
      tool('submit_detection', 2000),
      detection(2500),
      assistant('', 3000, '检测完成,报告见上卡。'),
    ];
    const textNodes = (f: ReturnType<typeof buildAnalysisFlow>) =>
      f.phases.flatMap((p) => p.nodes).filter((n) => (n as FlowText).kind === 'text');
    expect(textNodes(buildAnalysisFlow(es, detId(es)))).toHaveLength(0);
    expect(textNodes(buildAnalysisFlow(es, null))).toHaveLength(0);
  });

  it('实时态末尾说明随流式标 live;纯文本问答轮次仍不产节点', () => {
    const es = [
      user('q', 1000),
      tool('video_meta', 2000),
      assistant('', 2500, '结果说明流入中…'),
    ];
    const f = buildAnalysisFlow(es, null);
    expect(kindOf(f.phases[0]!.nodes[1])).toBe('text');
    expect((f.phases[0]!.nodes[1] as FlowText).live).toBe(true);
    const qa = buildAnalysisFlow([user('q', 1000), assistant('', 1500, '直接回答')], null);
    expect(qa.phases).toHaveLength(0);
  });
});

describe('buildAnalysisFlow(并行批)', () => {
  it('at 差 <1s 的相邻工具合并为 parallel 节点(transitive 三连)', () => {
    const entries = [
      user('q', 1000),
      tool('extract_frames', 2000),
      tool('draw_boxes', 2300),
      tool('read_file', 2600), // 未命中阶段表 → 其他;仍与上一批 <1s?2600-2300=300 <1s → 会并入!
      detection(5000),
    ];
    const flow = buildAnalysisFlow(entries, entries.at(-1)!.id);
    // read_file 归「其他」相位,不会并进 probe 相位的批(批在相位分组前构建,
    // 但批次按工具链合并——read_file 与 draw_boxes 只隔 nothing 且差 300ms → 并入同一批)
    const allPar = flow.phases.flatMap((p) => p.nodes).filter((n) => 'kind' in n);
    expect(allPar.length).toBe(1);
    expect((allPar[0] as FlowParallel).steps).toHaveLength(3);
  });

  it('at 差 ≥1s 不合并,保持独立步骤', () => {
    const entries = [
      user('q', 1000),
      tool('extract_frames', 2000),
      tool('draw_boxes', 3500), // 差 1500ms
      detection(5000),
    ];
    const flow = buildAnalysisFlow(entries, entries.at(-1)!.id);
    const nodes = flow.phases.flatMap((p) => p.nodes);
    expect(nodes).toHaveLength(2);
    expect(nodes.every((n) => !('kind' in n))).toBe(true);
  });

  it('手动模式审批夹在两工具之间不切断并发批(approval 不产节点)', () => {
    const entries = [
      user('q', 1000),
      tool('extract_frames', 2000),
      approval('draw_boxes', 2100),
      tool('draw_boxes', 2300),
      detection(5000),
    ];
    const flow = buildAnalysisFlow(entries, entries.at(-1)!.id);
    expect(flow.approvals).toBe(1);
    const par = flow.phases.flatMap((p) => p.nodes).find((n) => 'kind' in n) as FlowParallel;
    expect(par.steps).toHaveLength(2);
  });
});

describe('buildAnalysisFlow(子代理)', () => {
  function subagentEntries(done: boolean): AgentEntry[] {
    return [
      user('q', 1000),
      assistant('派子代理核对', 1500),
      tool('spawn_subagent', 2000, {
        args: JSON.stringify({ task: '核对画面细节' }),
        done,
        result: done ? '结论:检出违停。\n详细依据见报告' : '',
        children: [
          { kind: 'think', text: '子代理思考中' },
          { kind: 'tool', id: 'c1', name: 'read_file', args: '{}', done: true },
          { kind: 'tool', id: 'c2', name: 'grep_files', args: '{}', done: false },
        ],
      }),
      detection(4000),
    ];
  }

  it('task 取自 args.task,结论取结果首行,inner 子步骤带状态', () => {
    const entries = subagentEntries(true);
    const flow = buildAnalysisFlow(entries, entries.find((e) => e.kind === 'detection')!.id);
    expect(flow.subagents).toBe(1);
    const node = flow.phases
      .find((p) => p.key === 'forensics')!
      .nodes.find((n) => 'inner' in n) as FlowSubagent;
    expect(node.task).toBe('核对画面细节');
    expect(node.conclusion).toBe('结论:检出违停。');
    expect(node.ok).toBe(true);
    expect(node.durationMs).toBe(2000); // 4000-2000(检测卡收口)
    expect(node.inner).toHaveLength(2); // think 不是步骤
    expect(node.inner[0]).toMatchObject({ ok: true });
    expect(node.inner[0]!.active).toBeUndefined(); // 已完成子工具无进行中标记
    expect(node.inner[1]).toMatchObject({ ok: false, active: true }); // 未完成子工具=进行中
  });

  it('args 非 JSON 时回退原文截断', () => {
    const entries = [
      user('q', 1000),
      tool('spawn_subagent', 2000, { args: '纯文本任务描述', done: true, result: '完' }),
      detection(3000),
    ];
    const flow = buildAnalysisFlow(entries, entries.find((e) => e.kind === 'detection')!.id);
    const node = flow.phases[0]!.nodes[0] as FlowSubagent;
    expect(node.task).toBe('纯文本任务描述');
  });
});

describe('buildAnalysisFlow(失败步与重试标记)', () => {
  it('isError 步 ok=false;其后同名工具成功 → retried 标记', () => {
    const entries = [
      user('q', 1000),
      tool('extract_frames', 2000, { isError: true, result: '抽帧失败:文件损坏' }),
      tool('extract_frames', 5000),
      detection(6000),
    ];
    const flow = buildAnalysisFlow(entries, entries.find((e) => e.kind === 'detection')!.id);
    const [fail, retried] = flow.phases[0]!.nodes as Array<{ ok: boolean; retried?: boolean }>;
    expect(fail.ok).toBe(false);
    expect(fail.retried).toBe(true);
    expect(retried.ok).toBe(true);
    expect(retried.retried).toBeUndefined();
  });

  it('后续同名调用也失败则不标 retried(没有成功的重试)', () => {
    const entries = [
      user('q', 1000),
      tool('extract_frames', 2000, { isError: true, result: '失败 A' }),
      tool('extract_frames', 5000, { isError: true, result: '失败 B' }),
      detection(6000),
    ];
    const flow = buildAnalysisFlow(entries, entries.find((e) => e.kind === 'detection')!.id);
    const [first, second] = flow.phases[0]!.nodes as Array<{ ok: boolean; retried?: boolean }>;
    expect(first.retried).toBeUndefined();
    expect(second.ok).toBe(false);
  });
});

describe('buildAnalysisFlow(区间边界)', () => {
  it('无 user 条目时从数组开头起算', () => {
    const entries = [tool('video_meta', 1000), detection(2000)];
    const flow = buildAnalysisFlow(entries, entries.at(-1)!.id);
    expect(flow.fromUserText).toBe('');
    expect(flow.toolCalls).toBe(1);
    expect(flow.totalMs).toBe(1000);
  });

  it('连续追问取最近一条 user 为边界,此前轮次不计入', () => {
    const entries = [
      user('第一问', 1000),
      tool('video_meta', 2000),
      assistant('第一轮回答', 3000),
      user('再看一遍尾部', 8000),
      tool('draw_boxes', 9000),
      detection(10000),
    ];
    const flow = buildAnalysisFlow(entries, entries.at(-1)!.id);
    expect(flow.fromUserText).toBe('再看一遍尾部');
    expect(flow.toolCalls).toBe(1); // 只有 draw_boxes
    expect(flow.loops).toBe(0);
    expect(flow.totalMs).toBe(1000); // 9000 → 10000
    expect(flow.done).toBe(true);
  });

  it('detectionId 未命中退化为实时口径(done=false)', () => {
    const flow = buildAnalysisFlow(linearEntries(), 'no-such-id');
    expect(flow.done).toBe(false);
  });
});

describe('buildAnalysisFlow(实时态,detectionId=null)', () => {
  it('区间为最后一条 user 之后至末尾;进行中步 ok=false+active=true', () => {
    const entries = [
      user('第一问', 1000),
      tool('video_meta', 2000), // 上一轮,不计
      user('接着分析', 8000),
      assistant('开始加载', 8500),
      tool('load_video', 9000, { done: false }), // 执行中
    ];
    const flow = buildAnalysisFlow(entries, null);
    expect(flow.done).toBe(false);
    expect(flow.fromUserText).toBe('接着分析');
    expect(flow.toolCalls).toBe(1);
    const step = flow.phases[0]!.nodes.find((n) => !('kind' in n)) as {
      ok: boolean;
      active?: boolean;
      durationMs: null;
    };
    expect(step.ok).toBe(false);
    expect(step.active).toBe(true);
    expect(step.durationMs).toBeNull(); // 末条目无「下一条目」
  });

  it('最后一个节点恒标 active(当前步),即使该步已完成', () => {
    const entries = [user('q', 1000), tool('video_meta', 2000)];
    const flow = buildAnalysisFlow(entries, null);
    const last = flow.phases[0]!.nodes[0] as { ok: boolean; active?: boolean };
    expect(last.ok).toBe(true);
    expect(last.active).toBe(true);
  });

  it('还没有任何工具节点时 phases 为空(渲染层自行隐藏)', () => {
    const entries = [user('q', 1000), assistant('思考中…', 1100)];
    const flow = buildAnalysisFlow(entries, null);
    expect(flow.phases).toHaveLength(0);
    expect(flow.loops).toBe(1);
  });

  it('并行批的末步承接当前步标记', () => {
    const entries = [
      user('q', 1000),
      tool('extract_frames', 2000),
      tool('draw_boxes', 2300, { done: false }),
    ];
    const flow = buildAnalysisFlow(entries, null);
    const par = flow.phases[0]!.nodes[0] as FlowParallel;
    expect(par.steps[0]!.active).toBeUndefined();
    expect(par.steps[1]!.active).toBe(true);
  });
});

describe('buildAnalysisFlow(诚实降级)', () => {
  it('区间内任一条目缺 at 则 totalMs=null、缺失步骤时长=null', () => {
    const old = { ...linearEntries()[2]! }; // video_meta(历史老数据无 at)
    delete (old as { at?: number }).at;
    const entries = [user('q', 1000), old, ...linearEntries().slice(3)];
    const flow = buildAnalysisFlow(entries, entries.find((e) => e.kind === 'detection')!.id);
    expect(flow.totalMs).toBeNull();
    const probe = flow.phases[0]!.nodes as Array<{ durationMs: number | null }>;
    expect(probe[0]!.durationMs).toBeNull(); // 自身缺 at
  });
});
