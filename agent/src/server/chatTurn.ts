/** POST /chat 轮次执行(自 app.ts 拆出):SSE 生命周期、审批桥联动、
 * onEvent 事件累积与落盘、steer 队列消费。
 *
 * [文件说明]
 * 作用:handleChat 的完整轮次逻辑——请求校验与 busy 互斥、SSE 响应头
 * 与断连标记(res 'close' 不 abort 轮次)、TurnPersister 条目/消息双水位
 * 落盘、审批桥 emitter/settle 绑定、loop onEvent 的 text/think 累积与
 * tool 条目配对(tool_call_start 暂存 → tool_result 配对)、传输/落盘
 * 裁剪(video dataURL 占位、image dataURL → media 引用)、stop_turn 的
 * detection 事件合成、轮末兜底(剩余条目落盘 + 未消费 steer 丢弃 +
 * 挂起审批强制 settle)。传输裁剪 helper 与 parseImages 一并在此。
 * 上游:app.ts(createAgentServer 组装 deps 后转调)。
 * 下游:loop/agentLoop(runAgentLoop)、turnPersister、mediaStore、
 * routes(sendError/writeSseEvent)、session(SessionManager)。
 */
import type { ServerResponse } from 'node:http';

import type { ChatProvider, ContentPart, Message } from '../llm/kosong';
import { runAgentLoop, type AgentLoopEvent } from '../loop/agentLoop';
import type { DetectionPayload } from '../tools/builtin/submitDetection';
import type { PermissionGate } from '../permissions/gate';
import type { ApprovalBridge } from './approvalBridge';
import { mediaUrl, saveMediaFile } from './mediaStore';
import { sendError, writeSseEvent, isRecord } from './routes';
import type { SessionManager, Session } from './session';
import type { TimelineEntry } from './storage';
import { TurnPersister } from './turnPersister';
import type { ToolRegistry } from '../tools/registry';

export interface QueuedSteer {
  readonly text: string;
  readonly videoPath?: string;
  readonly images: string[];
  readonly message: Message;
}

export interface SessionRuntime {
  readonly registry: ToolRegistry;
  readonly gate: PermissionGate;
  readonly bridge: ApprovalBridge;
  /** 同 session 的 /chat 串行锁:true 时有进行中的轮次。 */
  busy: boolean;
  /** 进行中轮次的 AbortController(/cancel 触发;轮次结束清空)。 */
  controller: AbortController | null;
  /** steer 排队的 user 消息(注入后清空;轮次结束时未消费的丢弃并发
   *  steer_dropped 事件,不带入下一轮,避免插话注入在新用户消息之后)。 */
  readonly steerQueue: QueuedSteer[];
}

/** POST /chat 单轮图片附件上限。 */
const MAX_IMAGES_PER_TURN = 4;

/**
 * 工具结果传输/落盘裁剪:video part 的 dataURL 可达几十 MB(load_video),
 * SSE 与 sqlite 都承受不起,替换为占位文本;image part 的 dataURL 同样达
 * MB 级(extract_frames/track_suspects/draw_boxes),按内容寻址写盘
 * (<workspace>/.agent/media/<sha256>.<ext>)后替换为 media 引用 URL
 * (/sessions/{id}/media/{name},仍为 image_url part,前端经代理按需加载)。
 * 仅影响 SSE 事件与 entries 落盘,loop 内 messages 仍持原始内容(模型照常
 * 收到真实图片);写盘失败或非白名单图片类型时保留原 part(回退为旧行为)。
 */
function sanitizeToolOutputForTransport(
  output: string | ContentPart[],
  media: { sessionId: string; workspaceDir: string },
): string | ContentPart[] {
  if (!Array.isArray(output)) return output;
  return output.map((part): ContentPart => {
    if (part.type === 'video_url') {
      return { type: 'text', text: '[完整视频已发送给模型,不在此展示]' };
    }
    if (part.type === 'image_url') {
      const url = part.imageUrl.url;
      if (url.startsWith('data:image/')) {
        const name = saveMediaFile(media.workspaceDir, url);
        if (name !== undefined) {
          return { ...part, imageUrl: { ...part.imageUrl, url: mediaUrl(media.sessionId, name) } };
        }
      }
    }
    return part;
  });
}

/**
 * 结构化载荷(submit_detection 的 DetectionPayload)传输/落盘裁剪:逐事件
 * annotated_image(jpeg dataURL)按内容寻址写盘并替换为 media 引用 URL,
 * tool 条目的 payload、detection 事件与 detection 条目共用这一道转换。
 * 非 dataURL(已转换的 URL 引用/旧条目)与解析失败的原值原样保留。
 */
function sanitizeDetectionPayloadForTransport(
  payload: unknown,
  media: { sessionId: string; workspaceDir: string },
): unknown {
  if (!isRecord(payload) || !Array.isArray(payload['events'])) return payload;
  return {
    ...payload,
    events: (payload['events'] as unknown[]).map((event) => {
      if (!isRecord(event) || typeof event['annotated_image'] !== 'string') return event;
      const url = event['annotated_image'];
      if (!url.startsWith('data:image/')) return event;
      const name = saveMediaFile(media.workspaceDir, url);
      return name === undefined ? event : { ...event, annotated_image: mediaUrl(media.sessionId, name) };
    }),
  };
}

/** 解析可选 images 字段:接受 dataURL 或裸 base64,统一为 dataURL,最多 4 张。 */
export function parseImages(body: Record<string, unknown>): string[] {
  if (!Array.isArray(body.images)) return [];
  const images: string[] = [];
  for (const item of body.images) {
    if (typeof item !== 'string' || item === '') continue;
    if (images.length >= MAX_IMAGES_PER_TURN) break;
    images.push(item.startsWith('data:') ? item : `data:image/png;base64,${item}`);
  }
  return images;
}

export interface ChatTurnDeps {
  readonly sessions: SessionManager;
  readonly runtimeFor: (session: Session) => SessionRuntime;
  readonly providerFactory: () => { provider: ChatProvider };
  /** 渲染后的 system prompt(每轮注入 loop)。 */
  readonly systemPrompt: string;
  /** 上下文窗口 token 数(压缩触发阈值)。 */
  readonly contextTokens: number;
  /** 抽帧(多图)后的 generate 步思考档位(降档防烧穿 maxTokens)。 */
  readonly resolveFramesThinkingEffort: () =>
    ReturnType<typeof import('../llm/provider').resolveFramesThinkingEffort>;
}

export const runChatTurn = async (
  res: ServerResponse,
  body: unknown,
  deps: ChatTurnDeps,
): Promise<void> => {
  if (!isRecord(body) || typeof body.sessionId !== 'string') {
    sendError(res, 400, 'invalid_request', 'sessionId is required');
    return;
  }
  if (typeof body.input !== 'string' || body.input === '') {
    sendError(res, 400, 'invalid_request', 'input is required and must be a non-empty string');
    return;
  }
  const videoPath =
    typeof body.videoPath === 'string' && body.videoPath !== '' ? body.videoPath : undefined;
  const images = parseImages(body);
  const session = deps.sessions.get(body.sessionId);
  if (session === undefined) {
    sendError(res, 404, 'session_not_found', `unknown session: ${body.sessionId}`);
    return;
  }
  const runtime = deps.runtimeFor(session);
  if (runtime.busy) {
    sendError(res, 409, 'chat_in_progress', `session ${session.id} already has a chat turn in progress`);
    return;
  }
  runtime.busy = true;
  // 轮次级 AbortController:/cancel 显式终止用(P1 起断连不再 abort,
  // 停止语义由 POST /sessions/{id}/cancel 承担)。
  const controller = new AbortController();
  runtime.controller = controller;

  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
  });
  // writeHead 只排队不发送:不显式 flush,客户端要等第一个事件(可能
  // 是首个 token / 工具结果)才收到响应头,长首包时误判超时。
  res.flushHeaders();

  // 断连不杀轮次:只标记客户端断开,loop 继续跑完并落盘(maxSteps 兜底);
  // SSE 写出错同样只标记断开,不再抛出。断连后挂起的审批保留,用户可经
  // events/history 恢复后重新回执;仅在本轮仍进行中时处理(res 'close' 在
  // 正常结束时也会触发,此时 controller 已清空),防止误伤后续轮次的新审批。
  // 注:请求体在路由分发时已被 readJsonBody 读完,req 的 'close' 早已发出、
  // 此后监听不到;感知断连用 res 的 'close'(连接被提前断开或响应完成时触发)。
  let clientDisconnected = false;
  res.on('close', () => {
    if (runtime.controller !== controller) return;
    clientDisconnected = true;
    // 不再 cancelAll:显式 /cancel 与轮末兜底仍负责 settle。
  });

  // SSE 事件带 seq:发出时已落盘 entries 的水位(= 当前最大落盘 seq),
  // 前端刷新后以最后收到的 seq 调 GET /sessions/{id}/events?fromSeq=N 补齐。
  const emit = (event: unknown): void => {
    if (clientDisconnected) return;
    const payload = isRecord(event) ? { ...event, seq: session.entries.length } : event;
    try {
      writeSseEvent(res, payload);
    } catch {
      clientDisconnected = true;
    }
  };

  // ---- 轮次持久化:条目/消息双水位与压缩回写统一由 TurnPersister 持有 ----
  const persister = new TurnPersister({
    sessions: deps.sessions,
    sessionId: session.id,
    baseMessageCount: session.messages.length,
  });
  const userEntry: TimelineEntry = {
    kind: 'user',
    text: body.input,
    images,
    ...(videoPath !== undefined ? { videoPath } : {}),
    at: Date.now(),
  };
  persister.pushEntry(userEntry);

  let assistantText = '';
  let assistantThink = '';
  /** 当前步 generate 的壁钟耗时(generate_done 事件暂存),随 assistant 条目落盘。 */
  let pendingGenerateMs: number | undefined;
  const flushAssistant = (): void => {
    if (assistantText === '' && assistantThink === '') return;
    persister.pushEntry({
      kind: 'assistant',
      text: assistantText,
      think: assistantThink,
      ...(pendingGenerateMs !== undefined ? { generateMs: pendingGenerateMs } : {}),
      at: Date.now(),
    });
    assistantText = '';
    assistantThink = '';
    pendingGenerateMs = undefined;
  };
  /** tool_call_start 暂存,tool_result 到达时配对成一条 tool 条目。 */
  const pendingCalls = new Map<string, { name: string; arguments: string | null }>();

  runtime.bridge.bindEmitter((event) => {
    emit(event);
    persister.pushEntry({
      kind: 'approval',
      requestId: event.requestId,
      toolName: event.toolName,
      approvalRule: event.approvalRule,
      ...(event.description !== undefined ? { description: event.description } : {}),
      ...(event.accesses.length > 0 ? { accesses: event.accesses } : {}),
      ...(event.preview !== undefined ? { preview: event.preview } : {}),
      at: Date.now(),
    });
    // 审批条目立即落盘:断连恢复时 events/history 能重新投递未决审批。
    persister.flushEntries();
  });
  runtime.bridge.bindSettleHook((requestId, response) => {
    // 审批条目的 decision 回填:在落盘前的 step_done 批量落盘时一并序列化。
    persister.settleApproval(requestId, response.decision);
  });

  /** 轮末未消费的 steer:丢弃并经 SSE 告知「插话未生效」,不带入下一轮
   *  (否则会注入在下一轮新用户消息之后,模型看到时间倒置的指令)。 */
  const dropUnconsumedSteers = (): void => {
    if (runtime.steerQueue.length === 0) return;
    for (const item of runtime.steerQueue.splice(0, runtime.steerQueue.length)) {
      emit({
        type: 'steer_dropped',
        text: item.text,
        images: item.images,
        ...(item.videoPath !== undefined ? { videoPath: item.videoPath } : {}),
      });
    }
  };

  const userText =
    videoPath === undefined ? body.input : `视频路径:${videoPath}\n\n${body.input}`;
  const userContent: ContentPart[] = [{ type: 'text', text: userText }];
  for (const url of images) {
    userContent.push({ type: 'image_url', imageUrl: { url } });
  }
  const userMessage: Message = { role: 'user', content: userContent, toolCalls: [] };
  // loop 的初始历史必须在 appendMessages 推进 session.messages 之前快照,
  // 否则 user 消息会重复一份。
  const turnMessages: Message[] = [...session.messages, userMessage];
  // entry ↔ message 映射:记录本轮 user 消息写入前的 messages 长度,
  // recall 该 user 条目时按此值截断 messages(见 storage.ts messageIndex)。
  userEntry.messageIndex = session.messages.length;
  deps.sessions.appendMessages(session.id, [userMessage]);
  persister.markUserMessagePersisted();
  persister.flushEntries(); // user 条目立即落盘:崩溃也至少保留用户输入。

  const { provider } = deps.providerFactory();

  try {
    const result = await runAgentLoop({
      provider,
      systemPrompt: deps.systemPrompt,
      registry: runtime.registry,
      gate: runtime.gate,
      messages: turnMessages,
      signal: controller.signal,
      // steer:每个 step 的 generate 之前由 loop 逐条取回(取走即消费);
      // 每条立即构建 user 条目并落盘(TurnPersister),经活跃流发 SSE steer
      // 事件(客户端已断则 emit 为空操作,只落盘),消息体交给 loop 追加
      // 并即时落盘。
      nextSteer: () => {
        const item = runtime.steerQueue.shift();
        if (item === undefined) return null;
        // 与 /chat 的 user 条目同一映射:注入点即该消息在 messages 中的下标
        // (loop 逐条取走、取走即落盘,session.messages 已推进)。
        const entry: TimelineEntry = {
          kind: 'user',
          text: item.text,
          images: item.images,
          ...(item.videoPath !== undefined ? { videoPath: item.videoPath } : {}),
          messageIndex: session.messages.length,
          at: Date.now(),
        };
        persister.pushEntry(entry);
        persister.flushEntries(); // steer 条目立即落盘(seq 水位先于 SSE 事件推进)。
        emit({
          type: 'steer',
          text: item.text,
          images: item.images,
          ...(item.videoPath !== undefined ? { videoPath: item.videoPath } : {}),
        });
        return item.message;
      },
      compaction: { maxContextTokens: deps.contextTokens },
      // 抽帧(多图)后的 generate 步降思考档位,防逐帧分析烧穿 maxTokens。
      framesThinkingEffort: deps.resolveFramesThinkingEffort(),
      onStepPersist: (update) => {
        // loop 按消息确定点即时回灌(steer user / assistant / tool 增量,
        // 或压缩后的整体折叠),TurnPersister 统一决定 append 还是重写
        // (sqlite 同步写,在 onEvent 同一调用栈内完成,不引入异步积压)。
        persister.onStepPersist(update);
      },
      onEvent: (event: AgentLoopEvent) => {
        switch (event.type) {
          case 'text_delta':
            assistantText += event.text;
            break;
          case 'think_delta':
            assistantThink += event.text;
            break;
          case 'context_usage':
            deps.sessions.setLastKnownUsage(session.id, event.usedTokens);
            break;
          case 'tool_call_start':
            flushAssistant();
            pendingCalls.set(event.call.id, {
              name: event.call.name,
              arguments: event.call.arguments,
            });
            break;
          case 'tool_result': {
            const call = pendingCalls.get(event.toolCallId);
            pendingCalls.delete(event.toolCallId);
            // load_video 的 output 含整段视频 dataURL(可达 ~50MB):SSE 与
            // sqlite 都用裁剪版——video part 替换为占位文本;image part 的
            // dataURL 写盘为 media 引用 URL(见 sanitizeToolOutputForTransport)。
            // 模型侧 messages 不受影响(loop 内部仍持原始 output)。
            const media = { sessionId: session.id, workspaceDir: session.workspaceDir };
            const safeOutput = sanitizeToolOutputForTransport(event.result.output, media);
            // 成功结果的结构化附件(submit_detection 的检测载荷)进条目前
            // 同样把 annotated_image dataURL 转为 media 引用 URL。
            const resultPayload =
              'payload' in event.result
                ? sanitizeDetectionPayloadForTransport(event.result.payload, media)
                : undefined;
            persister.pushEntry({
              kind: 'tool',
              toolCallId: event.toolCallId,
              name: event.name,
              arguments: call?.arguments ?? null,
              output: safeOutput,
              isError: event.isError,
              ...(event.result.note !== undefined ? { note: event.result.note } : {}),
              ...(resultPayload !== undefined ? { payload: resultPayload } : {}),
              at: Date.now(),
            });
            emit({
              ...event,
              result: {
                ...event.result,
                output: safeOutput,
                ...(resultPayload !== undefined ? { payload: resultPayload } : {}),
              },
            });
            return;
          }
          case 'step_done':
            flushAssistant();
            persister.flushEntries();
            break;
          case 'generate_done':
            // 暂存本步 generate 耗时,随本条 assistant 条目落盘(flushAssistant);
            // 事件本身经末尾 emit 透传 SSE,供前端实时展示。
            pendingGenerateMs = event.generateMs;
            break;
          case 'done': {
            dropUnconsumedSteers();
            flushAssistant();
            // stop_turn 的结构化附件(submit_detection 的检测载荷)直接从
            // payload 读取,合成 detection SSE 事件并落盘——不再有
            // JSON.parse(note) 字符串编解码。
            const stopPayload =
              event.stopResult !== undefined && 'payload' in event.stopResult
                ? event.stopResult.payload
                : undefined;
            if (event.reason === 'stop_turn' && stopPayload !== undefined) {
              // annotated_image dataURL 先转 media 引用 URL 再进 SSE 与落盘。
              const safePayload = sanitizeDetectionPayloadForTransport(
                stopPayload,
                { sessionId: session.id, workspaceDir: session.workspaceDir },
              );
              emit({ type: 'detection', data: safePayload });
              persister.pushEntry({
                kind: 'detection',
                data: safePayload as DetectionPayload,
                at: Date.now(),
              });
            }
            emit({
              type: 'done',
              reason: event.reason,
              ...(event.error !== undefined ? { error: event.error } : {}),
              ...(event.truncated === true ? { truncated: true } : {}),
            });
            return;
          }
        }
        emit(event);
      },
    });
    // 轮末兜底:正常路径各消息已即时落盘,这里只补齐理论不可达的余量
    // (水位与返回值脱节的回归安全网)与剩余条目;幂等。
    persister.finalize(result.messages);
  } catch (error) {
    emit({
      type: 'done',
      reason: 'error',
      error: error instanceof Error ? error.message : String(error),
    });
  } finally {
    flushAssistant();
    persister.finalize(undefined); // 失败/中断的轮次也把剩余条目落盘,保证不丢。
    dropUnconsumedSteers(); // done 未经过 onEvent(异常路径)或竞态到达时兜底。
    // 轮末兜底:强制 settle 本轮残留的挂起审批,防止悬挂(settleHook 仍
    // 绑定,审批条目的 decision 一并落定为 cancelled)。
    runtime.bridge.cancelAll('轮次已结束');
    runtime.bridge.unbindSettleHook();
    runtime.bridge.unbindEmitter();
    runtime.controller = null;
    runtime.busy = false;
    try {
      res.end();
    } catch {
      // 客户端已断开:忽略写回错误。
    }
  }
};
