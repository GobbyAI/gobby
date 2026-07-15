import React, { useState } from "react";
import { Text, Box } from "ink";
import TextInput from "ink-text-input";
import SelectInput from "ink-select-input";
import { patchPorts } from "../utils/config.js";
import { runGobby } from "../utils/gobby.js";
import { saveState } from "../utils/state.js";
import { StatusMessage } from "../components/StatusMessage.js";
import type { StepProps } from "../types.js";

type Phase = "show" | "editing" | "saving" | "error" | "done";
type EditField = "http" | "ws" | "ui";

const FIELD_ORDER: EditField[] = ["http", "ws", "ui"];
const FIELD_LABELS: Record<EditField, string> = {
  http: "HTTP API port",
  ws: "WebSocket port",
  ui: "Web UI port",
};

export function Configuration({ state, setState, onNext }: StepProps): React.ReactElement {
  const [phase, setPhase] = useState<Phase>("show");
  const [ports, setPorts] = useState(state.ports);
  const [editingIdx, setEditingIdx] = useState(0);
  const [editValue, setEditValue] = useState("");
  const [error, setError] = useState<string | null>(null);

  const commit = (finalPorts: typeof ports): void => {
    setError(null);
    setPhase("saving");

    // Keep the daemon on loopback unless the firewall was explicitly configured.
    patchPorts(
      finalPorts.http,
      finalPorts.ws,
      finalPorts.ui,
      state.firewall_configured,
    );

    // Run configuration and DB initialization without installing hooks or services.
    const result = runGobby(["install", "--config-only"], { timeout: 30000 });
    if (!result.success) {
      setError(result.output.trim() || "gobby install --config-only failed or timed out.");
      setPhase("error");
      return;
    }

    setState((prev) => {
      const next = {
        ...prev,
        ports: finalPorts,
        completed_step_id: "config" as const,
      };
      saveState(next);
      return next;
    });

    setPhase("done");
    setTimeout(onNext, 300);
  };

  if (phase === "show") {
    return (
      <Box flexDirection="column">
        <Text>  Default ports:</Text>
        <Text>    HTTP API:  {ports.http}</Text>
        <Text>    WebSocket: {ports.ws}</Text>
        <Text>    Web UI:    {ports.ui}</Text>
        <Box marginTop={1}>
          <Text>Customize ports?</Text>
        </Box>
        <SelectInput
          items={[
            { label: "No, use defaults", value: "defaults" },
            { label: "Yes, customize", value: "custom" },
          ]}
          onSelect={(item) => {
            if (item.value === "defaults") {
              commit(ports);
            } else {
              const field = FIELD_ORDER[0];
              setEditValue(String(ports[field]));
              setPhase("editing");
            }
          }}
        />
      </Box>
    );
  }

  if (phase === "editing") {
    const field = FIELD_ORDER[editingIdx];
    return (
      <Box flexDirection="column">
        <Text>{FIELD_LABELS[field]}:</Text>
        <Box>
          <Text>  {">"} </Text>
          <TextInput
            value={editValue}
            onChange={setEditValue}
            onSubmit={(val) => {
              const port = parseInt(val, 10);
              if (!isNaN(port) && port > 0 && port < 65536) {
                const newPorts = { ...ports, [field]: port };
                setPorts(newPorts);
                setError(null);

                if (editingIdx < FIELD_ORDER.length - 1) {
                  const nextIdx = editingIdx + 1;
                  setEditingIdx(nextIdx);
                  setEditValue(String(newPorts[FIELD_ORDER[nextIdx]]));
                } else if (new Set(Object.values(newPorts)).size !== FIELD_ORDER.length) {
                  setError("Ports must be unique.");
                  setEditingIdx(0);
                  setEditValue(String(newPorts[FIELD_ORDER[0]]));
                } else {
                  commit(newPorts);
                }
              }
            }}
          />
        </Box>
        <Text dimColor>  Current: {ports[field]}</Text>
        {error && <StatusMessage level="error">{error}</StatusMessage>}
      </Box>
    );
  }

  if (phase === "saving") {
    return <Text>  Saving configuration...</Text>;
  }

  if (phase === "error") {
    return (
      <Box flexDirection="column">
        <StatusMessage level="error">Configuration failed: {error}</StatusMessage>
        <SelectInput
          items={[
            { label: "Retry", value: "retry" },
            { label: "Exit setup", value: "exit" },
          ]}
          onSelect={(item) => {
            if (item.value === "retry") {
              commit(ports);
            } else {
              process.exit(1);
            }
          }}
        />
      </Box>
    );
  }

  return <StatusMessage level="success">Configuration saved.</StatusMessage>;
}
