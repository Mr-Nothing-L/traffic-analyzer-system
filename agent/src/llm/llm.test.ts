/**
 * llm 模块单元测试:
 * - env.ts 的 `.env` 解析语义(对齐 Python `_load_env_llm_providers`);
 * - provider.ts 的 aliyun → OpenAI 兼容适配;
 * - kosong generate() 对 OpenAI chat-completions SSE 流的组装
 *   (本地 node:http mock server,不打真实 API)。
 */
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { createServer, type Server } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  generate,
  createAssistantMessage,
  createToolMessage,
  createUserMessage,
  extractText,
  OpenAILegacyChatProvider,
  type Message,
  type ToolCall,
  type Tool,
} from './kosong';

import { createCompactionConfig } from '../loop/compaction';
import { compactMessagesWithSummary } from '../loop/summarize';

import {
  buildProviderConfig,
  loadEnvLLMProviders,
  mergeDotenvIntoProcessEnv,
  parseDotenv,
} from './env.ts';
import { createProviderFromEnv, withThinkingDisabled } from './provider.ts';

// ---------------------------------------------------------------------------
// env.ts
// ---------------------------------------------------------------------------

describe('parseDotenv', () => {
  it('解析键值、引号与注释', () => {
    const env = parseDotenv(
      [
        '# comment',
        'FOO=bar',
        'QUOTED="hello world"',
        "SINGLE='x y'",
        'INLINE=value # trailing comment',
        'export EXPORTED=1',
        '',
        'NO_EQUALS_SIGN',
        'EMPTY=',
      ].join('\n'),
    );
    expect(env).toEqual({
      FOO: 'bar',
      QUOTED: 'hello world',
      SINGLE: 'x y',
      INLINE: 'value',
      EXPORTED: '1',
      EMPTY: '',
    });
  });
});

describe('buildProviderConfig', () => {
  it('indexed 前缀读取 + provider-specific 覆盖', () => {
    const env = parseDotenv(
      [
        'LLM_PROVIDER_0_PROVIDER=aliyun',
        'LLM_PROVIDER_0_API_KEY=generic-key',
        'ALIYUN_API_KEY=specific-key',
        'LLM_PROVIDER_0_BASE_URL=http://generic/v1',
        'ALIYUN_BASE_URL=http://specific/v1',
        'LLM_PROVIDER_0_MODEL=qwen-generic',
        'ALIYUN_MODEL=qwen-specific',
        'LLM_PROVIDER_0_MAX_TOKENS=8192',
        'LLM_PROVIDER_0_TEMPERATURE=0.7',
      ].join('\n'),
    );
    const config = buildProviderConfig(env, 'LLM_PROVIDER_0');
    expect(config).toMatchObject({
      provider: 'aliyun',
      apiKey: 'specific-key',
      baseUrl: 'http://specific/v1',
      model: 'qwen-specific',
      maxTokens: 8192,
      temperature: 0.7,
    });
  });

  it('legacy 模式支持 VLM_PROVIDER', () => {
    const env = parseDotenv(['VLM_PROVIDER=aliyun', 'LLM_MODEL=qwen3', 'LLM_BASE_URL=http://x/v1'].join('\n'));
    const config = buildProviderConfig(env, null);
    expect(config.provider).toBe('aliyun');
    expect(config.model).toBe('qwen3');
    expect(config.baseUrl).toBe('http://x/v1');
  });
});

describe('loadEnvLLMProviders', () => {
  let dir: string;
  const writeEnv = (content: string): string => {
    const path = join(dir, '.env');
    writeFileSync(path, content);
    return path;
  };

  beforeAll(() => {
    dir = mkdtempSync(join(tmpdir(), 'llm-env-'));
  });
  afterAll(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('无 indexed 变量时回退 legacy 单 provider', () => {
    const path = writeEnv('LLM_PROVIDER=aliyun\nLLM_MODEL=m\n');
    const providers = loadEnvLLMProviders(path);
    expect(providers).toHaveLength(1);
    expect(providers[0]?.provider).toBe('aliyun');
  });

  it('按实际 index 顺序解析全部 provider(跳号不补空;TS 侧只消费 configs[0])', () => {
    const path = writeEnv(
      [
        'LLM_PROVIDER_0_PROVIDER=aliyun',
        'LLM_PROVIDER_1_PROVIDER=openai',
        'LLM_PROVIDER_3_PROVIDER=vllm', // 跳号 index 2
      ].join('\n'),
    );
    const providers = loadEnvLLMProviders(path);
    expect(providers.map((p) => p.provider)).toEqual(['aliyun', 'openai', 'vllm']);
  });

  it('TS 侧不处理 LLM_AUTO_SWITCH / _ENABLED 过滤语义', () => {
    const path = writeEnv(
      [
        'LLM_PROVIDER_0_PROVIDER=aliyun',
        'LLM_PROVIDER_0_ENABLED=0',
        'LLM_PROVIDER_1_PROVIDER=openai',
        'LLM_PROVIDER_1_ENABLED=0',
        'LLM_AUTO_SWITCH=0',
      ].join('\n'),
    );
    expect(loadEnvLLMProviders(path).map((p) => p.provider)).toEqual([
      'aliyun',
      'openai',
    ]);
  });

  it('.env 缺失时返回默认配置单元素列表', () => {
    const providers = loadEnvLLMProviders(join(dir, 'does-not-exist.env'));
    expect(providers).toHaveLength(1);
    expect(providers[0]?.provider).toBe('anthropic');
  });
});

describe('mergeDotenvIntoProcessEnv', () => {
  let dir: string;
  const writeEnv = (content: string): string => {
    const path = join(dir, '.env');
    writeFileSync(path, content);
    return path;
  };

  beforeAll(() => {
    dir = mkdtempSync(join(tmpdir(), 'llm-merge-env-'));
  });
  afterAll(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('把 .env 变量补进 process.env,不覆盖已存在的 shell 变量', () => {
    const path = writeEnv(
      [
        'AGENT_PORT=9999',
        'AGENT_HOST=0.0.0.0',
        'AGENT_ENABLE_THINKING=false',
        'AGENT_MAX_TOKENS=8192',
      ].join('\n'),
    );
    const prevPort = process.env.AGENT_PORT;
    process.env.AGENT_PORT = '8602'; // 模拟 shell 导出,应保持不变
    try {
      mergeDotenvIntoProcessEnv(path);
      expect(process.env.AGENT_PORT).toBe('8602');
      expect(process.env.AGENT_HOST).toBe('0.0.0.0');
      expect(process.env.AGENT_ENABLE_THINKING).toBe('false');
      expect(process.env.AGENT_MAX_TOKENS).toBe('8192');
    } finally {
      delete process.env.AGENT_HOST;
      delete process.env.AGENT_ENABLE_THINKING;
      delete process.env.AGENT_MAX_TOKENS;
      if (prevPort === undefined) {
        delete process.env.AGENT_PORT;
      } else {
        process.env.AGENT_PORT = prevPort;
      }
    }
  });
});

describe('createProviderFromEnv', () => {
  let dir: string;
  beforeAll(() => {
    dir = mkdtempSync(join(tmpdir(), 'llm-provider-'));
  });
  afterAll(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('aliyun 视为 OpenAI 兼容并构造 OpenAILegacyChatProvider', () => {
    const path = join(dir, '.env');
    writeFileSync(
      path,
      [
        'LLM_PROVIDER_0_PROVIDER=aliyun',
        'LLM_PROVIDER_0_API_KEY=test-key',
        'LLM_PROVIDER_0_MODEL=qwen3.8-27b-fp8',
        'LLM_PROVIDER_0_BASE_URL=http://10.103.0.6:8003/v1',
      ].join('\n'),
    );
    const { provider, model, config } = createProviderFromEnv(path);
    expect(provider).toBeInstanceOf(OpenAILegacyChatProvider);
    expect(provider.modelName).toBe('qwen3.8-27b-fp8');
    expect(provider.modelParameters['baseUrl']).toBe('http://10.103.0.6:8003/v1');
    expect(model).toBe('qwen3.8-27b-fp8');
    expect(config.provider).toBe('aliyun');
  });

  it('非 OpenAI 兼容 provider 抛错', () => {
    const path = join(dir, '.env');
    writeFileSync(path, 'LLM_PROVIDER_0_PROVIDER=anthropic\nLLM_PROVIDER_0_API_KEY=k\n');
    expect(() => createProviderFromEnv(path)).toThrow(/not OpenAI-compatible/);
  });

  it('LLM_MAX_TOKENS 低于 16384 且未显式设 AGENT_MAX_TOKENS 时打 warning 并兜底', () => {
    const path = join(dir, '.env');
    writeFileSync(
      path,
      [
        'LLM_PROVIDER_0_PROVIDER=aliyun',
        'LLM_PROVIDER_0_API_KEY=test-key',
        'LLM_PROVIDER_0_MODEL=qwen3.8-27b-fp8',
        'LLM_PROVIDER_0_BASE_URL=http://10.103.0.6:8003/v1',
        'LLM_PROVIDER_0_MAX_TOKENS=8192',
      ].join('\n'),
    );
    delete process.env.AGENT_MAX_TOKENS;
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    try {
      createProviderFromEnv(path);
      expect(warnSpy).toHaveBeenCalledWith(
        expect.stringMatching(/LLM_MAX_TOKENS=8192 below agent floor 16384/),
      );
    } finally {
      warnSpy.mockRestore();
    }
  });
});

// ---------------------------------------------------------------------------
// SSE mock server + kosong generate()
// ---------------------------------------------------------------------------

interface CapturedRequest {
  body: Record<string, unknown>;
}

function sseChunk(delta: Record<string, unknown>, finishReason: string | null = null): unknown {
  return {
    id: 'chatcmpl-mock',
    object: 'chat.completion.chunk',
    created: 0,
    model: 'mock-model',
    choices: [{ index: 0, delta, finish_reason: finishReason }],
  };
}

const USAGE_CHUNK = {
  id: 'chatcmpl-mock',
  object: 'chat.completion.chunk',
  created: 0,
  model: 'mock-model',
  choices: [],
  usage: { prompt_tokens: 11, completion_tokens: 7, total_tokens: 18 },
};

describe('generate() over mock OpenAI SSE server', () => {
  let server: Server;
  let baseUrl: string;
  let captured: CapturedRequest[] = [];
  let responseChunks: unknown[] = [];

  beforeAll(async () => {
    server = createServer((req, res) => {
      let raw = '';
      req.on('data', (c: Buffer) => (raw += c.toString('utf8')));
      req.on('end', () => {
        captured.push({ body: JSON.parse(raw) as Record<string, unknown> });
        res.writeHead(200, { 'content-type': 'text/event-stream' });
        for (const chunk of responseChunks) {
          res.write(`data: ${JSON.stringify(chunk)}\n\n`);
        }
        res.write('data: [DONE]\n\n');
        res.end();
      });
    });
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    const address = server.address();
    if (address === null || typeof address === 'string') throw new Error('no server address');
    baseUrl = `http://127.0.0.1:${address.port}/v1`;
  });
  afterAll(async () => {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });
  beforeEach(() => {
    captured = [];
    responseChunks = [];
  });

  const makeProvider = (): OpenAILegacyChatProvider =>
    new OpenAILegacyChatProvider({ apiKey: 'test-key', baseUrl, model: 'mock-model' });

  it('组装分块到达的纯文本流', async () => {
    responseChunks = [
      sseChunk({ role: 'assistant', content: '你好' }),
      sseChunk({ content: '，世界' }),
      sseChunk({}, 'stop'),
      USAGE_CHUNK,
    ];
    const result = await generate(makeProvider(), '你是助手。', [], [createUserMessage('hi')]);
    expect(extractText(result.message)).toBe('你好，世界');
    expect(result.finishReason).toBe('completed');
    expect(result.rawFinishReason).toBe('stop');
    expect(result.usage).not.toBeNull();
    expect(result.message.toolCalls).toHaveLength(0);
    // 请求侧:system prompt + user 消息 + 流式
    const messages = captured[0]?.body['messages'] as unknown[];
    expect(captured[0]?.body['model']).toBe('mock-model');
    expect(captured[0]?.body['stream']).toBe(true);
    expect(messages[0]).toEqual({ role: 'system', content: '你是助手。' });
    expect(messages[1]).toEqual({ role: 'user', content: 'hi' });
  });

  it('组装 tool_calls 增量分块(含并行调用交错到达)', async () => {
    responseChunks = [
      // call 0 header
      sseChunk({
        tool_calls: [
          { index: 0, id: 'call_1', type: 'function', function: { name: 'get_weather', arguments: '' } },
        ],
      }),
      // call 0 args 第一片
      sseChunk({ tool_calls: [{ index: 0, function: { arguments: '{"city":"' } }] }),
      // call 1 header(与 call 0 交错)
      sseChunk({
        tool_calls: [
          { index: 1, id: 'call_2', type: 'function', function: { name: 'get_time', arguments: '' } },
        ],
      }),
      // call 0 args 第二片(在 call 1 header 之后到达,测试 index 路由)
      sseChunk({ tool_calls: [{ index: 0, function: { arguments: '北京"}' } }] }),
      // call 1 args
      sseChunk({ tool_calls: [{ index: 1, function: { arguments: '{"tz":"UTC"}' } }] }),
      sseChunk({}, 'tool_calls'),
      USAGE_CHUNK,
    ];
    const tools: Tool[] = [
      {
        name: 'get_weather',
        description: '查询天气',
        parameters: {
          type: 'object',
          properties: { city: { type: 'string' } },
          required: ['city'],
        },
      },
      {
        name: 'get_time',
        description: '查询时间',
        parameters: { type: 'object', properties: { tz: { type: 'string' } } },
      },
    ];
    const fired: ToolCall[] = [];
    const result = await generate(
      makeProvider(),
      '你是助手。',
      tools,
      [createUserMessage('北京天气和现在时间?')],
      { onToolCall: (tc) => fired.push(tc) },
    );
    expect(result.finishReason).toBe('tool_calls');
    expect(result.message.toolCalls).toHaveLength(2);
    const [weather, time] = result.message.toolCalls;
    expect(weather).toMatchObject({ id: 'call_1', name: 'get_weather' });
    expect(JSON.parse(weather?.arguments ?? 'null')).toEqual({ city: '北京' });
    expect(time).toMatchObject({ id: 'call_2', name: 'get_time' });
    expect(JSON.parse(time?.arguments ?? 'null')).toEqual({ tz: 'UTC' });
    // onToolCall 在流结束后按最终顺序逐个触发
    expect(fired.map((tc) => tc.name)).toEqual(['get_weather', 'get_time']);
    // 请求侧 tools 序列化
    const wireTools = captured[0]?.body['tools'] as Array<Record<string, unknown>>;
    expect(wireTools).toHaveLength(2);
    expect(wireTools[0]).toMatchObject({ type: 'function', function: { name: 'get_weather' } });
  });

  it('空流响应抛 APIEmptyResponseError', async () => {
    responseChunks = [sseChunk({}, 'stop'), USAGE_CHUNK];
    await expect(generate(makeProvider(), '', [], [createUserMessage('hi')])).rejects.toThrow(
      /empty response/i,
    );
  });

  it('AGENT_ENABLE_THINKING=0 时请求带 chat_template_kwargs.enable_thinking=false', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'llm-thinking-'));
    const envPath = join(dir, '.env');
    writeFileSync(
      envPath,
      [
        'LLM_PROVIDER_0_PROVIDER=vllm',
        'LLM_PROVIDER_0_API_KEY=EMPTY',
        'LLM_PROVIDER_0_MODEL=mock-model',
        `LLM_PROVIDER_0_BASE_URL=${baseUrl}`,
      ].join('\n'),
    );
    process.env.AGENT_ENABLE_THINKING = '0';
    try {
      const { provider } = createProviderFromEnv(envPath);
      responseChunks = [sseChunk({ content: 'ok' }, 'stop'), USAGE_CHUNK];
      await generate(provider, 'sys', [], [createUserMessage('hi')]);
      expect(captured[0]?.body['chat_template_kwargs']).toEqual({ enable_thinking: false });
    } finally {
      delete process.env.AGENT_ENABLE_THINKING;
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('AGENT_ENABLE_THINKING 缺省时 enable_thinking=true(默认开)', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'llm-thinking-'));
    const envPath = join(dir, '.env');
    writeFileSync(
      envPath,
      [
        'LLM_PROVIDER_0_PROVIDER=vllm',
        'LLM_PROVIDER_0_API_KEY=EMPTY',
        'LLM_PROVIDER_0_MODEL=mock-model',
        `LLM_PROVIDER_0_BASE_URL=${baseUrl}`,
      ].join('\n'),
    );
    delete process.env.AGENT_ENABLE_THINKING;
    try {
      const { provider } = createProviderFromEnv(envPath);
      responseChunks = [sseChunk({ content: 'ok' }, 'stop'), USAGE_CHUNK];
      await generate(provider, 'sys', [], [createUserMessage('hi')]);
      expect(captured[0]?.body['chat_template_kwargs']).toEqual({ enable_thinking: true });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('withThinkingDisabled 克隆的请求带 enable_thinking=false', async () => {
    responseChunks = [sseChunk({ content: 'ok' }, 'stop'), USAGE_CHUNK];
    await generate(withThinkingDisabled(makeProvider()), 'sys', [], [createUserMessage('hi')]);
    expect(captured[0]?.body['chat_template_kwargs']).toEqual({ enable_thinking: false });
  });

  it('compactMessagesWithSummary 的摘要请求关思考且 max_tokens=2048', async () => {
    const messages: Message[] = [
      createUserMessage('u1'),
      createAssistantMessage([{ type: 'text', text: '调用工具' }], [
        { type: 'function', id: 't1', name: 'echo', arguments: '{}' },
      ]),
      createToolMessage('t1', '工具结果'),
      createAssistantMessage([{ type: 'text', text: '看完了' }]),
      createUserMessage('u2'),
      createAssistantMessage([{ type: 'text', text: 'a2' }]),
    ];
    const config = createCompactionConfig(1_000_000, { maxRecentMessages: 2 });
    responseChunks = [sseChunk({ content: '摘要内容' }, 'stop'), USAGE_CHUNK];

    const outcome = await compactMessagesWithSummary(messages, config, makeProvider());

    expect(outcome.summarized).toBe(true);
    expect(captured[0]?.body['chat_template_kwargs']).toEqual({ enable_thinking: false });
    expect(captured[0]?.body['max_tokens']).toBe(2048);
  });
});
