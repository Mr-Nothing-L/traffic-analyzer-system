/**
 * registerBuiltinTools: one-shot registration of the detection agent's eight
 * builtin tools (video_meta / extract_frames / draw_boxes / load_video /
 * read_file / write_file / run_script / submit_detection).
 * 历史:extract_frames 曾于 2026-08-25 短暂下线(d6cba0d),同日恢复并
 * 升级为 fps 采样(见 videoTools.ts 与 toolset.json)。
 *
 * Model-facing descriptions come from agent/config/toolset.json; the
 * submit_detection parameters are toolset.json's `$ref` to
 * submit_detection.schema.json, inlined here.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import type { WorkspaceConfig } from '../../sandbox/path-access';
import type { ExecutableTool } from '../contract';
import type { ToolRegistry } from '../registry';
import { createFileTools } from './fileTools';
import { ToolserverClient } from './httpToolserver';
import { createLoadVideoTool } from './loadVideo';
import { createSubmitDetectionTool, loadSubmitDetectionSchema } from './submitDetection';
import { createVideoTools } from './videoTools';

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
 * is replaced with the referenced file's contents (the only $ref form used in
 * toolset.json); anything else is returned as-is.
 */
export function expandToolsetParameters(
  entry: ToolsetEntry | undefined,
): Record<string, unknown> {
  const parameters = entry?.parameters;
  if (parameters === undefined) return { type: 'object', properties: {} };
  const ref = parameters['$ref'];
  if (typeof ref === 'string') {
    if (ref === './submit_detection.schema.json') {
      return loadSubmitDetectionSchema();
    }
    const refPath = ref.replace(/^\.\//, '');
    return JSON.parse(
      readFileSync(fileURLToPath(new URL(refPath, CONFIG_DIR_URL)), 'utf8'),
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
  const describe = (name: string): string =>
    entriesByName.get(name)?.description ?? name;

  const client = new ToolserverClient({ baseUrl: options.toolserverUrl });

  const tools: ExecutableTool[] = [
    // extract_frames 与 load_video 并存:extract_frames(fps=1 全片采样)是
    // 看画面的主方式,load_video 用于需要完整时序连贯理解的场景。
    ...createVideoTools(client, workspace, describe),
    createLoadVideoTool(client, workspace, describe('load_video')),
    ...createFileTools(workspace, describe),
    createSubmitDetectionTool(
      describe('submit_detection'),
      expandToolsetParameters(entriesByName.get('submit_detection')),
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
} from './videoTools';
export { createLoadVideoTool } from './loadVideo';
export { createFileTools, createReadFileTool, createRunScriptTool, createWriteFileTool } from './fileTools';
export { createSubmitDetectionTool, crossValidateDetection, loadSubmitDetectionSchema } from './submitDetection';
export {
  createSpawnSubagentTool,
  MAX_CONCURRENT_SUBAGENTS,
  SPAWN_SUBAGENT_TOOL_NAME,
  SUBAGENT_MAX_STEPS,
  SUBAGENT_TIMEOUT_MS,
  type SpawnSubagentDeps,
  type SpawnSubagentProviderHandle,
} from './spawnSubagent';
