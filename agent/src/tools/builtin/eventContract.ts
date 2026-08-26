/**
 * 事件契约(agent/config/event_contract.json)的加载与消费。
 *
 * 该 JSON 由 scripts/gen_agent_event_contract.py 从权威 YAML
 * (traffic_analyzer/config/event_categories.yaml + annotation_spec.yaml)生成,
 * 是 agent 侧事件事实的单一来源(ADR-0005):
 *   - submitDetection.ts 从它派生活跃事件枚举与编码位宽;
 *   - submit_detection 模型可见 schema 的 enum/pattern 由
 *     applyEventContractToSubmitSchema 注入;
 *   - chat_system.md 的 {{EVENT_DEFINITIONS}} / {{ADJUDICATION_RULES}} 等占位符
 *     由 renderSystemPrompt 渲染(手抄副本已删除)。
 *
 * 加载失败即抛错(fail-fast):契约缺失/损坏时 agent 拒绝启动,而不是拿着
 * 过期的事件定义静默运行。契约与 YAML 的同步由
 * traffic_analyzer/tests/test_agent_event_contract.py(--check)守护。
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

export interface EventContractEvent {
  readonly event_id: number;
  readonly event_code: string;
  readonly name: string;
  readonly name_zh: string;
  readonly description: string;
  readonly definition: string;
  /** 标注规范(annotation_spec.yaml)的逐事件边界条件。 */
  readonly boundary_conditions: readonly string[];
}

export interface EventContractAdjudicationRule {
  readonly rule_id: string;
  readonly name: string;
  readonly description: string;
  readonly priority: number;
}

export interface EventContract {
  readonly schema_version: number;
  readonly encoding_length: number;
  readonly normal_bit_index: number;
  readonly active_event_ids: readonly number[];
  readonly events: readonly EventContractEvent[];
  readonly adjudication_rules: readonly EventContractAdjudicationRule[];
  readonly global_guidelines: readonly string[];
}

const CONTRACT_URL = new URL('../../../config/event_contract.json', import.meta.url);

let cached: EventContract | undefined;

/** 加载并校验事件契约;失败抛错(fail-fast),结果进程内缓存。 */
export function loadEventContract(): EventContract {
  if (cached !== undefined) return cached;
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(fileURLToPath(CONTRACT_URL), 'utf8'));
  } catch (error) {
    throw new Error(
      `加载 agent/config/event_contract.json 失败(${error instanceof Error ? error.message : String(error)});` +
        '请运行 python3 scripts/gen_agent_event_contract.py 生成',
    );
  }
  cached = validateContract(raw);
  return cached;
}

function validateContract(raw: unknown): EventContract {
  if (typeof raw !== 'object' || raw === null) {
    throw new Error('event_contract.json 结构不合法:顶层不是对象');
  }
  const contract = raw as Record<string, unknown>;
  if (contract['schema_version'] !== 1) {
    throw new Error(`event_contract.json schema_version 不支持: ${String(contract['schema_version'])}`);
  }
  const ids = contract['active_event_ids'];
  if (!Array.isArray(ids) || ids.length === 0 || !ids.every((id) => Number.isInteger(id))) {
    throw new Error('event_contract.json active_event_ids 不合法(须为非空整数数组)');
  }
  if (new Set(ids).size !== ids.length) {
    throw new Error('event_contract.json active_event_ids 存在重复编号');
  }
  // ADR-0001:位 9 = 正常指示位,不对应任何事件类别。
  if (contract['normal_bit_index'] !== 9) {
    throw new Error(`event_contract.json normal_bit_index 必须为 9(ADR-0001),实际 ${String(contract['normal_bit_index'])}`);
  }
  const encodingLength = contract['encoding_length'];
  const maxId = Math.max(...(ids as number[]));
  if (!Number.isInteger(encodingLength) || (encodingLength as number) < maxId) {
    throw new Error(
      `event_contract.json encoding_length 不合法(须 >= 最大事件编号 ${maxId}),实际 ${String(encodingLength)}`,
    );
  }
  const events = contract['events'];
  if (!Array.isArray(events)) throw new Error('event_contract.json events 不合法');
  const byId = new Map<number, EventContractEvent>();
  for (const event of events) {
    if (typeof event !== 'object' || event === null) {
      throw new Error('event_contract.json events 条目不是对象');
    }
    const typed = event as Partial<EventContractEvent>;
    if (
      !Number.isInteger(typed.event_id) ||
      typeof typed.name_zh !== 'string' ||
      typeof typed.definition !== 'string' ||
      typeof typed.description !== 'string' ||
      !Array.isArray(typed.boundary_conditions)
    ) {
      throw new Error(`event_contract.json 事件 ${String(typed.event_id)} 字段不完整`);
    }
    byId.set(typed.event_id as number, {
      event_id: typed.event_id as number,
      event_code: typed.event_code ?? '',
      name: typed.name ?? '',
      name_zh: typed.name_zh as string,
      description: typed.description as string,
      definition: typed.definition as string,
      boundary_conditions: (typed.boundary_conditions as unknown[]).map(String),
    });
  }
  for (const id of ids as number[]) {
    if (!byId.has(id)) {
      throw new Error(`event_contract.json events 缺少活跃事件 ${id} 的定义`);
    }
  }
  const rules = contract['adjudication_rules'];
  if (!Array.isArray(rules)) throw new Error('event_contract.json adjudication_rules 不合法');
  const ruleIds = new Set<string>();
  for (const rule of rules) {
    if (typeof rule !== 'object' || rule === null || typeof rule['rule_id'] !== 'string') {
      throw new Error('event_contract.json adjudication_rules 条目不合法');
    }
    if (ruleIds.has(rule['rule_id'])) {
      throw new Error(`event_contract.json adjudication_rules rule_id 重复: ${rule['rule_id']}`);
    }
    ruleIds.add(rule['rule_id']);
  }
  const guidelines = contract['global_guidelines'];
  if (!Array.isArray(guidelines) || !guidelines.every((g) => typeof g === 'string')) {
    throw new Error('event_contract.json global_guidelines 不合法');
  }
  return {
    schema_version: contract['schema_version'] as number,
    encoding_length: encodingLength as number,
    normal_bit_index: contract['normal_bit_index'] as number,
    active_event_ids: ids as number[],
    events: [...byId.values()].sort((a, b) => a.event_id - b.event_id),
    adjudication_rules: rules as EventContractAdjudicationRule[],
    global_guidelines: guidelines as string[],
  };
}

/**
 * 二进制编码校验正则:^[01]_[01]_..._(共 encoding_length 位,下划线连接)。
 * submitDetection 的 zod 校验与 submit_detection.schema.json 的 pattern
 * 均由它派生,位宽只随 event_contract.json 变化。
 */
export function binaryEncodingPattern(encodingLength: number): RegExp {
  return new RegExp(`^[01]${'_[01]'.repeat(encodingLength - 1)}$`);
}

/** 编号列表压缩为可读区间,如 [1..8,10,11] → "1-8、10、11"。 */
export function formatEventIdList(ids: readonly number[]): string {
  const sorted = [...ids].sort((a, b) => a - b);
  const parts: string[] = [];
  let start = sorted[0] as number;
  let prev = start;
  for (let i = 1; i <= sorted.length; i += 1) {
    const current = sorted[i];
    if (current === prev + 1) {
      prev = current;
      continue;
    }
    parts.push(start === prev ? `${start}` : `${start}-${prev}`);
    start = current as number;
    prev = current as number;
  }
  return parts.join('、');
}

/**
 * 把事件契约注入 submit_detection 的模型可见 schema:event_id 的 enum、
 * binary_encoding 的 pattern 与涉及活跃事件编号/位宽的描述文字全部派生,
 * schema 文件里的静态值仅为文档参考(漂移由测试守护)。
 */
export function applyEventContractToSubmitSchema(
  schema: Record<string, unknown>,
): Record<string, unknown> {
  const contract = loadEventContract();
  const properties = schema['properties'];
  if (typeof properties !== 'object' || properties === null) {
    throw new Error('submit_detection.schema.json 缺少 properties');
  }
  const props = properties as Record<string, Record<string, unknown>>;
  const events = props['events'];
  const encoding = props['binary_encoding'];
  if (
    typeof events !== 'object' || events === null ||
    typeof encoding !== 'object' || encoding === null
  ) {
    throw new Error('submit_detection.schema.json 缺少 events / binary_encoding');
  }
  const eventItems = events['items'];
  if (typeof eventItems !== 'object' || eventItems === null) {
    throw new Error('submit_detection.schema.json events 缺少 items');
  }
  const eventId = (eventItems as Record<string, Record<string, unknown>>)['properties']?.['event_id'];
  if (typeof eventId !== 'object' || eventId === null) {
    throw new Error('submit_detection.schema.json events.items.properties.event_id 不合法');
  }
  const eventIdSchema = eventId as Record<string, unknown>;
  const idList = formatEventIdList(contract.active_event_ids);
  const count = contract.active_event_ids.length;
  eventIdSchema['enum'] = [...contract.active_event_ids];
  events['description'] =
    `逐事件判定。应覆盖全部 ${count} 个活跃事件编号(${idList}),未检出的事件也要给出 detected=false 与简短 reasoning。`;
  encoding['pattern'] = binaryEncodingPattern(contract.encoding_length).source;
  encoding['description'] = binaryEncodingDescription(contract.encoding_length);
  return schema;
}

function binaryEncodingDescription(encodingLength: number): string {
  const bits = (setter: (index: number) => string) =>
    Array.from({ length: encodingLength }, (_, index) => setter(index)).join('_');
  const normalCode = bits((index) => (index === 8 ? '1' : '0'));
  const example = bits((index) => (index === 0 || index === 2 || index === 9 ? '1' : '0'));
  return (
    `${encodingLength} 位二进制编码 {bit_1_..._bit_${encodingLength}},位序 = 事件编号 1..${encodingLength},` +
    '位与位之间用下划线连接。位 9 为正常指示位:已分析且无任何事件检出时编码为 ' +
    `{${normalCode}}(位 9=1);有事件检出时位 9=0、对应事件位=1。全零编码不是合法的正常编码。` +
    `示例:{${example}} 表示事件 1、3、10 检出。`
  );
}

/** chat_system.md 事件定义摘要段:逐事件「定义(YAML)+ 标注边界(annotation_spec)」。 */
export function renderEventDefinitionsSection(contract: EventContract): string {
  const indent = (text: string): string =>
    text
      .split('\n')
      .map((line, index) => (index === 0 || line === '' ? line : `   ${line}`))
      .join('\n');
  return contract.events
    .map((event) => {
      const head = `${event.event_id}. **${event.name_zh}**:${event.description}。`;
      const body = indent(event.definition);
      const boundary =
        event.boundary_conditions.length > 0
          ? `\n   标注边界:${event.boundary_conditions.join(';')}。`
          : '';
      return `${head}\n   ${body}${boundary}`;
    })
    .join('\n\n');
}

/** chat_system.md 裁决规则段:adjudication_rules(priority 降序)+ 标注总则。 */
export function renderAdjudicationSection(contract: EventContract): string {
  const ruleLines = contract.adjudication_rules.map(
    (rule) => `- **${rule.name}**(priority=${rule.priority}):${rule.description.replace(/\n+/g, ' ')}`,
  );
  const guidelineLines = contract.global_guidelines.map((line) => `- ${line}`);
  return [...ruleLines, ...guidelineLines].join('\n');
}

/** chat_system.md 中可用的占位符 → 渲染值。 */
const PROMPT_PLACEHOLDERS: Record<string, (contract: EventContract) => string> = {
  EVENT_DEFINITIONS: renderEventDefinitionsSection,
  ADJUDICATION_RULES: renderAdjudicationSection,
  ACTIVE_EVENT_COUNT: (contract) => String(contract.active_event_ids.length),
  ACTIVE_EVENT_ID_LIST: (contract) => formatEventIdList(contract.active_event_ids),
};

/**
 * 渲染系统 prompt 模板:替换全部事件契约占位符。缺少任一已知占位符或残留
 * 未知占位符都视为模板损坏,抛错(fail-fast)——占位符丢失意味着事件定义
 * 从模型可见 prompt 中静默消失。
 */
export function renderSystemPrompt(template: string): string {
  const contract = loadEventContract();
  let rendered = template;
  for (const [name, render] of Object.entries(PROMPT_PLACEHOLDERS)) {
    const token = `{{${name}}}`;
    if (!rendered.includes(token)) {
      throw new Error(`系统 prompt 缺少占位符 ${token}(事件契约注入点)`);
    }
    rendered = rendered.split(token).join(render(contract));
  }
  const leftover = /\{\{[A-Z_]+\}\}/.exec(rendered);
  if (leftover !== null) {
    throw new Error(`系统 prompt 存在未知占位符 ${leftover[0]}`);
  }
  return rendered;
}
