/** ChatEntryDetection 渲染测试:检出事件无 annotated_image 时显示低调「未定位」
 * 小标;有标注图则渲染 <img> 且不显示该标。经 @vue/server-renderer SSR 直渲染
 * (纯 node 环境,vue 自带依赖,无需 jsdom/@vue/test-utils)。 */
import { describe, it, expect } from 'vitest';
import { createSSRApp, h } from 'vue';
import { renderToString } from 'vue/server-renderer';

import ChatEntryDetection from '../ChatEntryDetection.vue';
import type { AgentEntry } from '../../../stores/agentchat';

const JPEG_URL = 'data:image/jpeg;base64,WFg=';

function detectionEntry(data: unknown): AgentEntry {
  return { id: 'entry-1', kind: 'detection', data };
}

async function renderHtml(data: unknown): Promise<string> {
  const app = createSSRApp({
    render: () => h(ChatEntryDetection, { entry: detectionEntry(data) }),
  });
  return await renderToString(app);
}

describe('ChatEntryDetection', () => {
  it('无标注图的检出事件渲染「未定位」小标(且无 <img>)', async () => {
    const html = await renderHtml({
      video_path: 'demo.mp4',
      binary_encoding: '0_0_1_0_0_0_0_0_0_0_0',
      normal: false,
      events: [
        {
          event_id: 3,
          detected: true,
          confidence: 0.9,
          instances: [],
          reasoning: '静止车辆',
          evidence_frames: [3.0],
        },
      ],
      report_markdown: '# 报告\n检出事件 3。',
    });
    expect(html).toContain('detection-event-note');
    expect(html).toContain('未定位');
    expect(html).not.toContain('detection-event-img');
  });

  it('有 annotated_image 的事件渲染标注图,不显示「未定位」', async () => {
    const html = await renderHtml({
      binary_encoding: '0_0_1_0_0_0_0_0_0_0_0',
      normal: false,
      events: [
        {
          event_id: 3,
          detected: true,
          confidence: 0.9,
          instances: [],
          reasoning: '静止车辆',
          evidence_frames: [3.0],
          annotated_image: JPEG_URL,
        },
      ],
      report_markdown: '# 报告',
    });
    expect(html).toContain('detection-event-img');
    expect(html).toContain(JPEG_URL);
    // 注:SSR 会保留模板内注释(文案恰含「未定位」三字),故按小标类名断言。
    expect(html).not.toContain('detection-event-note');
  });

  it('normal 提交(无检出事件)不渲染「未定位」小标', async () => {
    const html = await renderHtml({
      binary_encoding: '0_0_0_0_0_0_0_0_1_0_0',
      normal: true,
      events: [
        {
          event_id: 1,
          detected: false,
          confidence: 0.1,
          instances: [],
          reasoning: '未见异常',
          evidence_frames: [],
        },
      ],
      report_markdown: '# 报告',
    });
    expect(html).toContain('正常');
    expect(html).not.toContain('detection-event-note');
  });
});
