import { execSync, spawnSync } from "child_process";

function gobbyBin(): string {
  return process.env.GOBBY_BIN || "gobby";
}

export function runGobby(
  args: string[],
  options?: { cwd?: string; timeout?: number; input?: string },
): { success: boolean; output: string } {
  try {
    const result = spawnSync(gobbyBin(), args, {
      encoding: "utf-8",
      timeout: options?.timeout || 60000,
      cwd: options?.cwd,
      input: options?.input,
      env: process.env,
    });
    return {
      success: result.status === 0,
      output: (result.stdout || "") + (result.stderr || ""),
    };
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : "Unknown error";
    return { success: false, output: msg };
  }
}

export function isGobbyInstalled(): boolean {
  try {
    execSync("which gobby", { encoding: "utf-8", timeout: 5000 });
    return true;
  } catch {
    return false;
  }
}

export async function checkHealth(
  port: number,
  timeout = 30000,
): Promise<boolean> {
  const start = Date.now();

  return new Promise<boolean>((resolve) => {
    const poll = async (): Promise<void> => {
      if (Date.now() - start > timeout) {
        resolve(false);
        return;
      }
      try {
        const res = await fetch(`http://localhost:${port}/admin/health`, {
          signal: AbortSignal.timeout(2000),
        });
        if (res.ok) {
          resolve(true);
          return;
        }
      } catch {
        /* keep polling */
      }
      setTimeout(poll, 500);
    };
    poll();
  });
}
