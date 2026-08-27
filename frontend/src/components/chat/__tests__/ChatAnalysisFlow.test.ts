/** ChatAnalysisFlow 渲染测试(W6):冻结态折叠摘要/展开阶段树、并行批 chips、
 * 子代理分支、失败步与实时态当前步脉冲。经 @vue/server-renderer SSR 直渲染
 * (纯 node 环境,组件数据由 buildAnalysisFlow 从真实条目形状推导)。 */
import { describe, it, expect } from 'vitest';
import { createSSRApp, h } from 'vue';
import { renderToString } from 'vue/server-renderer';

import ChatAnalysisFlow from '../ChatAnalysisFlow.vue';
import ChatEntryDetection from '../ChatEntryDetection.vue';
import { buildAnalysisFlow } from '../../../utils/analysisFlow';
import type { AnalysisFlow, FlowStep } from '../../../utils/analysisFlow';
import type { AgentEntry } from '../../../stores/agentchat';

let seq = 0;
function user(text: string, at: number): AgentEntry {
  seq += 1;
  return { id: `u${seq}`, kind: 'user', text, at };
}
function assistant(think: string, at: number): AgentEntry {
  seq += 1;
  return { id: `a${seq}`, kind: 'assistant', text: '', think, at };
}
function tool(
  name: string,
  at: number,
  opts: {
    isError?: boolean;
    done?: boolean;
    args?: string;
    result?: string;
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
    children: [],
    isError: opts.isError === true,
    done: opts.done !== false,
    at,
  };
}
function detection(at: number): AgentEntry {
  seq += 1;
  return { id: `d${seq}`, kind: 'detection', data: { normal: true }, at };
}

const DET_ID = (entries: AgentEntry[]) =>
  entries.find((e) => e.kind === 'detection')!.id;

async function renderHtml(flow: AnalysisFlow, extra: Record<string, unknown> = {}): Promise<string> {
  const app = createSSRApp({ render: () => h(ChatAnalysisFlow, { flow, ...extra }) });
  return await renderToString(app);
}

/** 线性链冻结态(总耗时 95s,单子代理无审批):摘要文案可整串断言。 */
function linearEntries(): AgentEntry[] {
  return [
    user('检测这段视频', 100_000),
    assistant('先看元信息再抽帧', 101_000),
    tool('video_meta', 102_000),
    tool('track_suspects', 138_000),
    tool('submit_detection', 190_000),
    detection(196_000),
  ];
}

describe('ChatAnalysisFlow(冻结态折叠)', () => {
  it('默认渲染一行摘要(轮次/调用/秒),不展开阶段树', async () => {
    const entries = linearEntries();
    const flow = buildAnalysisFlow(entries, DET_ID(entries));
    expect(flow.totalMs).toBe(95_000);
    const html = await renderHtml(flow);
    expect(html).toContain('aflow-summary');
    expect(html).toContain('1 轮循环 · 3 次调用 · 95 秒');
    expect(html).not.toContain('aflow-phase-title');
    expect(html).not.toContain('初步勘察');
  });

  it('zero/null 数据段静默省略:无子代理/审批/耗时则不出现对应文案', async () => {
    const entries = [
      user('q', 1000),
      assistant('先看元信息', 1500),
      tool('video_meta', 2000),
      detection(3000),
    ];
    delete (entries[1] as { at?: number }).at; // 缺耗时段
    const flow = buildAnalysisFlow(entries, DET_ID(entries));
    const html = await renderHtml(flow);
    expect(html).toContain('1 轮循环 · 1 次调用');
    expect(html).not.toContain('子代理');
    expect(html).not.toContain('次审批');
    expect(html).not.toContain('秒');
  });
});

describe('ChatAnalysisFlow(冻结态展开)', () => {
  it('open=true 展开阶段树:阶段标题 + 步骤中文标签 + mono 耗时', async () => {
    const entries = linearEntries();
    const flow = buildAnalysisFlow(entries, DET_ID(entries));
    const html = await renderHtml(flow, { open: true });
    // 折叠头仍在(可收起)+ 阶段齐全
    expect(html).toContain('aflow-summary');
    expect(html).toContain('初步勘察');
    expect(html).toContain('深度取证');
    expect(html).toContain('裁决提交');
    expect(html).toContain('视频元信息(video_meta)');
    expect(html).toContain('定向跟踪(track_suspects)');
    expect(html).toContain('36s'); // 138000-102000
    expect(html).toContain('先看元信息再抽帧'); // thinking 一句话摘要(sans 说明行)
  });

  it('失败步红色(is-fail)+ 同名后续成功显示「已重试」标记', async () => {
    const entries = [
      user('q', 1000),
      tool('extract_frames', 2000, { isError: true, result: '抽帧失败' }),
      tool('extract_frames', 3000),
      detection(4000),
    ];
    const flow = buildAnalysisFlow(entries, DET_ID(entries));
    const html = await renderHtml(flow, { open: true });
    expect(html).toContain('is-fail');
    expect(html).toContain('已重试');
  });

  it('并行批渲染「并发」标签与横排 chips(两步标签都在)', async () => {
    const entries = [
      user('q', 1000),
      tool('extract_frames', 2000),
      tool('draw_boxes', 2300),
      detection(5000),
    ];
    const flow = buildAnalysisFlow(entries, DET_ID(entries));
    const parNode = flow.phases[0]!.nodes[0] as { kind?: string; steps?: FlowStep[] };
    expect(parNode.kind).toBe('parallel');
    const html = await renderHtml(flow, { open: true });
    expect(html).toContain('并发');
    expect(html).toContain('抽帧(extract_frames)');
    expect(html).toContain('画框标注(draw_boxes)');
    expect(html).toContain('aflow-chip');
  });

  it('子代理分支节点:任务描述 + 结论可见(内嵌子步骤默认折叠不渲染)', async () => {
    seq += 1;
    const entries: AgentEntry[] = [
      user('q', 1000),
      {
        id: `t${seq}`,
        kind: 'tool',
        callId: 'call-sub',
        name: 'spawn_subagent',
        args: JSON.stringify({ task: '核对画面细节' }),
        result: '结论:检出违停。\n依据略',
        images: [],
        hasVideo: false,
        children: [{ kind: 'tool', id: 'c1', name: 'read_file', args: '{}', done: true }],
        isError: false,
        done: true,
        at: 2000,
      },
      detection(4000),
    ];
    const flow = buildAnalysisFlow(entries, DET_ID(entries));
    const html = await renderHtml(flow, { open: true });
    expect(html).toContain('派生子代理');
    expect(html).toContain('核对画面细节');
    expect(html).toContain('结论:检出违停。');
    expect(html).not.toContain('读文件(read_file)'); // inner 默认折叠
  });
});

describe('ChatAnalysisFlow(实时态)', () => {
  it('realtime 无摘要行,恒展开;当前步挂脉冲类 is-run', async () => {
    const entries = [
      user('接着分析', 8000),
      tool('video_meta', 9000),
      tool('load_video', 12_000, { done: false }),
    ];
    const flow = buildAnalysisFlow(entries, null);
    const html = await renderHtml(flow, { realtime: true });
    expect(html).not.toContain('轮循环');
    expect(html).not.toContain('aflow-summary');
    expect(html).toContain('初步勘察');
    expect(html).toContain('is-run');
    expect(html).toContain('加载视频(load_video)');
  });

  it('零节点(只有思考流式)渲染为空容器', async () => {
    const entries = [user('q', 1000), assistant('思考中…', 1100)];
    const flow = buildAnalysisFlow(entries, null);
    const html = await renderHtml(flow, { realtime: true });
    expect(html).not.toContain('aflow-phase-title');
  });
});

describe('ChatEntryDetection 集成(冻结态接线冒烟)', () => {
  const detData = {
    normal: false,
    events: [
      {
        event_id: 1,
        detected: true,
        reasoning: '违停',
        evidence_frames: [3],
      },
    ],
    report_markdown: '# 报告',
  };

  it('传 flow 时检测卡内出现分析链路摘要行', async () => {
    const entries = linearEntries();
    const flow = buildAnalysisFlow(entries, DET_ID(entries));
    const app = createSSRApp({
      render: () =>
        h(ChatEntryDetection, {
          entry: { id: DET_ID(entries), kind: 'detection', data: detData },
          flow,
        }),
    });
    const html = await renderToString(app);
    expect(html).toContain('检测结果');
    expect(html).toContain('1 轮循环 · 3 次调用 · 95 秒');
  });

  it('不传 flow 时维持原样(无流程图 DOM)', async () => {
    const app = createSSRApp({
      render: () =>
        h(ChatEntryDetection, {
          entry: { id: 'dx', kind: 'detection', data: detData },
        }),
    });
    const html = await renderToString(app);
    expect(html).not.toContain('aflow-summary');
    expect(html).toContain('检测结果');
  });
});
