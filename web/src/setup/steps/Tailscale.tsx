import React, { useCallback, useEffect, useState } from "react";
import { Text, Box } from "ink";
import Spinner from "ink-spinner";
import SelectInput from "ink-select-input";
import { spawnSync } from "child_process";
import { StatusMessage } from "../components/StatusMessage.js";
import { saveState } from "../utils/state.js";
import type { StepProps } from "../types.js";

export function Tailscale({ state, setState, onNext }: StepProps): React.ReactElement {
  const [phase, setPhase] = useState<"prompt" | "running" | "done">("prompt");
  const [result, setResult] = useState<string | null>(null);

  const finish = useCallback((configured: boolean): void => {
    setState((prev) => {
      const next = {
        ...prev,
        tailscale_configured: configured,
        completed_step_id: "tailscale" as const,
      };
      saveState(next);
      return next;
    });
    setTimeout(onNext, 300);
  }, [onNext, setState]);

  const configureTailscale = (): void => {
    setPhase("running");
  };

  useEffect(() => {
    if (phase !== "running") return;

    try {
      const r = spawnSync(
        "tailscale",
        ["serve", "--bg", String(state.ports.http)],
        { encoding: "utf-8", timeout: 30000 },
      );
      if (r.status === 0) {
        // The running phase intentionally owns this synchronous command and its result state.
        // eslint-disable-next-line react-hooks/set-state-in-effect
        setResult("success");
        setPhase("done");
        finish(true);
        return;
      }
      setResult(`failed: ${(r.stderr || "").trim() || "command exited unsuccessfully"}`);
    } catch (error) {
      setResult(`failed: ${error instanceof Error ? error.message : "command could not run"}`);
    }
    setPhase("done");
  }, [finish, phase, state.ports.http]);

  if (phase === "prompt") {
    return (
      <Box flexDirection="column">
        <Text>{"  "}Tailscale detected on this machine.</Text>
        <Box marginTop={1}>
          <Text>Expose Gobby's web UI over Tailscale?</Text>
        </Box>
        <SelectInput
          items={[
            { label: "Yes, configure tailscale serve", value: "yes" },
            { label: "Skip", value: "skip" },
          ]}
          onSelect={(item) => {
            if (item.value === "skip") {
              setResult("skipped");
              setPhase("done");
              finish(false);
              return;
            }

            configureTailscale();
          }}
        />
      </Box>
    );
  }

  if (phase === "running") {
    return (
      <Text>
        <Spinner type="dots" /> Configuring Tailscale serve...
      </Text>
    );
  }

  return (
    <Box flexDirection="column">
      {result === "success" && (
        <StatusMessage level="success">
          Tailscale serve configured.
        </StatusMessage>
      )}
      {result === "skipped" && <Text dimColor>{"  "}Skipped.</Text>}
      {result && result.startsWith("failed") && (
        <Box flexDirection="column">
          <StatusMessage level="error">Tailscale setup {result}</StatusMessage>
          <SelectInput
            items={[
              { label: "Retry", value: "retry" },
              { label: "Continue without Tailscale", value: "skip" },
              { label: "Exit setup", value: "exit" },
            ]}
            onSelect={(item) => {
              if (item.value === "retry") {
                configureTailscale();
              } else if (item.value === "skip") {
                finish(false);
              } else {
                process.exit(1);
              }
            }}
          />
        </Box>
      )}
    </Box>
  );
}
