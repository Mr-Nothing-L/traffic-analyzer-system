import { describe, expect, it } from 'vitest';

import {
  canonicalizePath,
  expandUserPath,
  isSensitiveFile,
  isWithinDirectory,
  isWithinWorkspace,
  PathSecurityError,
  resolvePathAccess,
  STRICT_WORKSPACE_ACCESS_POLICY,
  type WorkspaceConfig,
} from './path-access';

const WORKSPACE: WorkspaceConfig = {
  workspaceDir: '/ws/app',
  additionalDirs: ['/shared/data'],
};

describe('canonicalizePath', () => {
  it('resolves relative paths against cwd and normalizes dot segments', () => {
    expect(canonicalizePath('a/../b.txt', '/ws/app')).toBe('/ws/app/b.txt');
    expect(canonicalizePath('/abs//x/', '/ws/app')).toBe('/abs/x/');
  });

  it('rejects empty paths', () => {
    expect(() => canonicalizePath('', '/ws/app')).toThrowError(PathSecurityError);
    try {
      canonicalizePath('', '/ws/app');
    } catch (error) {
      expect((error as PathSecurityError).code).toBe('PATH_INVALID');
    }
  });

  it('rejects resolving against a non-absolute cwd', () => {
    expect(() => canonicalizePath('rel', 'not/absolute')).toThrowError(PathSecurityError);
  });
});

describe('isWithinDirectory / isWithinWorkspace', () => {
  it('matches the directory itself and its descendants, not siblings', () => {
    expect(isWithinDirectory('/ws/app', '/ws/app')).toBe(true);
    expect(isWithinDirectory('/ws/app/sub/f', '/ws/app')).toBe(true);
    expect(isWithinDirectory('/ws/app2/f', '/ws/app')).toBe(false);
    expect(isWithinDirectory('/ws', '/ws/app')).toBe(false);
  });

  it('honors additionalDirs', () => {
    expect(isWithinWorkspace('/ws/app/f', WORKSPACE)).toBe(true);
    expect(isWithinWorkspace('/shared/data/f', WORKSPACE)).toBe(true);
    expect(isWithinWorkspace('/etc/passwd', WORKSPACE)).toBe(false);
  });
});

describe('resolvePathAccess', () => {
  it('accepts paths inside the workspace', () => {
    const access = resolvePathAccess('sub/f.txt', '/ws/app', WORKSPACE, { operation: 'read' });
    expect(access).toEqual({ path: '/ws/app/sub/f.txt', outsideWorkspace: false });
  });

  it('throws PATH_OUTSIDE_WORKSPACE for relative paths escaping the workspace', () => {
    try {
      resolvePathAccess('../outside.txt', '/ws/app', WORKSPACE, { operation: 'write' });
      expect.unreachable();
    } catch (error) {
      expect(error).toBeInstanceOf(PathSecurityError);
      expect((error as PathSecurityError).code).toBe('PATH_OUTSIDE_WORKSPACE');
      expect((error as PathSecurityError).canonicalPath).toBe('/ws/outside.txt');
    }
  });

  it('allows absolute paths outside the workspace under absolute-outside-allowed', () => {
    const access = resolvePathAccess('/tmp/scratch.txt', '/ws/app', WORKSPACE, {
      operation: 'read',
    });
    expect(access).toEqual({ path: '/tmp/scratch.txt', outsideWorkspace: true });
  });

  it('guardMode disabled permits relative paths escaping the workspace', () => {
    const access = resolvePathAccess('../outside.txt', '/ws/app', WORKSPACE, {
      operation: 'write',
      policy: { guardMode: 'disabled', checkSensitive: true },
    });
    expect(access.outsideWorkspace).toBe(true);
  });

  it('strict guardMode rejects absolute paths outside the workspace', () => {
    try {
      resolvePathAccess('/tmp/scratch.txt', '/ws/app', WORKSPACE, {
        operation: 'read',
        policy: STRICT_WORKSPACE_ACCESS_POLICY,
      });
      expect.unreachable();
    } catch (error) {
      expect(error).toBeInstanceOf(PathSecurityError);
      expect((error as PathSecurityError).code).toBe('PATH_OUTSIDE_WORKSPACE');
      expect((error as PathSecurityError).canonicalPath).toBe('/tmp/scratch.txt');
    }
  });

  it('strict guardMode rejects relative paths escaping the workspace', () => {
    try {
      resolvePathAccess('../outside.txt', '/ws/app', WORKSPACE, {
        operation: 'write',
        policy: STRICT_WORKSPACE_ACCESS_POLICY,
      });
      expect.unreachable();
    } catch (error) {
      expect((error as PathSecurityError).code).toBe('PATH_OUTSIDE_WORKSPACE');
    }
  });

  it('strict guardMode accepts paths inside the workspace and additionalDirs', () => {
    expect(
      resolvePathAccess('sub/f.txt', '/ws/app', WORKSPACE, {
        operation: 'read',
        policy: STRICT_WORKSPACE_ACCESS_POLICY,
      }),
    ).toEqual({ path: '/ws/app/sub/f.txt', outsideWorkspace: false });
    expect(
      resolvePathAccess('/shared/data/f.bin', '/ws/app', WORKSPACE, {
        operation: 'read',
        policy: STRICT_WORKSPACE_ACCESS_POLICY,
      }).outsideWorkspace,
    ).toBe(false);
  });

  it('blocks sensitive files with PATH_SENSITIVE even inside the workspace', () => {
    try {
      resolvePathAccess('.env', '/ws/app', WORKSPACE, { operation: 'read' });
      expect.unreachable();
    } catch (error) {
      expect(error).toBeInstanceOf(PathSecurityError);
      expect((error as PathSecurityError).code).toBe('PATH_SENSITIVE');
    }
  });

  it('checkSensitive:false lets sensitive files through', () => {
    const access = resolvePathAccess('.env', '/ws/app', WORKSPACE, {
      operation: 'read',
      policy: { guardMode: 'absolute-outside-allowed', checkSensitive: false },
    });
    expect(access.path).toBe('/ws/app/.env');
  });

  it('expands ~ against the provided homeDir', () => {
    const access = resolvePathAccess('~/notes.txt', '/ws/app', WORKSPACE, {
      operation: 'read',
      homeDir: '/ws/app',
    });
    expect(access.path).toBe('/ws/app/notes.txt');
  });
});

describe('expandUserPath', () => {
  it('expands ~ and ~/ against homeDir, leaves other paths untouched', () => {
    expect(expandUserPath('~', '/home/u')).toBe('/home/u');
    expect(expandUserPath('~/notes', '/home/u')).toBe('/home/u/notes');
    expect(expandUserPath('/abs/x', '/home/u')).toBe('/abs/x');
    expect(expandUserPath('rel', undefined)).toBe('rel');
  });
});

describe('isSensitiveFile', () => {
  it('flags exact sensitive basenames', () => {
    for (const name of ['.env', 'id_rsa', 'id_ed25519', 'id_ecdsa', 'credentials']) {
      expect(isSensitiveFile(`/home/u/${name}`)).toBe(true);
    }
  });

  it('flags .env.* variants but exempts example/sample/template', () => {
    expect(isSensitiveFile('/app/.env.local')).toBe(true);
    expect(isSensitiveFile('/app/.env.production')).toBe(true);
    expect(isSensitiveFile('/app/.env.example')).toBe(false);
    expect(isSensitiveFile('/app/.env.sample')).toBe(false);
    expect(isSensitiveFile('/app/.env.template')).toBe(false);
  });

  it('flags private-key dot variants but exempts .pub', () => {
    expect(isSensitiveFile('/home/u/.ssh/id_rsa.bak')).toBe(true);
    expect(isSensitiveFile('/home/u/.ssh/id_ed25519.pem')).toBe(true);
    expect(isSensitiveFile('/home/u/.ssh/id_ecdsa.key')).toBe(true);
    expect(isSensitiveFile('/home/u/.ssh/id_rsa.old')).toBe(true);
    expect(isSensitiveFile('/home/u/.ssh/id_rsa.pub')).toBe(false);
    expect(isSensitiveFile('/home/u/.ssh/id_ed25519.pub')).toBe(false);
    expect(isSensitiveFile('/home/u/.ssh/id_ecdsa.pub')).toBe(false);
  });

  it('flags dash/underscore private-key variants', () => {
    expect(isSensitiveFile('/home/u/id_rsa-old')).toBe(true);
    expect(isSensitiveFile('/home/u/credentials_backup')).toBe(true);
  });

  it('flags cloud CLI credential files by path', () => {
    expect(isSensitiveFile('/home/u/.aws/credentials')).toBe(true);
    expect(isSensitiveFile('/home/u/.gcp/credentials')).toBe(true);
    expect(isSensitiveFile('/home/u/.aws/config')).toBe(false);
  });

  it('does not flag ordinary files', () => {
    expect(isSensitiveFile('/app/src/index.ts')).toBe(false);
    expect(isSensitiveFile('/app/environment.yml')).toBe(false);
  });
});
