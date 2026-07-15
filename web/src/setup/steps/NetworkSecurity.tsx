import React, { useCallback, useEffect, useState } from "react";
import { Text, Box } from "ink";
import SelectInput from "ink-select-input";
import Spinner from "ink-spinner";
import { spawnSync } from "child_process";
import { StatusMessage } from "../components/StatusMessage.js";
import { resolveFirewallScriptPath } from "../utils/firewall.js";
import { saveState } from "../utils/state.js";
import type { StepProps } from "../types.js";

export function NetworkSecurity({ state, setState, onNext }: StepProps): React.ReactElement {
  const [phase, setPhase] = useState<"prompt" | "running" | "done">("prompt");
  const [result, setResult] = useState<"success" | "failed" | "skipped" | null>(null);
  const [failureMessage, setFailureMessage] = useState<string | null>(null);

  const plat = process.platform;

  const finish = useCallback((firewallConfigured: boolean): void => {
    setState((prev) => {
      const next = {
        ...prev,
        firewall_configured: firewallConfigured,
        completed_step_id: "firewall" as const,
      };
      saveState(next);
      return next;
    });
    setTimeout(onNext, 300);
  }, [onNext, setState]);

  const configureFirewall = (): void => {
    setFailureMessage(null);
    setPhase("running");
  };

  useEffect(() => {
    if (phase !== "running") return;

    const scriptPath = resolveFirewallScriptPath();
    if (!scriptPath) {
      // The running phase intentionally owns this synchronous command and its result state.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setFailureMessage(
        "Firewall setup script was not found. Reinstall with npx @gobby/setup@latest and retry.",
      );
      setResult("failed");
      setPhase("done");
      return;
    }

    try {
      const { http, ws, ui } = state.ports;
      const r = spawnSync(
        "sudo",
        ["bash", scriptPath, String(http), String(ws), String(ui)],
        { stdio: "inherit", timeout: 60000 },
      );
      if (r.status === 0) {
        setResult("success");
        setPhase("done");
        finish(true);
        return;
      }
      setFailureMessage("Firewall setup command failed. Review the output above and retry.");
    } catch (error) {
      setFailureMessage(error instanceof Error ? error.message : "Firewall setup command failed.");
    }
    setResult("failed");
    setPhase("done");
  }, [finish, phase, state.ports]);

  if (plat === "darwin") {
    if (phase === "prompt") {
      return (
        <Box flexDirection="column">
          <Text>
            {"  "}macOS detected. Gobby can configure pf firewall rules to
            restrict
          </Text>
          <Text>{"  "}port access to localhost and Tailscale only.</Text>
          <Box marginTop={1}>
            <Text>Configure macOS firewall rules? (requires sudo)</Text>
          </Box>
          <SelectInput
            items={[
              { label: "Yes, configure firewall", value: "yes" },
              { label: "Skip", value: "skip" },
            ]}
            onSelect={(item) => {
              if (item.value === "skip") {
                setResult("skipped");
                setPhase("done");
                finish(false);
                return;
              }

              configureFirewall();
            }}
          />
        </Box>
      );
    }

    if (phase === "running") {
      return (
        <Text>
          <Spinner type="dots" /> Configuring firewall rules...
        </Text>
      );
    }

    return (
      <Box flexDirection="column">
        {result === "success" && (
          <StatusMessage level="success">
            Firewall rules configured.
          </StatusMessage>
        )}
        {result === "failed" && (
          <Box flexDirection="column">
            <StatusMessage level="error">
              {failureMessage ?? "Firewall setup failed."}
            </StatusMessage>
            <SelectInput
              items={[
                { label: "Retry", value: "retry" },
                { label: "Continue without firewall", value: "skip" },
                { label: "Exit setup", value: "exit" },
              ]}
              onSelect={(item) => {
                if (item.value === "retry") {
                  configureFirewall();
                } else if (item.value === "skip") {
                  finish(false);
                } else {
                  process.exit(1);
                }
              }}
            />
          </Box>
        )}
        {result === "skipped" && <Text dimColor>  Skipped.</Text>}
      </Box>
    );
  }

  if (plat === "linux") {
    return (
      <Box flexDirection="column">
        <Text>{"  "}Linux detected. Consider adding firewall rules:</Text>
        {[state.ports.http, state.ports.ws, state.ports.ui].map((p) => (
          <Text key={p} dimColor>
            {"    "}sudo ufw allow from 127.0.0.1 to any port {p}
          </Text>
        ))}
        <Box marginTop={1}>
          <SelectInput
            items={[{ label: "Continue", value: "next" }]}
            onSelect={() => finish(false)}
          />
        </Box>
      </Box>
    );
  }

  // Other platforms — skip
  return (
    <Box flexDirection="column">
      <Text dimColor>
        {"  "}Skipping firewall setup (platform not macOS or Linux).
      </Text>
      <SelectInput
        items={[{ label: "Continue", value: "next" }]}
        onSelect={() => finish(false)}
      />
    </Box>
  );
}
