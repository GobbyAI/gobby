import { execSync } from "child_process";

const VERSION_CMDS: Record<string, string[]> = {
  uv: ["uv", "--version"],
  claude: ["claude", "--version"],
  tmux: ["tmux", "-V"],
  git: ["git", "--version"],
  docker: ["docker", "--version"],
  tailscale: ["tailscale", "version"],
  gemini: ["gemini", "--version"],
  codex: ["codex", "--version"],
  clawhub: ["clawhub", "--cli-version"],
};

export const REQUIRED_TOOLS = ["python", "node", "uv", "claude", "tmux"];
export const OPTIONAL_TOOLS = [
  "git",
  "docker",
  "tailscale",
  "gemini",
  "codex",
  "clawhub",
];

function which(cmd: string): string | null {
  try {
    return (
      execSync(`which ${cmd}`, { encoding: "utf-8", timeout: 5000 }).trim() ||
      null
    );
  } catch {
    return null;
  }
}

function parseVersion(output: string): string | null {
  for (const word of output.split(/\s+/)) {
    if (word && /^\d/.test(word)) {
      return word.replace(/[,).:]$/, "");
    }
  }
  return null;
}

export function detectTool(tool: string): string | null {
  // --- Python: need >= 3.13 ---
  if (tool === "python") {
    for (const cmd of ["python3", "python"]) {
      if (!which(cmd)) continue;
      try {
        const out = execSync(`${cmd} --version`, {
          encoding: "utf-8",
          timeout: 5000,
        }).trim();
        const ver = parseVersion(out);
        if (ver) {
          const [major, minor] = ver.split(".").map(Number);
          if (major > 3 || (major === 3 && minor >= 13)) return ver;
        }
      } catch {
        /* try next */
      }
    }
    return null;
  }

  // --- Node ---
  if (tool === "node") {
    if (!which("node")) return null;
    try {
      const out = execSync("node --version", {
        encoding: "utf-8",
        timeout: 5000,
      }).trim();
      return out.replace(/^v/, "");
    } catch {
      return null;
    }
  }

  // --- Standard CLI tools ---
  const cmdName = (VERSION_CMDS[tool] || [tool])[0];
  if (!which(cmdName)) return null;

  const cmd = VERSION_CMDS[tool] || [tool, "--version"];
  try {
    const out = execSync(cmd.join(" "), {
      encoding: "utf-8",
      timeout: 10000,
    })
      .trim()
      .replace(/\n/g, " ");
    return parseVersion(out) || out.slice(0, 40) || "installed";
  } catch {
    return null;
  }
}

export interface DetectedTools {
  detected: Record<string, boolean>;
  versions: Record<string, string>;
}

export function detectAllTools(): DetectedTools {
  const detected: Record<string, boolean> = {};
  const versions: Record<string, string> = {};

  for (const tool of [...REQUIRED_TOOLS, ...OPTIONAL_TOOLS]) {
    const ver = detectTool(tool);
    detected[tool] = ver !== null;
    if (ver) versions[tool] = ver;
  }

  return { detected, versions };
}
