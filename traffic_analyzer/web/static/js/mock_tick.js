/* ================================================================
   Mock 数据层(?mock=1)—— 专家泳道慢速模拟与 mockTick 推进
   ================================================================ */
import { REAL, mockDb, MOCK_EVENT_CONFIG } from './mock_db.js';

/* ------------------------------------------------------------ 专家泳道慢速模拟 */
// 与 config 中 8 个激活类别一致 + 最后的「裁决」泳道;label 为各阶段中文短文案
const MOCK_EXPERT_DEFS = [
  ['违法停车', ['扫描路肩与车道边缘', '比对目标静止时长', '排除缓行车流误报']],
  ['应急车道占用', ['标定应急车道区域', '检测车道内停留目标', '核对特种车辆豁免特征']],
  ['交通事故', ['检测车辆异常姿态', '识别碎片与散落痕迹', '分析车流速度突变']],
  ['高速公路行人出现', ['扫描人体轮廓特征', '追踪目标移动轨迹', '排除护栏阴影干扰']],
  ['摩托车出现', ['检测两轮目标', '核对车型长宽比例', '评估行驶车道合法性']],
  ['拥堵', ['统计车道车流密度', '估算区间平均车速', '定位缓行队列尾部']],
  ['道路施工', ['识别锥桶与围挡', '检测施工机械特征', '核对车道封闭标志']],
  ['车辆逆行/倒车', ['估计车辆行驶方向', '比对断面车流主流向', '确认逆向持续时长']],
  ['裁决', ['汇总各专家结论', '仲裁冲突证据', '生成最终判定']],
];
// /api/expert-phases 的 mock 应答:里程碑取 (i+1)/(n+1),不含 1.0,给前端缓行封顶留出余量
const MOCK_EXPERT_PHASES = {};
MOCK_EXPERT_DEFS.forEach(([name, labels]) => {
  MOCK_EXPERT_PHASES[name] = labels.map((label, i) => ({
    fraction: +(((i + 1) / (labels.length + 1)).toFixed(2)), label: label,
  }));
});

function initMockExperts() {
  const lanes = MOCK_EXPERT_DEFS.map(([name]) => ({
    name: name, status: 'queued', detected: null, fraction: 0, label: '等待调度',
  }));
  // 阶段泳道从一开始就占位(排队态),与真实后端的泳道表对齐
  lanes.push({ name: 'SFT 标注', status: 'queued', detected: null, fraction: 0, label: '等待调度' });
  lanes.push({ name: '报告', status: 'queued', detected: null, fraction: 0, label: '等待调度' });
  return lanes;
}

// 推进一条泳道 0.05-0.2,并按里程碑刷新阶段文案;到 1.0 置 done
// detectedIds 非空时(真实结果)按 泳道名→event_id 映射判定检出,否则走合成兜底
function advanceLane(lane, detectedIds) {
  lane.fraction = Math.min(1, +(lane.fraction + 0.05 + Math.random() * 0.15).toFixed(3));
  const phases = MOCK_EXPERT_PHASES[lane.name];
  const idx = phases.findIndex(s => s.fraction > lane.fraction);
  lane.label = phases[idx >= 0 ? idx : phases.length - 1].label;
  if (lane.fraction >= 1) {
    lane.status = 'done';
    if (lane.name === '裁决') {
      lane.detected = true; // 裁决泳道视为有结论
    } else if (detectedIds) {
      const ev = MOCK_EVENT_CONFIG.find(e => e.name_zh === lane.name);
      lane.detected = !!(ev && detectedIds.indexOf(ev.event_id) >= 0);
    } else {
      // 与合成 mock 结果集一致:仅「应急车道占用」检出
      lane.detected = lane.name === '应急车道占用';
    }
    lane.label = lane.detected ? '检出疑似目标' : '未发现相关迹象';
  }
}

export function mockTick() {
  mockDb.tickCount++;
  // 推理 job:串行推进
  let running = mockDb.jobs.find(j => j.status === 'running');
  if (!running) {
    const next = mockDb.jobs.find(j => j.status === 'queued');
    if (next) { next.status = 'running'; running = next; }
  }
  if (running) {
    if (running.kind === 'infer') {
      // 8 个类别专家泳道 + 「裁决」泳道的慢速 staggered 步进
      if (!running._experts) running._experts = initMockExperts();
      // 真实结果的检出集合(首次 tick 时确定);无则回退合成逻辑
      if (running._detected === undefined) {
        running._detected = (REAL && REAL.detectedMap && REAL.detectedMap[running.stem]) || null;
      }
      const experts = running._experts;
      // 类别泳道(不含裁决与 SFT/报告阶段泳道,阶段泳道不参与随机推进)
      const lanes = experts.filter(e => ['裁决', 'SFT 标注', '报告'].indexOf(e.name) < 0);
      const verdict = experts.find(e => e.name === '裁决');
      // 4 并发上限的假象:running 不足 4 条时启动下一条排队泳道
      const runningLanes = lanes.filter(e => e.status === 'running');
      if (runningLanes.length < 4) {
        const nextLane = lanes.find(e => e.status === 'queued');
        if (nextLane) {
          nextLane.status = 'running';
          nextLane.label = MOCK_EXPERT_PHASES[nextLane.name][0].label;
          runningLanes.push(nextLane);
        }
      }
      // 每 tick 随机挑 1-2 条 running 泳道推进
      const pool = runningLanes.slice();
      const picks = Math.min(pool.length, 1 + Math.floor(Math.random() * 2));
      for (let i = 0; i < picks; i++) {
        advanceLane(pool.splice(Math.floor(Math.random() * pool.length), 1)[0], running._detected);
      }
      // 全部类别 done 后才推进裁决泳道
      if (lanes.every(e => e.status === 'done')) {
        if (verdict.status === 'queued') {
          verdict.status = 'running';
          verdict.label = MOCK_EXPERT_PHASES['裁决'][0].label;
        } else if (verdict.status === 'running') {
          advanceLane(verdict, running._detected);
        }
      }
      // 总进度 = 全体泳道均值,与后端 jobs.py _recompute_fraction 同刻度
      // (专家阶段尚无阶段泳道,均值天然只含类别+裁决)
      const frac = experts.reduce((s, e) => s + (e.fraction || 0), 0) / experts.length;
      running.progress = {
        step_label: verdict.status === 'queued' ? '专家分析' : '裁决',
        step_index: verdict.status === 'queued' ? 2 : 3,
        total_steps: 5,
        fraction: +frac.toFixed(3),
        experts: experts,
      };
      running.log_tail = '[mock] 专家泳道完成 '
        + experts.filter(e => e.status === 'done').length + '/' + experts.length;
      if (verdict.status === 'done') {
        // 裁决后补两条阶段泳道:SFT 标注 → 报告,各 3 拍生命周期且 fraction 分拍递进。
        // 前端轮询 1.5s/次,每拍 0.7s,3 拍可保证两条泳道稳定可见,与真实任务周期对齐
        const STAGE_STEPS = [
          ['SFT 标注', 0.2, '读取裁决结论', 'SFT', 4],
          ['SFT 标注', 0.5, 'SFT 标签改写', 'SFT', 4],
          ['SFT 标注', 0.85, '校验 SFT 样本', 'SFT', 4],
          ['报告', 0.2, '汇总检测结果', '报告', 5],
          ['报告', 0.5, '生成分析报告', '报告', 5],
          ['报告', 0.85, '润色报告结论', '报告', 5],
        ];
        const stageIdx = running._stage || 0;
        running._stage = stageIdx + 1;
        if (stageIdx < STAGE_STEPS.length) {
          const st = STAGE_STEPS[stageIdx];
          // 泳道在 initMockExperts 已占位;每拍先收口其他阶段泳道,再推进当前泳道
          experts.forEach(e => {
            if ((e.name === 'SFT 标注' || e.name === '报告') && e.name !== st[0]
                && e.status === 'running') {
              Object.assign(e, { status: 'done', fraction: 1, label: e.name + '完成' });
            }
          });
          const lane = experts.find(e => e.name === st[0]);
          if (lane) {
            lane.status = 'running';
            lane.fraction = st[1];
            lane.label = st[2];
          }
          running.progress = {
            step_label: st[3], step_index: st[4], total_steps: 5,
            // 与后端一致:全体泳道均值(此时类别+裁决已全 1.0,天然单调)
            fraction: +(experts.reduce((s, e) => s + (e.fraction || 0), 0) / experts.length).toFixed(3),
            experts: experts,
          };
        } else {
          const rep = experts.find(e => e.name === '报告');
          if (rep) Object.assign(rep, { status: 'done', fraction: 1, label: '报告完成' });
          running.status = 'done';
          running.progress = { step_label: '完成', step_index: 5, total_steps: 5, fraction: 1, experts: experts };
          running.returncode = 0;
          const v = mockDb.videos.find(v => v.stem === running.stem);
          if (v) v.has_results = true;
        }
      }
    }
  }
}

export { MOCK_EXPERT_PHASES };
