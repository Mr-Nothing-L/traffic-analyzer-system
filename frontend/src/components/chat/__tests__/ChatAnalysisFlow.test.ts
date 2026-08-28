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
    images?: string[];
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
    images: opts.images ?? [],
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

describe('ChatAnalysisFlow(链路节点即工具条目,折叠/展开)', () => {
  /** 带真实结果的取证链:抽帧有图,track_suspects 有取证产物行。 */
  function forensicEntries(): AgentEntry[] {
    return [
      user('检测这段视频', 100_000),
      assistant('先看元信息', 101_000),
      tool('video_meta', 102_000),
      tool('extract_frames', 120_000, {
        result: '抽帧完成:共 2 帧',
        images: ['data:image/jpeg;base64,WFg='],
      }),
      tool('track_suspects', 138_000, {
        result:
          '已跟踪目标轨迹。\n取证产物已保存:目录 .agent/tracks/E1/T;' +
          '轨迹片段 .agent/tracks/E1/T/track_overlay.mp4;' +
          '数据表 .agent/tracks/E1/T/tracks.csv(供用户复核与引用)',
      }),
      tool('submit_detection', 190_000),
      detection(196_000),
    ];
  }

  const toolId = (entries: AgentEntry[], name: string) =>
    entries.find((e) => e.kind === 'tool' && e.name === name)!.id;

  it('节点默认折叠:展开阶段树后只有 label/耗时/状态,不渲染结果明细', async () => {
    const entries = forensicEntries();
    const flow = buildAnalysisFlow(entries, DET_ID(entries));
    const html = await renderHtml(flow, { open: true });
    expect(html).toContain('抽帧(extract_frames)'); // 折叠态仍可见 label
    expect(html).not.toContain('抽帧完成:共 2 帧'); // 结果文本
    expect(html).not.toContain('data:image/jpeg;base64,WFg='); // 结果图片
    expect(html).not.toContain('tool-track-video'); // 取证视频
  });

  it('expandedTools 命中节点:渲染结果文本 + 图片(preview 走画廊)', async () => {
    const entries = forensicEntries();
    const flow = buildAnalysisFlow(entries, DET_ID(entries));
    const html = await renderHtml(flow, {
      open: true,
      expandedTools: new Set([toolId(entries, 'extract_frames')]),
    });
    expect(html).toContain('抽帧完成:共 2 帧');
    expect(html).toContain('tool-imgs');
    expect(html).toContain('data:image/jpeg;base64,WFg=');
    // 未命中的节点仍折叠
    expect(html).not.toContain('tool-track-video');
  });

  it('track_suspects 节点展开:取证叠加视频与可复制目录路径可用', async () => {
    const entries = forensicEntries();
    const flow = buildAnalysisFlow(entries, DET_ID(entries));
    const html = await renderHtml(flow, {
      open: true,
      expandedTools: new Set([toolId(entries, 'track_suspects')]),
    });
    expect(html).toContain('tool-track-video');
    expect(html).toContain(
      `/api/workspace/stream?path=${encodeURIComponent('.agent/tracks/E1/T/track_overlay.mp4')}`,
    );
    expect(html).toContain('tool-artifacts-dir');
  });

  it('子代理节点展开:迷你时间线(think 块/子工具行)随明细渲染', async () => {
    seq += 1;
    const entries: AgentEntry[] = [
      user('q', 1000),
      {
        id: `t${seq}`,
        kind: 'tool',
        callId: 'call-sub',
        name: 'spawn_subagent',
        args: JSON.stringify({ task: '核对画面细节' }),
        result: '结论:检出违停。',
        images: [],
        hasVideo: false,
        children: [
          { kind: 'think', text: '子代理思考中' },
          { kind: 'tool', id: 'c1', name: 'read_file', args: '{}', done: true },
        ],
        isError: false,
        done: true,
        at: 2000,
      },
      detection(4000),
    ];
    const flow = buildAnalysisFlow(entries, DET_ID(entries));
    const collapsed = await renderHtml(flow, { open: true });
    expect(collapsed).not.toContain('子代理思考');
    const expandedHtml = await renderHtml(flow, {
      open: true,
      expandedTools: new Set([toolId(entries, 'spawn_subagent')]),
    });
    // 明细里:子代理迷你时间线折叠头 + 子工具行;think 文本仍在其二级折叠内
    expect(expandedHtml).toContain('子代理思考');
    expect(expandedHtml).toContain('读文件(read_file)');
  });

  it('面板提供「全部展开/全部折叠」工具行', async () => {
    const entries = forensicEntries();
    const flow = buildAnalysisFlow(entries, DET_ID(entries));
    const html = await renderHtml(flow, { open: true });
    expect(html).toContain('全部展开');
    expect(html).toContain('全部折叠');
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

  it('expandedTools 透传:冻结态检测卡内同样可展开节点明细', async () => {
    const entries: AgentEntry[] = [
      user('q', 1000),
      tool('extract_frames', 2000, { result: '抽帧完成:共 2 帧' }),
      detection(4000),
    ];
    const flow = buildAnalysisFlow(entries, DET_ID(entries));
    const tid = entries.find((e) => e.kind === 'tool')!.id;
    const render = (expandedTools?: Set<string>) =>
      renderToString(
        createSSRApp({
          render: () =>
            h(ChatEntryDetection, {
              entry: { id: DET_ID(entries), kind: 'detection', data: detData },
              flow,
              open: true,
              ...(expandedTools ? { expandedTools } : {}),
            }),
        }),
      );
    expect(await render()).not.toContain('抽帧完成:共 2 帧'); // 默认折叠
    expect(await render(new Set([tid]))).toContain('抽帧完成:共 2 帧'); // 展开明细
  });
});
