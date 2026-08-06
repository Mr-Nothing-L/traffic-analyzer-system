// useEvents 退订泄漏回归测试:
// 作用域销毁(组件卸载)自动退订 handler、重复挂载后事件只投递一次、
// 共享 EventSource 在 refCount 归零时才关闭
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { effectScope, type EffectScope } from 'vue';
import { createPinia, setActivePinia } from 'pinia';

type Cb = (ev: { data: string }) => void;

// EventSource 最小 mock:记录全部实例、按类型收集监听器、支持手动派发
class MockEventSource {
  static instances: MockEventSource[] = [];
  listeners = new Map<string, Set<Cb>>();
  closed = false;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    MockEventSource.instances.push(this);
  }
  addEventListener(type: string, cb: Cb) {
    let set = this.listeners.get(type);
    if (!set) {
      set = new Set();
      this.listeners.set(type, set);
    }
    set.add(cb);
  }
  close() {
    this.closed = true;
  }
  emit(type: string, data: unknown) {
    for (const cb of this.listeners.get(type) || []) cb({ data: JSON.stringify(data) });
  }
}

// 模块级 source/refCount/handlers 需逐用例重置,故动态 import
async function load() {
  vi.resetModules();
  return import('../useEvents');
}

// 组件 setup 运行于 effectScope 内,用它模拟一次「挂载」;stop 即「卸载」
function mount(fn: () => void): EffectScope {
  const scope = effectScope();
  scope.run(fn);
  return scope;
}

function lastSource(): MockEventSource {
  return MockEventSource.instances[MockEventSource.instances.length - 1];
}

beforeEach(() => {
  setActivePinia(createPinia());
  MockEventSource.instances = [];
  vi.stubGlobal('EventSource', MockEventSource);
});

describe('useEvents:退订', () => {
  it('作用域销毁后 handler 被移除,不再收到事件', async () => {
    const { useEvents } = await load();
    const spy = vi.fn();
    mount(() => useEvents().subscribe('job.done', spy)).stop();
    lastSource().emit('job.done', { id: 1 });
    expect(spy).not.toHaveBeenCalled();
  });

  it('重复挂载/卸载不叠加 handler:一条事件只投递给存活方一次', async () => {
    const { useEvents } = await load();
    const spyA = vi.fn();
    const spyB = vi.fn();
    mount(() => useEvents().subscribe('dashboard.changed', spyA)).stop(); // 第一次进入并离开
    mount(() => useEvents().subscribe('dashboard.changed', spyB)); // 第二次进入
    lastSource().emit('dashboard.changed', {});
    expect(spyA).not.toHaveBeenCalled(); // 旧闭包已退订(回归:修复前会再触发一次全量重拉)
    expect(spyB).toHaveBeenCalledTimes(1);
  });

  it('返回的退订函数仍可手动提前退订', async () => {
    const { useEvents } = await load();
    const spy = vi.fn();
    let unsubscribe: () => void;
    const scope = mount(() => {
      unsubscribe = useEvents().subscribe('presence', spy);
    });
    unsubscribe!();
    lastSource().emit('presence', []);
    expect(spy).not.toHaveBeenCalled();
    scope.stop();
  });
});

describe('useEvents:共享连接生命周期', () => {
  it('refCount 未归零不关连接,归零才关闭', async () => {
    const { useEvents } = await load();
    const scopeA = mount(() => useEvents().subscribe('job.progress', vi.fn()));
    const scopeB = mount(() => useEvents().subscribe('job.progress', vi.fn()));
    expect(MockEventSource.instances.length).toBe(1); // 共享一条连接
    scopeA.stop();
    expect(lastSource().closed).toBe(false); // 还有存活使用方
    scopeB.stop();
    expect(lastSource().closed).toBe(true); // 归零才关闭
  });

  it('全部销毁后再次订阅会新建连接', async () => {
    const { useEvents } = await load();
    mount(() => useEvents().subscribe('job.done', vi.fn())).stop();
    mount(() => useEvents().subscribe('job.done', vi.fn()));
    expect(MockEventSource.instances.length).toBe(2);
  });
});
