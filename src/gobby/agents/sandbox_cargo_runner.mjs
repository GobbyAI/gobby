// Select a persistent target for the workspace Cargo actually builds, while
// retaining the sandbox's existing write grant and shared registry home.
import { createHash } from 'node:crypto';
import { dirname, join } from 'node:path';
import { existsSync, realpathSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

const [cargo, seatWorkspace, seatTarget, ...args] = process.argv.slice(2);
const cargoArgs = args.slice(0, args.indexOf('--') < 0 ? args.length : args.indexOf('--'));
const toolchain = cargoArgs[0]?.startsWith('+') ? [cargoArgs[0]] : [];
const globalArgs = [];
let manifest = [];
for (let i = 0; i < cargoArgs.length; i++) {
  if (cargoArgs[i] === '--manifest-path') manifest = ['--manifest-path', cargoArgs[++i]];
  else if (cargoArgs[i].startsWith('--manifest-path=')) {
    manifest = ['--manifest-path', cargoArgs[i].slice('--manifest-path='.length)];
  }
  else if (cargoArgs[i] === '-C' || cargoArgs[i] === '-Z') {
    globalArgs.push(cargoArgs[i], cargoArgs[++i]);
  }
  else if (cargoArgs[i].startsWith('-C') || cargoArgs[i].startsWith('-Z')) {
    globalArgs.push(cargoArgs[i]);
  }
}
// Let Cargo validate malformed arguments and explicit target selections itself.
const explicitTarget = cargoArgs.some(a => a === '--target-dir' || a.startsWith('--target-dir='));
const env = { ...process.env };
if (!explicitTarget && !manifest.includes(undefined) && !globalArgs.includes(undefined)) {
  const located = spawnSync(cargo, [
    ...toolchain, ...globalArgs, 'locate-project', '--workspace', '--message-format', 'plain', ...manifest,
  ], { encoding: 'utf8', env });
  if (located.status === 0) {
    const cargoWorkspace = realpathSync(dirname(located.stdout.trim()));
    // A repository's workspace can live below its checkout (e.g. crates/).
    // Git worktrees have a .git file; ordinary checkouts have a .git directory.
    let workspace = cargoWorkspace;
    for (let ancestor = cargoWorkspace; ; ancestor = dirname(ancestor)) {
      if (existsSync(join(ancestor, '.git'))) {
        workspace = ancestor;
        break;
      }
      if (dirname(ancestor) === ancestor) break;
    }
    env.CARGO_TARGET_DIR = workspace === seatWorkspace ? seatTarget : join(
      seatTarget, 'reviewed-checkouts', createHash('sha256').update(workspace).digest('hex'),
    );
  }
}
const result = spawnSync(cargo, args, { stdio: 'inherit', env });
if (result.error) {
  console.error(`gobby cargo launcher: ${result.error.message}`);
  process.exit(1);
}
if (result.signal) process.kill(process.pid, result.signal);
else process.exit(result.status ?? 1);
