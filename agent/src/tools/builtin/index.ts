/**
 * registerBuiltinTools: one-shot registration of the detection agent's eight
 * builtin tools (video_meta / extract_frames / draw_boxes / load_video /
 * read_file / write_file / run_script / submit_detection).
 * 历史:extract_frames 曾于 2026-08-25 短暂下线(d6cba0d),同日恢复并
 * 升级为 fps 采样(见 videoTools.ts 与 toolset.json)。
 *
 * 模型可见的 description 与 parameters 均来自 agent/config/toolset.json:
 * parameters(含 `{"$ref": "./xxx.json"}` 相对引用,从 agent/config/ 解析)
 * 原样作为工具的 JSON Schema 发给 provider;缺条目/缺 description 即抛错
 * (fail-fast),不再静默回退。submit_detection 的 $ref 目标
 * (submit_detection.schema.json)在加载后由 event_contract.json 注入活跃
 * 事件枚举与编码位宽。
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import type { WorkspaceConfig } from '../../sandbox/path-access';
import type { ExecutableTool } from '../contract';
import type { ToolRegistry } from '../registry';
import { applyEventContractToSubmitSchema } from './eventContract';
import { createFileTools } from './fileTools';
import { ToolserverClient } from './httpToolserver';
import { createLoadVideoTool } from './loadVideo';
import { createSubmitDetectionTool } from './submitDetection';
import { createVideoTools, type ToolsetEntrySpec } from './videoTools';

export interface BuiltinToolsOptions {
  readonly workspaceDir: string;
  /** Overrides TOOLSERVER_URL env var and the http://127.0.0.1:8601 default. */
  readonly toolserverUrl?: string | undefined;
  readonly additionalDirs?: readonly string[];
}

interface ToolsetEntry {
  readonly name: string;
  readonly description?: string;
  readonly parameters?: Record<string, unknown>;
}

interface ToolsetFile {
  readonly tools: readonly ToolsetEntry[];
}

const CONFIG_DIR_URL = new URL('../../../config/', import.meta.url);

function loadToolset(): ToolsetFile {
  return JSON.parse(
    readFileSync(fileURLToPath(new URL('toolset.json', CONFIG_DIR_URL)), 'utf8'),
  ) as ToolsetFile;
}

/**
 * Inline a toolset.json `parameters` entry: a `{"$ref": "./file.json"}` entry
 * is replaced with the referenced file's contents, resolved against
 * agent/config/(仅接受同目录的 ./xxx.json 引用);其他形式原样返回。
 */
export function expandToolsetParameters(
  entry: ToolsetEntry | undefined,
): Record<string, unknown> {
  const parameters = entry?.parameters;
  if (parameters === undefined) {
    throw new Error(`toolset.json 工具 ${entry?.name ?? '(未知)'} 缺少 parameters`);
  }
  const ref = parameters['$ref'];
  if (typeof ref === 'string') {
    if (!/^\.\/[A-Za-z0-9._-]+\.json$/.test(ref)) {
      throw new Error(
        `toolset.json 不支持的 $ref(仅接受 agent/config/ 内的 './xxx.json' 相对引用): ${ref}`,
      );
    }
    return JSON.parse(
      readFileSync(fileURLToPath(new URL(ref, CONFIG_DIR_URL)), 'utf8'),
    ) as Record<string, unknown>;
  }
  return parameters;
}

export function registerBuiltinTools(
  registry: ToolRegistry,
  options: BuiltinToolsOptions,
): ExecutableTool[] {
  const workspace: WorkspaceConfig = {
    workspaceDir: path.resolve(options.workspaceDir),
    additionalDirs: options.additionalDirs ?? [],
  };

  const toolset = loadToolset();
  const entriesByName = new Map(toolset.tools.map((entry) => [entry.name, entry]));
  // 缺条目/缺 description 即抛错:模型拿到裸名字当描述是静默降级。
  const specOf = (name: string): ToolsetEntrySpec => {
    const entry = entriesByName.get(name);
    if (entry === undefined) {
      throw new Error(`toolset.json 缺少工具条目: ${name}`);
    }
    if (typeof entry.description !== 'string' || entry.description === '') {
      throw new Error(`toolset.json 工具 ${name} 缺少 description`);
    }
    return { description: entry.description, parameters: expandToolsetParameters(entry) };
  };

  const client = new ToolserverClient({ baseUrl: options.toolserverUrl });

  const submitSpec = specOf('submit_detection');
  const tools: ExecutableTool[] = [
    // extract_frames 与 load_video 并存:extract_frames(fps=1 全片采样)是
    // 看画面的主方式,load_video 用于需要完整时序连贯理解的场景。
    ...createVideoTools(client, workspace, specOf),
    createLoadVideoTool(client, workspace, specOf('load_video')),
    ...createFileTools(workspace, specOf),
    createSubmitDetectionTool(
      submitSpec.description,
      // 事件枚举/编码位宽从 event_contract.json 注入(单一权威源派生)。
      applyEventContractToSubmitSchema(submitSpec.parameters),
      // 传入 client 与 workspace:video_path 在 resolve 阶段做沙盒读校验,
      // accesses 进权限链,与其他视频工具一致。
      { client, workspace },
    ),
  ];
  for (const tool of tools) {
    registry.register(tool);
  }
  return tools;
}

export { ToolserverClient } from './httpToolserver';
export type { ToolserverErrorInfo, ToolserverResult } from './httpToolserver';
export {
  createDrawBoxesTool,
  createExtractFramesTool,
  createVideoMetaTool,
  createVideoTools,
  type ToolsetEntrySpec,
  type ToolsetLookup,
} from './videoTools';
export { createLoadVideoTool } from './loadVideo';
export { createFileTools, createReadFileTool, createRunScriptTool, createWriteFileTool } from './fileTools';
export { createSubmitDetectionTool, crossValidateDetection, loadSubmitDetectionSchema } from './submitDetection';
export { loadEventContract, renderSystemPrompt } from './eventContract';
export {
  createSpawnSubagentTool,
  MAX_CONCURRENT_SUBAGENTS,
  SPAWN_SUBAGENT_TOOL_NAME,
  SUBAGENT_MAX_STEPS,
  SUBAGENT_TIMEOUT_MS,
  type SpawnSubagentDeps,
  type SpawnSubagentProviderHandle,
} from './spawnSubagent';
