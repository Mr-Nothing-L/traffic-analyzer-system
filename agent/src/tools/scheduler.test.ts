import { describe, expect, it } from 'vitest';

import { ToolAccesses, type ToolAccesses as ToolAccessesType } from './contract';
import { ToolScheduler, type ToolCallTask } from './scheduler';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

interface TrackedTask {
  task: ToolCallTask<string>;
  started: boolean;
  finish: () => void;
}

function trackedTask(name: string, accesses: ToolAccessesType, log: string[]): TrackedTask {
  const gate = deferred<void>();
  const state: TrackedTask = {
    started: false,
    finish: () => gate.resolve(),
    task: {
      accesses,
      start: () => {
        state.started = true;
        log.push(`start:${name}`);
        return Promise.resolve({
          result: gate.promise.then(() => {
            log.push(`end:${name}`);
            return name;
          }),
        });
      },
    },
  };
  return state;
}

describe('ToolAccesses.conflict', () => {
  it('write-write on the same path conflicts', () => {
    expect(
      ToolAccesses.conflict(ToolAccesses.writeFile('/a/f.txt'), ToolAccesses.writeFile('/a/f.txt')),
    ).toBe(true);
  });

  it('write-read on the same path conflicts', () => {
    expect(
      ToolAccesses.conflict(ToolAccesses.writeFile('/a/f.txt'), ToolAccesses.readFile('/a/f.txt')),
    ).toBe(true);
  });

  it('read-read on the same path does not conflict', () => {
    expect(
      ToolAccesses.conflict(ToolAccesses.readFile('/a/f.txt'), ToolAccesses.readFile('/a/f.txt')),
    ).toBe(false);
  });

  it('writes to different paths do not conflict', () => {
    expect(
      ToolAccesses.conflict(ToolAccesses.writeFile('/a/x'), ToolAccesses.writeFile('/a/y')),
    ).toBe(false);
  });

  it('recursive tree write conflicts with a read inside the tree', () => {
    expect(
      ToolAccesses.conflict(
        ToolAccesses.writeTree('/a/sub'),
        ToolAccesses.readFile('/a/sub/deep/f.txt'),
      ),
    ).toBe(true);
    expect(
      ToolAccesses.conflict(ToolAccesses.writeTree('/a/sub'), ToolAccesses.readFile('/a/other')),
    ).toBe(false);
  });

  it('kind all conflicts with everything', () => {
    expect(ToolAccesses.conflict(ToolAccesses.all(), ToolAccesses.readFile('/a'))).toBe(true);
  });
});

describe('ToolScheduler', () => {
  it('runs non-conflicting tasks in parallel', async () => {
    const scheduler = new ToolScheduler<string>();
    const log: string[] = [];
    const a = trackedTask('a', ToolAccesses.readFile('/x'), log);
    const b = trackedTask('b', ToolAccesses.writeFile('/y'), log);

    const pa = scheduler.add(a.task);
    const pb = scheduler.add(b.task);

    expect(a.started).toBe(true);
    expect(b.started).toBe(true);

    b.finish();
    a.finish();
    expect(await pa).toBe('a');
    expect(await pb).toBe('b');
  });

  it('queues a conflicting task until the predecessor finishes', async () => {
    const scheduler = new ToolScheduler<string>();
    const log: string[] = [];
    const writer = trackedTask('writer', ToolAccesses.writeFile('/f'), log);
    const reader = trackedTask('reader', ToolAccesses.readFile('/f'), log);

    const pWriter = scheduler.add(writer.task);
    const pReader = scheduler.add(reader.task);

    expect(writer.started).toBe(true);
    expect(reader.started).toBe(false);

    writer.finish();
    await pWriter;
    await flushMicrotasks();
    expect(reader.started).toBe(true);

    reader.finish();
    expect(await pReader).toBe('reader');
    expect(log).toEqual(['start:writer', 'end:writer', 'start:reader', 'end:reader']);
  });

  it('lets an unrelated task start while a conflicting one is queued', async () => {
    const scheduler = new ToolScheduler<string>();
    const log: string[] = [];
    const writer = trackedTask('writer', ToolAccesses.writeFile('/f'), log);
    const reader = trackedTask('reader', ToolAccesses.readFile('/f'), log);
    const other = trackedTask('other', ToolAccesses.writeFile('/g'), log);

    scheduler.add(writer.task);
    const pReader = scheduler.add(reader.task);
    const pOther = scheduler.add(other.task);

    expect(reader.started).toBe(false);
    expect(other.started).toBe(true);

    other.finish();
    await pOther;
    writer.finish();
    await flushMicrotasks();
    expect(reader.started).toBe(true);
    reader.finish();
    await pReader;
  });

  it('never runs a run_script (all) task in parallel with a write task', async () => {
    const scheduler = new ToolScheduler<string>();
    const log: string[] = [];
    const writer = trackedTask('writer', ToolAccesses.writeFile('/ws/other.txt'), log);
    const script = trackedTask('script', ToolAccesses.all(), log);

    const pWriter = scheduler.add(writer.task);
    const pScript = scheduler.add(script.task);

    expect(writer.started).toBe(true);
    expect(script.started).toBe(false);

    writer.finish();
    await pWriter;
    await flushMicrotasks();
    expect(script.started).toBe(true);

    script.finish();
    await pScript;
    expect(log).toEqual(['start:writer', 'end:writer', 'start:script', 'end:script']);
  });

  it('runBatch preserves result order even when execution order differs', async () => {
    const scheduler = new ToolScheduler<string>();
    const log: string[] = [];
    const first = trackedTask('first', ToolAccesses.writeFile('/f'), log);
    const second = trackedTask('second', ToolAccesses.writeFile('/f'), log);
    const third = trackedTask('third', ToolAccesses.readFile('/z'), log);

    const batch = scheduler.runBatch([first.task, second.task, third.task]);

    expect(first.started).toBe(true);
    expect(second.started).toBe(false);
    expect(third.started).toBe(true);

    third.finish();
    first.finish();
    await flushMicrotasks();
    expect(second.started).toBe(true);
    second.finish();

    await expect(batch).resolves.toEqual(['first', 'second', 'third']);
  });

  it('rejects the result promise when start() throws synchronously', async () => {
    const scheduler = new ToolScheduler<string>();
    const failure = new Error('boom');
    await expect(
      scheduler.add({
        accesses: ToolAccesses.none(),
        start: () => {
          throw failure;
        },
      }),
    ).rejects.toBe(failure);
  });
});

async function flushMicrotasks(): Promise<void> {
  for (let i = 0; i < 10; i++) {
    await Promise.resolve();
  }
}
