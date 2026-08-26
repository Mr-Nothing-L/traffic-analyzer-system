/**
 * TurnPersister 单元测试:用真实 SessionManager + 临时 workspace 的 sqlite
 * 落盘(不发 HTTP),锁定 D5 收敛后的持久化不变量:
 *  - 条目/消息双水位只进不退,appended 增量 append、compacted 整体重写;
 *  - 压缩成果即时落盘(不等轮末):compacted 更新一到,磁盘就是压缩态;
 *  - finalize 幂等兜底;审批回执在落盘前回填 decision。
 */
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { createAssistantMessage, createUserMessage, type Message } from '../llm/kosong';

import { SessionManager } from './session';
import type { TimelineEntry } from './storage';
import { TurnPersister } from './turnPersister';

let workspace: string;
let sessions: SessionManager;
let sessionId: string;

beforeEach(() => {
  workspace = mkdtempSync(path.join(tmpdir(), 'turn-persister-test-'));
  sessions = new SessionManager();
  const restored = sessions.restoreWorkspace(workspace);
  expect(restored).toBe(0); // 只开库不建文件;create 会建
  const session = sessions.create({ workspaceDir: workspace, mode: 'yolo' });
  sessionId = session.id;
});

afterEach(() => {
  sessions.close();
  rmSync(workspace, { recursive: true, force: true });
});

function userEntry(text: string): TimelineEntry {
  return { kind: 'user', text, images: [], at: 1 };
}

function diskRows(table: 'entries' | 'messages'): Record<string, unknown>[] {
  const db = new DatabaseSync(path.join(workspace, '.agent', 'sessions.db'));
  try {
    const column = table === 'entries' ? 'entry_json' : 'message_json';
    return db
      .prepare(`SELECT ${column} FROM ${table} WHERE session_id = ? ORDER BY seq ASC`)
      .all(sessionId)
      .map((row) => JSON.parse(String(row[column])) as Record<string, unknown>);
  } finally {
    db.close();
  }
}

/** 直读磁盘 messages 表的 shadowed 列(验证压缩软遮蔽)。 */
function diskMessageRows(): { json: Record<string, unknown>; shadowed: number }[] {
  const db = new DatabaseSync(path.join(workspace, '.agent', 'sessions.db'));
  try {
    return db
      .prepare(
        'SELECT message_json, shadowed FROM messages WHERE session_id = ? ORDER BY seq ASC',
      )
      .all(sessionId)
      .map((row) => ({
        json: JSON.parse(String(row.message_json)) as Record<string, unknown>,
        shadowed: Number(row.shadowed),
      }));
  } finally {
    db.close();
  }
}

const msg = (role: Message['role'], text: string): Message => ({
  role,
  content: [{ type: 'text', text }],
  toolCalls: [],
});

describe('TurnPersister', () => {
  it('条目水位:push 后 flush 只落新增部分,重复 flush 不重复落盘', () => {
    const persister = new TurnPersister({ sessions, sessionId, baseMessageCount: 0 });
    persister.pushEntry(userEntry('u1'));
    persister.flushEntries();
    persister.pushEntry(userEntry('u2'));
    persister.pushEntry(userEntry('u3'));
    persister.flushEntries();
    persister.flushEntries(); // 幂等

    const texts = diskRows('entries').map((e) => e.text);
    expect(texts).toEqual(['u1', 'u2', 'u3']);
  });

  it('消息水位:appended 增量 append,一次一条与一次多条等价', () => {
    const persister = new TurnPersister({ sessions, sessionId, baseMessageCount: 0 });
    sessions.appendMessages(sessionId, [msg('user', '本轮输入')]);
    persister.markUserMessagePersisted();

    persister.onStepPersist({ kind: 'appended', messages: [createAssistantMessage([{ type: 'text', text: 'a' }], [])] });
    persister.onStepPersist({
      kind: 'appended',
      messages: [msg('tool', 't1'), msg('tool', 't2')],
    });

    const roles = diskRows('messages').map((m) => m.role);
    expect(roles).toEqual(['user', 'assistant', 'tool', 'tool']);
  });

  it('压缩即时回写:compacted 更新一到,磁盘立即整体重写(旧行软遮蔽),后续 appended 续接', () => {
    const persister = new TurnPersister({ sessions, sessionId, baseMessageCount: 0 });
    // 压缩前:user + 3 条历史消息已增量落盘
    const before: Message[] = [
      createUserMessage('u1'),
      msg('assistant', 'a1'),
      msg('tool', 't1'),
      msg('assistant', 'a2'),
    ];
    for (const m of before) persister.onStepPersist({ kind: 'appended', messages: [m] });
    expect(diskRows('messages')).toHaveLength(4);

    // 压缩发生:整体折叠为 2 条(摘要 + 保留区)
    const compacted: Message[] = [createUserMessage('[此前对话摘要]\n\n…'), createUserMessage('继续')];
    persister.onStepPersist({ kind: 'compacted', messages: compacted });

    // 不等轮末:磁盘立即是压缩态;旧行软遮蔽而非物理删除。
    const rows = diskMessageRows();
    expect(rows.filter((r) => r.shadowed === 0).map((r) => r.json.role)).toEqual(['user', 'user']);
    expect(rows.filter((r) => r.shadowed === 1)).toHaveLength(4);

    // 压缩后继续增量:水位已重置到压缩后长度,append 续接不重复。
    persister.onStepPersist({ kind: 'appended', messages: [msg('assistant', 'a3')] });
    const active = diskMessageRows().filter((r) => r.shadowed === 0);
    expect(active.map((r) => r.json.role)).toEqual(['user', 'user', 'assistant']);
  });

  it('finalize:兜底补齐剩余条目与消息,幂等不重复', () => {
    const persister = new TurnPersister({ sessions, sessionId, baseMessageCount: 0 });
    persister.pushEntry(userEntry('u1'));
    persister.pushEntry(userEntry('u2'));
    // 未经 onStepPersist 的消息(模拟异常路径)
    const finalMessages: Message[] = [createUserMessage('u'), msg('assistant', 'a')];
    persister.finalize(finalMessages);
    persister.finalize(finalMessages); // 幂等

    expect(diskRows('entries').map((e) => e.text)).toEqual(['u1', 'u2']);
    expect(diskRows('messages').map((m) => m.role)).toEqual(['user', 'assistant']);
  });

  it('finalize 只兜底条目:messages=undefined 时不追加任何消息', () => {
    const persister = new TurnPersister({ sessions, sessionId, baseMessageCount: 0 });
    sessions.appendMessages(sessionId, [msg('user', 'u')]);
    persister.markUserMessagePersisted();
    persister.pushEntry(userEntry('e1'));
    persister.finalize(undefined);

    expect(diskRows('entries').map((e) => e.text)).toEqual(['e1']);
    expect(diskRows('messages').map((m) => m.role)).toEqual(['user']);
  });

  it('settleApproval:落盘前回填 decision,倒序命中最近的 approval 条目', () => {
    const persister = new TurnPersister({ sessions, sessionId, baseMessageCount: 0 });
    persister.pushEntry({
      kind: 'approval',
      requestId: 'r1',
      toolName: 'write_file',
      approvalRule: 'write_file(/tmp/x)',
      at: 1,
    });
    persister.settleApproval('r1', 'approved');
    persister.pushEntry(userEntry('after'));
    persister.flushEntries();

    const approval = diskRows('entries').find((e) => e.kind === 'approval');
    expect(approval).toMatchObject({ requestId: 'r1', decision: 'approved' });
    // 已落盘后的回执不生效于磁盘(与旧行为一致:落盘后的 settle 只改内存)。
  });
});
