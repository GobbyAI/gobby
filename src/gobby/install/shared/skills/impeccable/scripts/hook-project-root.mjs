import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';

import { resolveProjectCwd } from './hook-lib.mjs';

export function resolveHookProjectCwd(stdinJson, fallback = process.cwd()) {
  let event = null;
  try {
    event = JSON.parse(stdinJson);
  } catch {
    // Malformed input uses the process cwd and remains fail-open.
  }

  const safeFallback = typeof fallback === 'string' && fallback.trim() ? fallback : process.cwd();
  const safeEvent = event !== null && typeof event === 'object' && !Array.isArray(event)
    ? {
        cwd: typeof event.cwd === 'string' && event.cwd.trim() ? event.cwd : undefined,
        workspace_roots: Array.isArray(event.workspace_roots)
          ? event.workspace_roots.filter((root) => typeof root === 'string' && root.trim())
          : undefined,
      }
    : null;
  const candidate = resolveProjectCwd(safeEvent, safeFallback);
  const start = resolve(
    typeof candidate === 'string' && candidate.trim() ? candidate : safeFallback,
  );
  let current = start;
  while (true) {
    if (existsSync(join(current, '.gobby', 'project.json')) || existsSync(join(current, '.git'))) {
      return current;
    }
    const parent = dirname(current);
    if (parent === current) return start;
    current = parent;
  }
}
