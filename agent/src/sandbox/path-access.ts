/**
 * Workspace path sandbox: canonicalize user-supplied paths, confine them to
 * the workspace, and block sensitive files (env / credentials / SSH keys).
 *
 * Ported from MoonshotAI/kimi-code (MIT) packages/agent-core-v2/src/tool/path-access.ts,
 * with `pathe` replaced by `node:path` (this runtime targets Linux/POSIX only).
 */
import path from 'node:path';
import os from 'node:os';

export interface WorkspaceConfig {
  readonly workspaceDir: string;
  readonly additionalDirs: readonly string[];
}

const SENSITIVE_BASENAMES = new Set<string>([
  '.env',
  'id_rsa',
  'id_ed25519',
  'id_ecdsa',
  'credentials',
]);

const SENSITIVE_PATH_SUFFIXES = [
  ['.aws', 'credentials'],
  ['.gcp', 'credentials'],
];

const ENV_PREFIX = '.env.';
const ENV_EXEMPTIONS = new Set<string>(['.env.example', '.env.sample', '.env.template']);

const SENSITIVE_BASENAME_PREFIXES = ['id_rsa', 'id_ed25519', 'id_ecdsa', 'credentials'];
const PUBLIC_KEY_BASENAMES = new Set<string>(['id_rsa.pub', 'id_ed25519.pub', 'id_ecdsa.pub']);
const SENSITIVE_DOT_VARIANT_SUFFIXES = new Set<string>([
  '.bak',
  '.pem',
  '.key',
  '.old',
]);

export function isSensitiveFile(filePath: string): boolean {
  const name = path.basename(filePath);
  const comparableName = name.toLowerCase();
  const comparablePath = filePath.toLowerCase();

  if (ENV_EXEMPTIONS.has(comparableName)) return false;
  if (PUBLIC_KEY_BASENAMES.has(comparableName)) return false;
  if (SENSITIVE_BASENAMES.has(comparableName)) return true;
  if (comparableName.startsWith(ENV_PREFIX)) return true;

  for (const prefix of SENSITIVE_BASENAME_PREFIXES) {
    if (comparableName.length > prefix.length && comparableName.startsWith(prefix)) {
      const suffix = comparableName.slice(prefix.length);
      const next = suffix[0];
      if (next === '-' || next === '_') return true;
      if (next === '.' && SENSITIVE_DOT_VARIANT_SUFFIXES.has(suffix)) return true;
    }
  }

  for (const suffixParts of SENSITIVE_PATH_SUFFIXES) {
    const suffix = suffixParts.join('/').toLowerCase();
    if (
      comparablePath.endsWith(`/${suffix}`) ||
      comparablePath.includes(`/${suffix}/`)
    ) {
      return true;
    }
  }

  return false;
}

export type PathSecurityCode = 'PATH_OUTSIDE_WORKSPACE' | 'PATH_SENSITIVE' | 'PATH_INVALID';
export type PathAccessOperation = 'read' | 'write' | 'search';
export type WorkspaceGuardMode = 'absolute-outside-allowed' | 'disabled';

export interface WorkspaceAccessPolicy {
  readonly guardMode: WorkspaceGuardMode;
  readonly checkSensitive: boolean;
}

export const DEFAULT_WORKSPACE_ACCESS_POLICY: WorkspaceAccessPolicy = {
  guardMode: 'absolute-outside-allowed',
  checkSensitive: true,
};

export interface PathAccess {
  readonly path: string;
  readonly outsideWorkspace: boolean;
}

export class PathSecurityError extends Error {
  readonly code: PathSecurityCode;
  readonly rawPath: string;
  readonly canonicalPath: string;

  constructor(code: PathSecurityCode, rawPath: string, canonicalPath: string, message: string) {
    super(message);
    this.name = 'PathSecurityError';
    this.code = code;
    this.rawPath = rawPath;
    this.canonicalPath = canonicalPath;
  }
}

function expandUserPath(filePath: string, homeDir: string | undefined): string {
  if (homeDir === undefined) return filePath;
  if (filePath === '~') return homeDir;
  if (filePath.startsWith('~/')) {
    return path.join(homeDir, filePath.slice(2));
  }
  return filePath;
}

export function canonicalizePath(filePath: string, cwd: string): string {
  if (filePath === '') {
    throw new PathSecurityError('PATH_INVALID', filePath, filePath, 'Path cannot be empty');
  }
  if (!path.isAbsolute(filePath) && !path.isAbsolute(cwd)) {
    throw new PathSecurityError(
      'PATH_INVALID',
      filePath,
      filePath,
      `Cannot resolve "${filePath}" against non-absolute cwd "${cwd}".`,
    );
  }
  const abs = path.isAbsolute(filePath) ? filePath : path.resolve(cwd, filePath);
  return path.normalize(abs);
}

export function isWithinDirectory(candidate: string, base: string): boolean {
  const nc = path.normalize(candidate);
  const nb = path.normalize(base);
  if (nc === nb) return true;
  const prefix = nb.endsWith('/') ? nb : nb + '/';
  return nc.startsWith(prefix);
}

export function isWithinWorkspace(candidate: string, config: WorkspaceConfig): boolean {
  if (isWithinDirectory(candidate, config.workspaceDir)) return true;
  for (const dir of config.additionalDirs) {
    if (isWithinDirectory(candidate, dir)) return true;
  }
  return false;
}

export interface ResolvePathAccessOptions {
  readonly operation: PathAccessOperation;
  readonly policy?: WorkspaceAccessPolicy;
  readonly homeDir?: string;
}

function relativeOutsideMessage(filePath: string, operation: PathAccessOperation): string {
  const verb =
    operation === 'write'
      ? 'write or edit a file'
      : operation === 'search'
        ? 'search'
        : 'read a file';
  return (
    `"${filePath}" is not an absolute path. ` +
    `You must provide an absolute path to ${verb} outside the working directory.`
  );
}

/**
 * Resolve `filePath` against `cwd` and enforce the workspace access policy.
 * Throws PathSecurityError on sensitive files (when policy.checkSensitive) and
 * on workspace escapes not permitted by policy.guardMode.
 */
export function resolvePathAccess(
  filePath: string,
  cwd: string,
  config: WorkspaceConfig,
  options: ResolvePathAccessOptions,
): PathAccess {
  const expandedPath = expandUserPath(filePath, options.homeDir ?? os.homedir());
  const rawIsAbsolute = path.isAbsolute(expandedPath);
  const canonical = canonicalizePath(expandedPath, cwd);
  const outsideWorkspace = !isWithinWorkspace(canonical, config);
  const policy = options.policy ?? DEFAULT_WORKSPACE_ACCESS_POLICY;

  if (policy.checkSensitive && isSensitiveFile(canonical)) {
    throw new PathSecurityError(
      'PATH_SENSITIVE',
      filePath,
      canonical,
      `"${filePath}" matches a sensitive-file pattern (env / credential / SSH key). ` +
        `Access is blocked to protect secrets.`,
    );
  }

  if (outsideWorkspace) {
    switch (policy.guardMode) {
      case 'absolute-outside-allowed':
        if (!rawIsAbsolute) {
          throw new PathSecurityError(
            'PATH_OUTSIDE_WORKSPACE',
            filePath,
            canonical,
            relativeOutsideMessage(filePath, options.operation),
          );
        }
        break;
      case 'disabled':
        break;
    }
  }

  return { path: canonical, outsideWorkspace };
}
