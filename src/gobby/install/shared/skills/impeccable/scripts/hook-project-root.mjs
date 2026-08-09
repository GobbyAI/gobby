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

  const start = resolve(resolveProjectCwd(event, fallback));
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
