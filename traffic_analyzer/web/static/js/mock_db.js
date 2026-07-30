/* ================================================================
   Mock 数据层(?mock=1)—— 数据库部分:mockDb 状态与结果/配置数据
   ================================================================ */
import { MOCK } from './state.js';

// 真实推理结果(scripts/build_mock_data.py 生成);不存在时回退下方合成数据
let REAL = null;
try {
  REAL = (await import('./mock_data.js')).REAL_MOCK;
} catch (e) { /* 无 mock_data.js:完全走合成兜底 */ }

// mock + 真实数据:演示视频的 <video src> 直连真实后端 /api/workspace/stream
// (不经过 mockApi),因此先把后端工作区切到演示区。用原生 fetch 而非 api(),
// 避免被 mockApi 拦截;失败静默(流不可用时前端有逐帧预览兜底)
if (MOCK && REAL && REAL.workspacePath) {
  try {
    const res = await fetch('/api/workspace');
    const cur = res.ok ? await res.json() : null;
    if (!cur || cur.path !== REAL.workspacePath) {
      await fetch('/api/workspace', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: REAL.workspacePath }),
      });
    }
  } catch (e) { /* 后端不可用:静默 */ }
}

const mockDb = {
  workspace: { path: '/mock/workspace' },
  // 顶层 + 嵌套(clips/)视频同源一份数据:树/列表/推理/结果都从这里查,has_results 天然同步
  videos: (REAL ? REAL.videos : [
    { name: '01-02_Event_101_1756000000000_1.mp4', stem: '01-02_Event_101_1756000000000_1', rel: '01-02_Event_101_1756000000000_1.mp4', size: 8388608, mtime: 1756000100, has_results: true },
    { name: '03_Event_102_1756000001000_1.mp4', stem: '03_Event_102_1756000001000_1', rel: '03_Event_102_1756000001000_1.mp4', size: 12582912, mtime: 1756000200, has_results: false },
    { name: '05-07_Event_129_1756000002000_1.mp4', stem: '05-07_Event_129_1756000002000_1', rel: '05-07_Event_129_1756000002000_1.mp4', size: 6291456, mtime: 1756000300, has_results: false },
  ]).concat([
    { name: 'nested_clip.mp4', stem: 'nested_clip', rel: 'clips/nested_clip.mp4', size: 1048576, mtime: 1756000500, has_results: false },
  ]),
  jobs: [],
  nextJobId: 1,
  results: {},
  reviewStates: {}, // 数据看板人工审核状态,stem → 'unconfirmed'|'confirmed'|'needs_review'(内存态,刷新即失)
  tickCount: 0,
};

// 目录弹窗用的模拟文件系统(?mock=1)
const mockFsTree = {
  '/': ['home', 'media', 'mock'],
  '/home': ['wanji'],
  '/home/wanji': ['projects', 'Videos'],
  '/home/wanji/projects': [],
  '/home/wanji/Videos': [],
  '/media': ['usb'],
  '/media/usb': [],
  '/mock': ['datasets', 'empty', 'workspace'],
  '/mock/datasets': ['labeled', 'raw'],
  '/mock/datasets/labeled': [],
  '/mock/datasets/raw': [],
  '/mock/empty': [],
  '/mock/workspace': ['analysis'],
  '/mock/workspace/analysis': [],
};

// 侧栏文件树的模拟数据(?mock=1);顶层视频与 clips/ 下的嵌套视频均由 mockDb.videos
// 动态生成,以便模拟推理完成后 has_results 同步变化
const mockWsDirs = {
  'analysis': [],
};

function mockVideoEntry(v) {
  return {
    name: v.name, rel: v.rel, type: 'file', is_video: true, stem: v.stem,
    size: v.size, mtime: v.mtime, has_results: v.has_results,
  };
}

function mockTreeEntries(rel) {
  if (rel === '') {
    return [
      { name: 'analysis', rel: 'analysis', type: 'dir' },
      { name: 'clips', rel: 'clips', type: 'dir' },
    ].concat(mockDb.videos.filter(v => v.rel.indexOf('/') < 0).map(mockVideoEntry))
      .concat([
        { name: '说明.md', rel: '说明.md', type: 'file', is_video: false, size: 2048, mtime: 1756000400 },
      ]);
  }
  if (rel === 'clips') {
    return mockDb.videos.filter(v => v.rel.indexOf('clips/') === 0).map(mockVideoEntry)
      .concat([
        { name: 'readme.txt', rel: 'clips/readme.txt', type: 'file', is_video: false, size: 512, mtime: 1756000600 },
      ]);
  }
  return mockWsDirs[rel];
}

const EVENT_NAMES_10 = ['违法停车', '应急车道占用', '交通事故', '高速公路行人出现', '摩托车出现',
  '拥堵', '道路施工', '车辆逆行/倒车', '抛洒物', '实线变道'];

function mockEvidence(stem) {
  const events = EVENT_NAMES_10.map((name, i) => ({
    event_id: i + 1,
    name: name,
    detected: i === 1,
    calibration: { frame_index: null, emergency_polygon_rel: null, chevron_polygon_rel: null },
    evidence_regions: [],
    gallery_images: [],
  }));
  const ev = events[1];
  ev.calibration = {
    frame_index: 4,
    emergency_polygon_rel: [[0.72, 0.35], [0.98, 0.42], [0.98, 0.95], [0.68, 0.92]],
    chevron_polygon_rel: [[0.05, 0.55], [0.22, 0.50], [0.30, 0.78], [0.10, 0.85]],
  };
  ev.evidence_regions = [
    { frame_index: 4, box_rel: [0.74, 0.48, 0.90, 0.66], label: '白色小车占用应急车道', image: 'images/zoom_ev2_1.jpg' },
    { frame_index: 6, box_rel: [0.40, 0.55, 0.52, 0.70], label: null, image: null },
  ];
  ev.gallery_images = ['images/comp_ev2_1.jpg', 'images/comp_ev2_2.jpg'];
  return {
    schema_version: 1,
    video: { file_name: stem + '.mp4', duration_sec: 15.0, fps: 25.0, width: 1920, height: 1080 },
    events: events,
  };
}

// 与 traffic_analyzer/config/event_categories.yaml 一致:编号 1-8 激活,10/11 未激活
// (编号 9 为「正常」占位,不在配置中;SFT 编辑器按 event_id 匹配草稿/勾选态)
// 有 REAL 时由 build_mock_data.py 从 yaml 生成(含 options 结构化选项组)
const MOCK_EVENT_IDS = [1, 2, 3, 4, 5, 6, 7, 8, 10, 11];
let MOCK_EVENT_CONFIG = EVENT_NAMES_10.map((name, i) => {
  const id = MOCK_EVENT_IDS[i];
  return { event_id: id, name_zh: name, is_active: id <= 8 };
});
if (REAL) MOCK_EVENT_CONFIG = REAL.eventConfig;

function mockSft(stem) {
  return {
    chunk: 'chunk #1', idx: 1, action: [2],
    description: '<think>\n违法停车：未发现违法停车。画面中无相关迹象。\n\n'
      + '应急车道占用：应急车道区域为画面最右侧白色实线以外;一辆白色小车静止于去向一侧应急车道内。\n\n'
      + '交通事故：未发现交通事故。画面中无相关迹象。\n</think>\n'
      + '<answer>\n天气：晴天\n时间：白天\n场景：高速公路双向主路场景,车流量中等。\n'
      + '最终结论：本视频块检出以下事件。\nclass2: 应急车道占用\n</answer>',
    start_timestamp: 0.0, end_timestamp: 15.0, chunk_name: stem + '.mp4',
  };
}

function mockReport() {
  return '# 交通事件分析报告\n\n**视频**: `demo.mp4`  ** chunk**: chunk #1\n\n' +
    '## 检测结论\n\n| 事件ID | 事件名称 | 是否检出 |\n|--------|----------|----------|\n' +
    '| 1 | 违法停车 | 否 |\n| 2 | 应急车道占用 | **是** |\n| 3 | 交通事故 | 否 |\n\n' +
    '## 证据\n\n- 帧 4:白色小车静止于应急车道\n- 帧 6:车辆仍未移动\n\n```\n最终结论: class2 应急车道占用\n```\n';
}

function mockResults(stem) {
  if (!mockDb.results[stem]) {
    // 有真实结果时优先用(REAL.results 由 build_mock_data.py 生成);
    // 证据图片引用沿用 mockImageUrl 占位,不搬真实图片
    if (REAL && REAL.results && REAL.results[stem]) {
      mockDb.results[stem] = REAL.results[stem];
    } else {
      mockDb.results[stem] = {
        report_md: mockReport(),
        sft_label: mockSft(stem),
        evidence: mockEvidence(stem),
      };
    }
  }
  return mockDb.results[stem];
}

export {
  REAL, mockDb, mockFsTree, mockTreeEntries, mockResults,
  MOCK_EVENT_CONFIG,
};
