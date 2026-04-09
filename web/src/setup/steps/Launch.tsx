import React, { useState, useEffect } from "react";
import { Text, Box } from "ink";
import Spinner from "ink-spinner";
import { execSync } from "child_process";
import { writeFileSync } from "fs";
import { join } from "path";
import { homedir, hostname, platform, arch, release, userInfo } from "os";
import { runGobby, checkHealth } from "../utils/gobby.js";
import { getGobbyHome } from "../utils/state.js";
import { StatusMessage } from "../components/StatusMessage.js";
import { saveState } from "../utils/state.js";
import type { StepProps } from "../types.js";

export function Launch({ state, setState, onNext: _onNext }: StepProps): React.ReactElement {
  const [phase, setPhase] = useState<"starting" | "waiting" | "done">("starting");
  const [healthy, setHealthy] = useState(false);

  useEffect(() => {
    let cancelled = false;

    const run = async (): Promise<void> => {
      // Start daemon
      runGobby(["start"], { timeout: 15000 });
      if (cancelled) return;
      setPhase("waiting");

      // Wait for health
      const ok = await checkHealth(state.ports.http, 30000);
      if (cancelled) return;
      setHealthy(ok);

      // Write INITIAL_SETUP.md
      writeInitialSetupMd(state);

      // Mark complete
      setState((prev) => {
        const next = {
          ...prev,
          completed_at: new Date().toISOString(),
          completed_step_id: "launch" as const,
        };
        saveState(next);
        return next;
      });

      // Open browser
      const url = `http://localhost:${state.ports.ui}/?first_run=true`;
      try {
        if (process.platform === "darwin") {
          execSync(`open "${url}"`, { timeout: 5000 });
        } else {
          execSync(`xdg-open "${url}"`, { timeout: 5000 });
        }
      } catch {
        /* browser open is best-effort */
      }

      if (cancelled) return;
      setPhase("done");
    };

    run();

    return () => {
      cancelled = true;
    };
  }, []);

  if (phase === "starting") {
    return (
      <Text>
        <Spinner type="dots" /> Starting Gobby daemon...
      </Text>
    );
  }

  if (phase === "waiting") {
    return (
      <Text>
        <Spinner type="dots" /> Waiting for daemon health check...
      </Text>
    );
  }

  return (
    <Box flexDirection="column">
      {healthy ? (
        <StatusMessage level="success">Daemon is running.</StatusMessage>
      ) : (
        <Box flexDirection="column">
          <StatusMessage level="warning">
            Daemon started but health check did not pass.
          </StatusMessage>
          <Text dimColor>{"  "}Check logs: ~/.gobby/logs/</Text>
        </Box>
      )}
      <Text> </Text>
      <Box
        borderStyle="round"
        borderColor="green"
        paddingX={2}
        paddingY={1}
        flexDirection="column"
      >
        <Text bold color="green">
          Setup complete!
        </Text>
        <Text> </Text>
        <Text>
          {"  "}Daemon:{"  "}http://localhost:{state.ports.http}
        </Text>
        <Text>
          {"  "}Web UI:{"  "}http://localhost:{state.ports.ui}
        </Text>
        <Text> </Text>
        <Text>
          {"  "}Run <Text bold>gobby status</Text> to check anytime.
        </Text>
        <Text>
          {"  "}Run <Text bold>gobby stop</Text> to stop the daemon.
        </Text>
      </Box>
    </Box>
  );
}

function writeInitialSetupMd(state: typeof import("../utils/state.js").loadState extends () => infer R ? R : never): void {
  const now = new Date().toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC");
  const ports = state.ports;
  const versions = state.tool_versions;
  const detected = state.detected_tools;
  const installed = state.installed_clis;

  const allTools = [
    "python", "node", "uv", "claude", "tmux",
    "git", "docker", "tailscale", "gemini", "codex", "copilot", "windsurf", "cursor",
  ];
  const _required = ["python", "node", "uv", "claude", "tmux"];

  const cliRows = allTools
    .map((t) => {
      const ver = versions[t] || "not found";
      let hooks = "n/a";
      if (installed.includes(t)) hooks = "global";
      else if (["python", "node", "uv", "tmux", "docker"].includes(t)) hooks = "n/a";
      else if (!detected[t]) hooks = "---";
      return `| ${t} | ${ver} | ${hooks} |`;
    })
    .join("\n");

  const projectRows =
    state.projects.length > 0
      ? state.projects
          .map((p) => {
            const name = p.split("/").pop() || p;
            const display = p.startsWith(homedir())
              ? "~" + p.slice(homedir().length)
              : p;
            return `| ${name} | ${display} | --- |`;
          })
          .join("\n")
      : "| (none) | --- | --- |";

  const osDisplay =
    process.platform === "darwin"
      ? `macOS (Darwin ${release()})`
      : `${platform()} ${release()}`;
  const archDisplay =
    process.platform === "darwin" && arch() === "arm64"
      ? `${arch()} (Apple Silicon)`
      : arch();

  const md = `# Gobby Setup

Completed: ${now}

## User
- Name: ${state.user_name || "unknown"}
- System user: ${userInfo().username}
- Home: ${homedir()}
- Shell: ${process.env.SHELL || "unknown"}

## Machine
- Hostname: ${hostname()}
- OS: ${osDisplay}
- Architecture: ${archDisplay}
- Python: ${versions.python || "not found"}
- Node: ${versions.node || "not found"}
- uv: ${versions.uv || "not found"}

## Ports
- HTTP API: ${ports.http}
- WebSocket: ${ports.ws}
- Web UI: ${ports.ui}

## Network
- Firewall: ${state.firewall_configured ? "macOS pf rules installed" : "not configured"}
- Tailscale: ${state.tailscale_configured ? "configured" : "not configured"}
- Bind host: ${state.tailscale_configured ? "0.0.0.0" : "127.0.0.1"}

## Installed CLIs
| CLI | Version | Hooks Installed |
|-----|---------|-----------------|
${cliRows}

## Projects
| Name | Path | GitHub |
|------|------|--------|
${projectRows}

## Services
- Neo4j: ${state.neo4j_installed ? "installed (Docker)" : "not installed"}
- Neo4j password: ${state.neo4j_password_set ? "custom" : state.neo4j_installed ? "auto-generated" : "n/a"}

## Personal Workspace
- Path: ~/.gobby/personal/
- Desktop shortcut: ${state.desktop_shortcut_created ? "~/Desktop/Gobby Personal" : "not created"}

## Notes
- All data is stored locally on this machine
- Daemon auto-starts on \`gobby start\`, stops on \`gobby stop\`
- Web UI: http://localhost:${ports.ui}
`;

  const mdPath = join(getGobbyHome(), "INITIAL_SETUP.md");
  writeFileSync(mdPath, md);
}
