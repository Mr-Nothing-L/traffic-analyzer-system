/**
 * 手动冒烟脚本(不在 vitest 内运行,会打真实 API):
 *
 *   npx tsx src/llm/smoke.ts [envPath]
 *
 * 读取 `.env`(默认仓库根 traffic_analyzer/config/.env,可用参数覆盖)配置的
 * primary provider,做两次真实调用:
 *   1. 纯文本对话;
 *   2. 带 tool 的调用(get_current_time),并把工具结果回灌拿最终回答。
 */
import { pathToFileURL } from 'node:url';

import { generate } from '#/generate';
import { createToolMessage, createUserMessage, extractText, type Message } from '#/message';
import type { Tool } from '#/tool';

import { defaultEnvPath } from './env.ts';
import { createProviderFromEnv } from './provider.ts';

const GET_CURRENT_TIME: Tool = {
  name: 'get_current_time',
  description: '获取当前日期时间',
  parameters: {
    type: 'object',
    properties: {
      timezone: { type: 'string', description: 'IANA 时区名,如 Asia/Shanghai' },
    },
    required: [],
  },
};

function maskApiKey(key: string): string {
  if (key.length <= 8) return key === '' ? '<empty>' : '****';
  return `${key.slice(0, 4)}****${key.slice(-4)}`;
}

async function main(): Promise<void> {
  const envPath = process.argv[2] ?? defaultEnvPath();
  const { provider, model, config } = createProviderFromEnv(envPath);
  console.log(
    `[smoke] env=${envPath} provider=${config.provider} model=${model} ` +
      `baseUrl=${config.baseUrl ?? '<default>'} apiKey=${maskApiKey(config.apiKey)}`,
  );

  // Round 1: 纯文本
  const textResult = await generate(provider, '你是一个简洁的助手。', [], [
    createUserMessage('用一句话介绍你自己。'),
  ]);
  console.log('[smoke] text round finishReason=', textResult.finishReason);
  console.log('[smoke] text round output:', extractText(textResult.message));
  console.log('[smoke] usage:', textResult.usage);

  // Round 2: tool call → 结果回灌 → 最终回答
  const history: Message[] = [createUserMessage('现在几点了?请使用工具查询。')];
  const toolResult = await generate(provider, '你是一个可以使用工具的助手。', [GET_CURRENT_TIME], history);
  console.log('[smoke] tool round finishReason=', toolResult.finishReason);
  if (toolResult.message.toolCalls.length === 0) {
    console.log('[smoke] model did NOT emit a tool call; raw text:', extractText(toolResult.message));
    return;
  }
  for (const call of toolResult.message.toolCalls) {
    console.log(`[smoke] tool call: ${call.name}(${call.arguments ?? ''}) id=${call.id}`);
  }
  history.push(toolResult.message);
  for (const call of toolResult.message.toolCalls) {
    history.push(
      createToolMessage(call.id, JSON.stringify({ current_time: new Date().toISOString() })),
    );
  }
  const finalResult = await generate(provider, '你是一个可以使用工具的助手。', [GET_CURRENT_TIME], history);
  console.log('[smoke] final answer:', extractText(finalResult.message));
  console.log('[smoke] OK');
}

const invokedAsScript =
  process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invokedAsScript) {
  main().catch((error: unknown) => {
    console.error('[smoke] FAILED:', error);
    process.exitCode = 1;
  });
}
