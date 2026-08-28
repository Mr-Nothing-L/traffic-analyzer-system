// 对话视图展示纯函数测试:
// - shouldSendOnEnter:输入法合成态(isComposing/keyCode 229)与 Shift+Enter 不发送;
// - toolLabel:工具名中文映射,未知工具回退原名;
// - workspaceVideoSrc:气泡视频地址由 path 确定性推导(历史重载同源);
// - copyText:clipboard API 缺失(非安全上下文)时回退 textarea + execCommand;
// - thinkSummaryLine:思考折叠行摘要(运行中取末行,结束后取首行);
// - toolErrorSummary:工具失败折叠摘要取错误首行(截断 80 字);
// - trackSuspectsView:track_suspects 取证产物行解析(三段路径 + 叠加视频 stream 地址)。
import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  copyText,
  lastUserEntryAt,
  shouldSendOnEnter,
  thinkSummaryLine,
  timelineEntries,
  toolErrorSummary,
  toolLabel,
  trackSuspectsView,
  workspaceVideoSrc,
} from '../chatDisplay';

describe('shouldSendOnEnter(composer Enter 发送判定)', () => {
  it('普通 Enter 发送', () => {
    expect(shouldSendOnEnter({ shiftKey: false })).toBe(true);
  });

  it('Shift+Enter 换行,不发送', () => {
    expect(shouldSendOnEnter({ shiftKey: true })).toBe(false);
  });

  it('输入法合成态(isComposing)的 Enter 是选词上屏,不发送', () => {
    expect(shouldSendOnEnter({ shiftKey: false, isComposing: true })).toBe(false);
  });

  it('keyCode 229(部分浏览器合成态只给 229)不发送', () => {
    expect(shouldSendOnEnter({ shiftKey: false, keyCode: 229 })).toBe(false);
  });
});

describe('toolLabel(工具名中文映射)', () => {
  it('已知工具显示「中文名(原名)」', () => {
    expect(toolLabel('video_meta')).toBe('视频元信息(video_meta)');
    expect(toolLabel('extract_frames')).toBe('抽帧(extract_frames)');
    expect(toolLabel('draw_boxes')).toBe('画框标注(draw_boxes)');
    expect(toolLabel('read_file')).toBe('读文件(read_file)');
    expect(toolLabel('write_file')).toBe('写文件(write_file)');
    expect(toolLabel('run_script')).toBe('运行脚本(run_script)');
    expect(toolLabel('submit_detection')).toBe('提交检测结果(submit_detection)');
  });

  it('未知工具回退原名', () => {
    expect(toolLabel('some_new_tool')).toBe('some_new_tool');
  });

  it('load_video / spawn_subagent 中文名', () => {
    expect(toolLabel('load_video')).toBe('加载视频(load_video)');
    expect(toolLabel('spawn_subagent')).toBe('派生子代理(spawn_subagent)');
  });
});

describe('timelineEntries(时间线条目过滤,链路节点即工具条目)', () => {
  it('剔除 tool 条目,其余(user/assistant/approval/detection/system)按原序保留', () => {
    const entries = [
      { id: 'u1', kind: 'user' },
      { id: 't1', kind: 'tool' },
      { id: 't2', kind: 'tool' },
      { id: 'a1', kind: 'assistant' },
      { id: 'p1', kind: 'approval' },
      { id: 'd1', kind: 'detection' },
      { id: 's1', kind: 'system' },
    ];
    expect(timelineEntries(entries).map((e) => e.id)).toEqual(['u1', 'a1', 'p1', 'd1', 's1']);
  });

  it('没有 tool 条目时原样返回(纯问答轮次)', () => {
    const entries = [{ id: 'u1', kind: 'user' }, { id: 'a1', kind: 'assistant' }];
    expect(timelineEntries(entries)).toEqual(entries);
  });
});

describe('lastUserEntryAt(轮次秒表起点)', () => {
  it('取最后一条 user 条目的 at(其后的 assistant/tool 不影响起点)', () => {
    const entries = [
      { kind: 'user', at: 1000 },
      { kind: 'assistant', at: 2000 },
      { kind: 'tool', at: 3000 },
      { kind: 'assistant', at: 4000 },
    ];
    expect(lastUserEntryAt(entries)).toBe(1000);
  });

  it('新一轮提问(含插话)刷新起点', () => {
    const entries = [
      { kind: 'user', at: 1000 },
      { kind: 'assistant', at: 2000 },
      { kind: 'user', at: 9000 },
    ];
    expect(lastUserEntryAt(entries)).toBe(9000);
  });

  it('无 user 条目、或 user 缺 at(旧数据)返回 null', () => {
    expect(lastUserEntryAt([{ kind: 'assistant', at: 1 }])).toBeNull();
    expect(lastUserEntryAt([{ kind: 'user' }, { kind: 'assistant', at: 2 }])).toBeNull();
    expect(lastUserEntryAt([])).toBeNull();
  });
});

describe('workspaceVideoSrc(气泡视频预览地址推导)', () => {
  it('有 src(当次上传附件)直接用', () => {
    expect(workspaceVideoSrc('a/b.mp4', '/api/agent/uploads/x.mp4', '/ws')).toBe(
      '/api/agent/uploads/x.mp4',
    );
  });

  it('工作区相对路径推 /api/workspace/stream(同路径恒同地址)', () => {
    expect(workspaceVideoSrc('sub/视频 1.mp4', undefined, '/ws')).toBe(
      `/api/workspace/stream?path=${encodeURIComponent('sub/视频 1.mp4')}`,
    );
  });

  it('工作区内绝对路径(如 .agent/uploads 落盘)剥前缀转相对', () => {
    expect(
      workspaceVideoSrc('/ws/.agent/uploads/20240101_a.mp4', undefined, '/ws'),
    ).toBe(`/api/workspace/stream?path=${encodeURIComponent('.agent/uploads/20240101_a.mp4')}`);
  });

  it('工作区外绝对路径推不出,返回 null(回退路径 chip)', () => {
    expect(workspaceVideoSrc('/other/x.mp4', undefined, '/ws')).toBeNull();
  });

  it('无 path 返回 null', () => {
    expect(workspaceVideoSrc(undefined, undefined, '/ws')).toBeNull();
  });
});

// node 环境无 DOM,这里垫最小假 document/navigator 验证回退分支
function fakeTextareaDoc(execResult: boolean) {
  const ta = {
    value: '',
    style: {} as Record<string, string>,
    setAttribute: vi.fn(),
    focus: vi.fn(),
    select: vi.fn(),
    remove: vi.fn(),
  };
  const doc = {
    createElement: vi.fn(() => ta),
    body: { appendChild: vi.fn() },
    execCommand: vi.fn(() => execResult),
  };
  return { ta, doc };
}

describe('copyText(消息复制)', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('clipboard API 可用时走 navigator.clipboard.writeText', async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    const { doc } = fakeTextareaDoc(true);
    vi.stubGlobal('document', doc);
    await copyText('你好');
    expect(writeText).toHaveBeenCalledWith('你好');
    expect(doc.execCommand).not.toHaveBeenCalled();
  });

  it('clipboard 缺失(非安全上下文,如局域网 IP)回退 textarea + execCommand', async () => {
    vi.stubGlobal('navigator', {}); // clipboard 为 undefined
    const { ta, doc } = fakeTextareaDoc(true);
    vi.stubGlobal('document', doc);
    await copyText('降级路径');
    expect(doc.createElement).toHaveBeenCalledWith('textarea');
    expect(ta.value).toBe('降级路径');
    expect(doc.body.appendChild).toHaveBeenCalledWith(ta);
    expect(ta.select).toHaveBeenCalled();
    expect(doc.execCommand).toHaveBeenCalledWith('copy');
    expect(ta.remove).toHaveBeenCalled(); // 隐藏节点用后清理
  });

  it('回退路径 execCommand 返回 false 时抛错(调用方提示「复制失败」),且仍清理节点', async () => {
    vi.stubGlobal('navigator', {});
    const { ta, doc } = fakeTextareaDoc(false);
    vi.stubGlobal('document', doc);
    await expect(copyText('x')).rejects.toThrow();
    expect(ta.remove).toHaveBeenCalled();
  });
});

describe('thinkSummaryLine(思考折叠行摘要)', () => {
  it('运行中显示最后一个非空行(跟随最新进展)', () => {
    expect(thinkSummaryLine('先理解需求\n\n再写代码\n', true)).toBe('再写代码');
  });

  it('结束后显示第一个非空行', () => {
    expect(thinkSummaryLine('先理解需求\n再写代码', false)).toBe('先理解需求');
  });

  it('空思考返回空串', () => {
    expect(thinkSummaryLine('', true)).toBe('');
    expect(thinkSummaryLine('\n  \n', false)).toBe('');
  });
});

describe('toolErrorSummary(工具失败摘要)', () => {
  it('取结果首个非空行', () => {
    expect(toolErrorSummary('\n读取失败:文件不存在\n堆栈第二行')).toBe('读取失败:文件不存在');
  });

  it('超长截断到 80 字并加省略号', () => {
    const long = 'x'.repeat(100);
    const out = toolErrorSummary(long);
    expect(out).toBe(`${'x'.repeat(80)}…`);
  });

  it('无内容回退「未知错误」', () => {
    expect(toolErrorSummary('')).toBe('未知错误');
    expect(toolErrorSummary('\n \n')).toBe('未知错误');
  });
});

describe('trackSuspectsView(track_suspects 取证产物行解析)', () => {
  // 与 agent/src/tools/builtin/trackSuspects.ts 附在输出末的产物文本行一致:
  // 「取证产物已保存:目录 <dir>;轨迹片段 <clip>;数据表 <csv>(供用户复核与引用)」
  const EMITTED = [
    '已跟踪 2 条目标轨迹,数值档案(JSON,数值问题的唯一引用来源)如下:',
    '全部轨迹叠加图:',
    '取证产物已保存:目录 .agent/tracks/E129/20250818_101530;' +
      '轨迹片段 .agent/tracks/E129/20250818_101530/track_overlay.mp4;' +
      '数据表 .agent/tracks/E129/20250818_101530/tracks.csv(供用户复核与引用)',
  ].join('\n');
  const CLIP = '.agent/tracks/E129/20250818_101530/track_overlay.mp4';

  it('标准产物行:解析三段路径,clip 推导出 stream 地址', () => {
    const view = trackSuspectsView(EMITTED);
    expect(view?.dir).toBe('.agent/tracks/E129/20250818_101530');
    expect(view?.clip).toBe(CLIP);
    expect(view?.csv).toBe('.agent/tracks/E129/20250818_101530/tracks.csv');
    expect(view?.videoSrc).toBe(`/api/workspace/stream?path=${encodeURIComponent(CLIP)}`);
  });

  it('半角标点、无尾注、行后还有内容也兼容(松于发射端格式)', () => {
    const view = trackSuspectsView(
      '前文\n取证产物已保存:目录 d;轨迹片段 c.mp4;数据表 t.csv\n后续文本',
    );
    expect(view).toEqual({
      dir: 'd',
      clip: 'c.mp4',
      csv: 't.csv',
      videoSrc: `/api/workspace/stream?path=${encodeURIComponent('c.mp4')}`,
    });
  });

  it('片段/数据表为「null」(toolserver 未产出)时 view 有值但不可播放', () => {
    const view = trackSuspectsView(
      '取证产物已保存:目录 .agent/tracks/X/T;轨迹片段 null;数据表 null(供用户复核与引用)',
    );
    expect(view?.dir).toBe('.agent/tracks/X/T');
    expect(view?.clip).toBeNull();
    expect(view?.videoSrc).toBeNull();
  });

  it('三段全为「null」视为无产物,整体返回 null', () => {
    expect(
      trackSuspectsView('取证产物已保存:目录 null;轨迹片段 null;数据表 null(供用户复核与引用)'),
    ).toBeNull();
  });

  it('无产物行(业务失败回退文本 / 其他工具结果 / 空结果)返回 null', () => {
    expect(trackSuspectsView('跟踪失败:无目标可跟踪,请退回纯视觉判断')).toBeNull();
    expect(trackSuspectsView('抽帧完成:共 12 帧')).toBeNull();
    expect(trackSuspectsView('')).toBeNull();
  });

  it('工作区外绝对路径片段推不出地址:降级保留路径字段', () => {
    const view = trackSuspectsView(
      '取证产物已保存:目录 /other/x;轨迹片段 /other/x/track_overlay.mp4;' +
        '数据表 /other/x/t.csv(供用户复核与引用)',
    );
    expect(view?.videoSrc).toBeNull();
    expect(view?.dir).toBe('/other/x');
    expect(view?.clip).toBe('/other/x/track_overlay.mp4');
  });
});
