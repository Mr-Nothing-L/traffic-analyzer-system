/**
 * repairTailMessages 单元测试 + SessionManager 懒恢复集成:磁盘上「assistant
 * 带 toolCalls 但工具结果未全部落盘」的半截轮次,恢复时合成 isError 工具
 * 消息补齐并回写磁盘,保证历史 provider-valid。不打模型 API。
 */
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { createToolMessage, createUserMessage, type Message, type ToolCall } from '../llm/kosong';

import { INTERRUPTED_TOOL_MESSAGE, repairTailMessages } from './repair';
import { SessionManager } from './session';
import { SessionStorage } from './storage';

function toolCall(id: string): ToolCall {
  return { type: 'function', id, name: 'echo', arguments: '{}' };
}

function assistantWithCalls(...ids: string[]): Message {
  return { role: 'assistant', content: [], toolCalls: ids.map(toolCall) };
}

function assistantText(text: string): Message {
  return { role: 'assistant', content: [{ type: 'text', text }], toolCalls: [] };
}

describe('repairTailMessages', () => {
  it('空历史 / 尾部是 user / 尾部 assistant 无 toolCalls:原样返回', () => {
    const empty: Message[] = [];
    expect(repairTailMessages(empty)).toBe(empty);

    const tailUser = [assistantWithCalls('c1'), createToolMessage('c1', 'ok'), createUserMessage('q')];
    expect(repairTailMessages(tailUser)).toBe(tailUser);

    const tailText = [createUserMessage('q'), assistantText('答')];
    expect(repairTailMessages(tailText)).toBe(tailText);
  });

  it('完整配对的尾部:原样返回', () => {
    const messages = [
      createUserMessage('q'),
      assistantWithCalls('c1', 'c2'),
      createToolMessage('c1', 'r1'),
      createToolMessage('c2', 'r2'),
    ];
    expect(repairTailMessages(messages)).toBe(messages);
  });

  it('末尾悬挂:assistant 带 toolCalls 但无任何 tool 消息 → 全部合成', () => {
    const messages = [createUserMessage('q'), assistantWithCalls('c1', 'c2')];
    const repaired = repairTailMessages(messages);
    expect(repaired).toHaveLength(4);
    expect(repaired[2]).toMatchObject({
      role: 'tool',
      toolCallId: 'c1',
      content: [{ type: 'text', text: INTERRUPTED_TOOL_MESSAGE }],
    });
    expect(repaired[3]).toMatchObject({ role: 'tool', toolCallId: 'c2' });
  });

  it('部分配对:只补缺失的 toolCall', () => {
    const messages = [
      createUserMessage('q'),
      assistantWithCalls('c1', 'c2'),
      createToolMessage('c1', 'r1'),
    ];
    const repaired = repairTailMessages(messages);
    expect(repaired).toHaveLength(4);
    expect(repaired[3]).toMatchObject({ role: 'tool', toolCallId: 'c2' });
  });

  it('只修尾部:悬挂在中间但历史已继续 → 不动(超出简化版范围)', () => {
    const messages = [
      assistantWithCalls('c1'), // c1 无配对,但后面跟了 user
      createUserMessage('q'),
      assistantText('答'),
    ];
    expect(repairTailMessages(messages)).toBe(messages);
  });
});

describe('SessionManager 懒恢复时的尾部修复', () => {
  let workspace: string;

  beforeEach(() => {
    workspace = mkdtempSync(path.join(tmpdir(), 'agent-repair-test-'));
  });

  afterEach(() => {
    rmSync(workspace, { recursive: true, force: true });
  });

  it('恢复悬挂 messages 时合成工具消息并回写磁盘', () => {
    // 直接造库:assistant 发起两个调用,只有一个工具结果落盘(模拟崩溃)。
    const storage = new SessionStorage(workspace);
    storage.insertSession({
      id: 's1',
      workspaceDir: workspace,
      mode: 'manual',
      title: '',
      createdAt: 1,
      lastActiveAt: 1,
    });
    storage.appendMessages('s1', [
      createUserMessage('q'),
      assistantWithCalls('c1', 'c2'),
      createToolMessage('c1', 'r1'),
    ]);
    storage.close();

    const manager = new SessionManager({ workspaces: [workspace] });
    try {
      const session = manager.get('s1');
      expect(session?.messages.map((m) => m.role)).toEqual([
        'user',
        'assistant',
        'tool',
        'tool',
      ]);
      const synthesized = session?.messages[3];
      expect(synthesized).toMatchObject({
        role: 'tool',
        toolCallId: 'c2',
        content: [{ type: 'text', text: INTERRUPTED_TOOL_MESSAGE }],
      });
    } finally {
      manager.close();
    }

    // 合成消息已回写磁盘:重开库直接读也是修复后的 4 条。
    const verify = new SessionStorage(workspace);
    expect(verify.loadMessages('s1').map((m) => m.role)).toEqual([
      'user',
      'assistant',
      'tool',
      'tool',
    ]);
    verify.close();
  });

  it('健康历史不受影响(不新增消息)', () => {
    const storage = new SessionStorage(workspace);
    storage.insertSession({
      id: 's2',
      workspaceDir: workspace,
      mode: 'manual',
      title: '',
      createdAt: 1,
      lastActiveAt: 1,
    });
    storage.appendMessages('s2', [
      createUserMessage('q'),
      assistantWithCalls('c1'),
      createToolMessage('c1', 'r1'),
      assistantText('答'),
    ]);
    storage.close();

    const manager = new SessionManager({ workspaces: [workspace] });
    try {
      expect(manager.get('s2')?.messages).toHaveLength(4);
    } finally {
      manager.close();
    }
    const verify = new SessionStorage(workspace);
    expect(verify.loadMessages('s2')).toHaveLength(4);
    verify.close();
  });
});
