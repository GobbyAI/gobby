import React, { useEffect, useState } from "react";
import { Text, Box } from "ink";
import TextInput from "ink-text-input";
import SelectInput from "ink-select-input";
import { spawnSync } from "child_process";
import { StatusMessage } from "../components/StatusMessage.js";
import { saveState } from "../utils/state.js";
import type { StepProps } from "../types.js";

interface Integration {
  id: string;
  label: string;
  secretName: string;
  description: string;
  helpUrl: string;
}

const INTEGRATIONS: Integration[] = [
  {
    id: "github",
    label: "GitHub",
    secretName: "github_personal_access_token",
    description: "Enables GitHub MCP server (issues, PRs, repos, code search)",
    helpUrl: "https://github.com/settings/tokens",
  },
  {
    id: "linear",
    label: "Linear",
    secretName: "linear_api_key",
    description: "Enables Linear MCP server (issue tracking)",
    helpUrl: "https://linear.app/settings/api",
  },
];

type Phase = "menu" | "input" | "saving" | "done";
type PendingSecret = { id: string; label: string; name: string; value: string };

function gobbyBin(): string {
  return process.env.GOBBY_BIN || "gobby";
}

function setSecret(name: string, value: string): boolean {
  const result = spawnSync(gobbyBin(), ["secrets", "set", name, "--stdin", "--category", "mcp_server"], {
    encoding: "utf-8",
    input: value,
    timeout: 10000,
    env: process.env,
  });
  return result.status === 0;
}

export function Integrations({ state, setState, onNext }: StepProps): React.ReactElement {
  const [phase, setPhase] = useState<Phase>("menu");
  const [currentIdx, setCurrentIdx] = useState(0);
  const [inputValue, setInputValue] = useState("");
  const [configured, setConfigured] = useState<string[]>(state.secrets_configured || []);
  const [error, setError] = useState<string | null>(null);
  const [pendingSecret, setPendingSecret] = useState<PendingSecret | null>(null);

  const finish = (): void => {
    setState((prev) => {
      const next = {
        ...prev,
        secrets_configured: configured,
        completed_step_id: "integrations" as const,
      };
      saveState(next);
      return next;
    });
    setTimeout(onNext, 300);
  };

  const integration = INTEGRATIONS[currentIdx];

  useEffect(() => {
    if (phase !== "saving" || !pendingSecret) return;

    const ok = setSecret(pendingSecret.name, pendingSecret.value);
    if (ok) {
      // The saving phase intentionally owns this synchronous command and its result state.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setConfigured((prev) => [...prev, pendingSecret.id]);
      setError(null);
      setCurrentIdx((i) => i + 1);
      setPhase("menu");
    } else {
      setError(`Failed to store ${pendingSecret.label} secret.`);
      setPhase("input");
    }
  }, [pendingSecret, phase]);

  if (phase === "done") {
    const count = configured.length;
    return (
      <Box flexDirection="column">
        {count > 0 ? (
          <StatusMessage level="success">
            {count} integration{count > 1 ? "s" : ""} configured.
          </StatusMessage>
        ) : (
          <StatusMessage level="info">
            No integrations configured. You can add them later with: gobby secrets set
          </StatusMessage>
        )}
      </Box>
    );
  }

  if (phase === "menu") {
    if (currentIdx >= INTEGRATIONS.length) {
      setPhase("done");
      finish();
      return <Text>  Finishing...</Text>;
    }

    return (
      <Box flexDirection="column">
        <Text>
          {"  "}Configure <Text bold>{integration.label}</Text>?
        </Text>
        <Text dimColor>{"  "}{integration.description}</Text>
        <Text> </Text>
        <SelectInput
          items={[
            { label: `Yes, enter ${integration.label} API key`, value: "yes" },
            { label: "Skip", value: "skip" },
            { label: "Skip all remaining", value: "skip-all" },
          ]}
          onSelect={(item) => {
            if (item.value === "yes") {
              setInputValue("");
              setError(null);
              setPhase("input");
            } else if (item.value === "skip") {
              setCurrentIdx((i) => i + 1);
            } else {
              setPhase("done");
              finish();
            }
          }}
        />
      </Box>
    );
  }

  if (phase === "input") {
    return (
      <Box flexDirection="column">
        <Text>
          {"  "}Enter your <Text bold>{integration.label}</Text> API key:
        </Text>
        <Text dimColor>{"  "}Generate one at: {integration.helpUrl}</Text>
        <Box marginTop={1}>
          <Text>{"  > "}</Text>
          <TextInput
            value={inputValue}
            onChange={setInputValue}
            mask="*"
            onSubmit={(val) => {
              const trimmed = val.trim();
              if (!trimmed) {
                setError(null);
                setCurrentIdx((i) => i + 1);
                setPhase("menu");
                return;
              }
              setPendingSecret({
                id: integration.id,
                label: integration.label,
                name: integration.secretName,
                value: trimmed,
              });
              setPhase("saving");
            }}
          />
        </Box>
        {error && <StatusMessage level="error">{error}</StatusMessage>}
        <Text dimColor>{"  "}Press enter with empty value to skip</Text>
      </Box>
    );
  }

  // saving (brief flash)
  return <Text>  Saving {integration.label} key...</Text>;
}
