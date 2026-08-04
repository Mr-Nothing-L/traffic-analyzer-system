// 测试夹具：事件配置对齐 traffic_analyzer/config/event_options.yaml(封闭枚举唯一事实源),
// 样本结构对齐后端 evidence_schema.py 的 SftSample。
import type { EventDef, SftLabel } from '../types';

export const EVENTS: EventDef[] = [
  {
    event_id: 1, name_zh: '机动车违停', is_active: true,
    options: [
      { key: 'lane_type', label: '车道类型', required: true, options: ['行车道', '应急车道', '导流区', '路肩'] },
      { key: 'direction', label: '方向', required: true, options: ['来向', '去向'] },
      { key: 'vehicle_type', label: '车辆类型', required: true, options: ['小型车', '大客车', '货车', '工程车'] },
    ],
  },
  {
    event_id: 7, name_zh: '道路施工', is_active: true,
    options: [
      { key: 'direction', label: '方向', required: true, options: ['来向', '去向'] },
      { key: 'work_elements', label: '施工要素', required: true, multi: true, options: ['施工车辆', '交通锥/隔离栏', '施工标志牌', '施工人员', '车道封闭'] },
    ],
  },
  {
    // 未激活事件：定义了选项但编辑器不渲染 chips
    event_id: 10, name_zh: '抛洒物', is_active: false,
    options: [
      { key: 'direction', label: '方向', required: true, options: ['来向', '去向'] },
      { key: 'object_type', label: '物体类型', required: true, options: ['塑料袋/纸张', '水瓶/容器', '木板/构件', '泥土/散落物', '三角警示牌', '其他'] },
    ],
  },
  {
    // 无选项组事件：后端对 event_attributes 直接拒绝(「no options defined」)
    event_id: 11, name_zh: '实线变道', is_active: true,
  },
];

export function ev(id: number): EventDef {
  const e = EVENTS.find(x => x.event_id === id);
  if (!e) throw new Error('no event ' + id);
  return e;
}

// 基础样本：仅事件 1 检出,无结构化属性(纯文本卡场景)
export function makeSft(overrides: Partial<SftLabel> = {}): SftLabel {
  return {
    chunk: 'chunk #1',
    idx: 1,
    action: [1],
    description:
      '<think>\n机动车违停：来向一侧行车道内停有一辆小型车。\n</think>\n' +
      '<answer>\n天气：晴\n时间：白天\n场景：高速\n最终结论：本视频块检出以下事件。\nclass1: 机动车违停\n</answer>',
    start_timestamp: 0,
    end_timestamp: 5,
    chunk_name: '机动车违停_demo_1.mp4',
    ...overrides,
  };
}

// 声明通道样本：事件 1 全属性选中,正文含背景同形词「小型车」(第二处不应被标注)
export function makeDeclSft(): SftLabel {
  return makeSft({
    action: [1],
    description:
      '<think>\n机动车违停：来向一侧行车道内停有一辆小型车,另有一辆小型车驶过。\n</think>\n' +
      '<answer>\n天气：晴\n时间：白天\n场景：高速\n最终结论：本视频块检出以下事件。\nclass1: 机动车违停\n</answer>',
    event_attributes: { 1: { direction: '来向', lane_type: '行车道', vehicle_type: '小型车' } },
    attr_mentions: { 1: { direction: ['来向'], lane_type: ['行车道'], vehicle_type: ['小型车'] } },
  });
}

// 多选嵌套声明样本(事件 7 施工):新格式「选项名 → 提及串数组」,提及串为文本中的实际书写形态
export function makeMultiNestSft(): SftLabel {
  return makeSft({
    action: [7],
    description:
      '<think>\n道路施工：来向一侧道路施工,现场有工程车和锥桶。\n</think>\n' +
      '<answer>\n天气：晴\n时间：白天\n场景：高速\n最终结论：本视频块检出以下事件。\nclass7: 道路施工\n</answer>',
    event_attributes: { 7: { direction: '来向', work_elements: ['施工车辆', '交通锥/隔离栏'] } },
    attr_mentions: { 7: { work_elements: { '施工车辆': ['工程车'], '交通锥/隔离栏': ['锥桶'] } } },
  });
}

// 多选旧扁平数组声明样本(事件 7):提及串需按别名映射归选项
export function makeMultiFlatSft(): SftLabel {
  const sft = makeMultiNestSft();
  sft.attr_mentions = { 7: { work_elements: ['工程车', '锥桶'] } };
  return sft;
}
