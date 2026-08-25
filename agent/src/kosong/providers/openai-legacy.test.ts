/**
 * Serialization tests for the local openai-legacy modification: tool-result
 * `video_url` parts are reattached as a follow-up user message (serialized as
 * OpenAI `{type:'video_url', video_url:{url}}`) instead of being dropped with
 * a "(video omitted)" placeholder. All traffic is captured by a fake client;
 * no real API is touched.
 */
import type OpenAI from 'openai';
import { describe, expect, it } from 'vitest';

import { createToolMessage, createUserMessage, type Message } from '#/message';

import { OpenAILegacyChatProvider } from './openai-legacy';

interface CapturedRequest {
  params?: Record<string, unknown> | undefined;
}

function fakeClient(capture: CapturedRequest): OpenAI {
  return {
    chat: {
      completions: {
        create: async (params: Record<string, unknown>) => {
          capture.params = params;
          return {
            id: 'chatcmpl-test',
            choices: [
              { message: { role: 'assistant', content: 'ok' }, finish_reason: 'stop' },
            ],
          };
        },
      },
    },
  } as unknown as OpenAI;
}

function makeProvider(capture: CapturedRequest): OpenAILegacyChatProvider {
  return new OpenAILegacyChatProvider({
    model: 'qwen-test',
    stream: false,
    clientFactory: () => fakeClient(capture),
  });
}

function historyWithToolResult(toolContent: Message['content']): Message[] {
  return [
    createUserMessage('分析这段视频'),
    {
      role: 'assistant',
      content: [],
      toolCalls: [
        { type: 'function', id: 'call_1', name: 'load_video', arguments: '{}' },
      ],
    },
    createToolMessage('call_1', toolContent),
  ];
}

interface WireMessage {
  role: string;
  content?: unknown;
  tool_call_id?: string;
}

describe('openai-legacy tool-result video serialization', () => {
  it('reattaches a tool-result video_url part as a follow-up user message', async () => {
    const capture: CapturedRequest = {};
    const provider = makeProvider(capture);
    const videoUrl = 'data:video/mp4;base64,AAAA';

    await provider.generate(
      '系统提示',
      [],
      historyWithToolResult([
        { type: 'text', text: '已加载完整视频:时长 20s' },
        { type: 'video_url', videoUrl: { url: videoUrl } },
      ]),
    );

    const messages = capture.params?.['messages'] as WireMessage[];
    const toolMessage = messages.find((m) => m.role === 'tool');
    expect(toolMessage?.content).toBe('已加载完整视频:时长 20s');
    expect(String(toolMessage?.content)).not.toContain('video omitted');

    const toolIndex = messages.indexOf(toolMessage as WireMessage);
    const mediaMessage = messages[toolIndex + 1];
    expect(mediaMessage?.role).toBe('user');
    expect(mediaMessage?.content).toEqual([
      { type: 'text', text: 'Attached media from tool result:' },
      { type: 'video_url', video_url: { url: videoUrl } },
    ]);
  });

  it('still omits audio parts with the placeholder (unchanged behavior)', async () => {
    const capture: CapturedRequest = {};
    const provider = makeProvider(capture);

    await provider.generate(
      '系统提示',
      [],
      historyWithToolResult([
        { type: 'text', text: 'audio ready' },
        { type: 'audio_url', audioUrl: { url: 'data:audio/wav;base64,BBBB' } },
      ]),
    );

    const messages = capture.params?.['messages'] as WireMessage[];
    const toolMessage = messages.find((m) => m.role === 'tool');
    expect(String(toolMessage?.content)).toContain('audio ready');
    expect(String(toolMessage?.content)).toContain('(audio omitted');
    // No media reattachment user message for audio.
    expect(messages.some((m) => m.role === 'user' && Array.isArray(m.content))).toBe(false);
  });
});
