/** ChatEntryAssistant 渲染测试:气泡隐藏规则(hideThink/hideText 独立控制)。
 * 经 @vue/server-renderer SSR 直渲染(纯 node 环境);
 * 隐藏 id 集合的推导口径见 utils/__tests__/chatDisplay.test.ts。 */
import { describe, it, expect } from 'vitest';
import { createSSRApp, h } from 'vue';
import { renderToString } from 'vue/server-renderer';

import ChatEntryAssistant from '../ChatEntryAssistant.vue';
import type { AgentEntry } from '../../../stores/agentchat';

let seq = 0;
function assistantEntry(text: string, think = ''): AgentEntry {
  seq += 1;
  return { id: `a${seq}`, kind: 'assistant', text, think, at: 1000 };
}

async function renderHtml(entry: AgentEntry, extra: Record<string, unknown> = {}): Promise<string> {
  const app = createSSRApp({
    render: () =>
      h(ChatEntryAssistant, {
        entry,
        copied: false,
        streaming: false,
        thinkOpen: false,
        time: '12:00',
        ...extra,
      }),
  });
  return await renderToString(app);
}

describe('ChatEntryAssistant(气泡隐藏规则)', () => {
  it('缺省渲染思考折叠与正文(markdown)', async () => {
    const html = await renderHtml(assistantEntry('**正文**内容', '思考内容'));
    expect(html).toContain('思考过程');
    expect(html).toContain('思考内容');
    expect(html).toContain('<strong>正文</strong>内容');
  });

  it('hideThink 隐藏思考,正文保留(思考改由链路面板思考节点呈现)', async () => {
    const html = await renderHtml(assistantEntry('正文内容', '思考内容'), { hideThink: true });
    expect(html).not.toContain('思考内容');
    expect(html).toContain('正文内容');
  });

  it('hideText 隐藏正文(不渲染 markdown),思考折叠不受影响', async () => {
    const html = await renderHtml(assistantEntry('正文内容', '思考内容'), { hideText: true });
    expect(html).not.toContain('正文内容');
    expect(html).toContain('思考内容');
  });

  it('正文与思考都隐藏时不留空壳气泡', async () => {
    const html = await renderHtml(assistantEntry('正文', '思考'), {
      hideThink: true,
      hideText: true,
    });
    expect(html).not.toContain('row assistant');
  });
});
