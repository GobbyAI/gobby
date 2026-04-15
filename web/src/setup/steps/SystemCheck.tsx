import React, { useState, useEffect } from "react";
import { Text, Box } from "ink";
import SelectInput from "ink-select-input";
import Spinner from "ink-spinner";
import {
  detectAllTools,
  REQUIRED_TOOLS,
  OPTIONAL_TOOLS,
} from "../utils/detect.js";
import { ToolTable, type ToolRow } from "../components/ToolTable.js";
import { StatusMessage } from "../components/StatusMessage.js";
import { saveState } from "../utils/state.js";
import type { StepProps } from "../types.js";

const INSTALL_HINTS: Record<string, string> = {
  python: "brew install python@3.13 (macOS) or https://python.org",
  node: "https://nodejs.org or brew install node",
  uv: "curl -LsSf https://astral.sh/uv/install.sh | sh",
  claude: "npm install -g @anthropic-ai/claude-code",
  qwen: "npm install -g @qwen-code/qwen-code",
  tmux:
    process.platform === "darwin"
      ? "brew install tmux"
      : "sudo apt install tmux",
  clawhub: "npm install -g clawhub",
};

export function SystemCheck({
  state: _state,
  setState,
  onNext,
}: StepProps): React.ReactElement {
  const [scanning, setScanning] = useState(true);
  const [tools, setTools] = useState<ToolRow[]>([]);
  const [missingRequired, setMissingRequired] = useState<string[]>([]);

  useEffect(() => {
    const result = detectAllTools();

    const rows: ToolRow[] = [];
    for (const t of REQUIRED_TOOLS) {
      rows.push({
        name: t,
        found: result.detected[t] ?? false,
        version: result.versions[t] ?? null,
        required: true,
      });
    }
    for (const t of OPTIONAL_TOOLS) {
      rows.push({
        name: t,
        found: result.detected[t] ?? false,
        version: result.versions[t] ?? null,
        required: false,
      });
    }

    setTools(rows);
    setMissingRequired(REQUIRED_TOOLS.filter((t) => !result.detected[t]));

    setState((prev) => ({
      ...prev,
      detected_tools: result.detected,
      tool_versions: result.versions,
    }));

    setScanning(false);
  }, [setState]);

  if (scanning) {
    return (
      <Text>
        <Spinner type="dots" /> Scanning for installed tools...
      </Text>
    );
  }

  if (missingRequired.length > 0) {
    return (
      <Box flexDirection="column">
        <ToolTable tools={tools} />
        <Box marginTop={1} flexDirection="column">
          <StatusMessage level="warning">Missing required tools:</StatusMessage>
          {missingRequired.map((t) => (
            <Text key={t}>
              {"  "}
              {t}: <Text dimColor>{INSTALL_HINTS[t] || ""}</Text>
            </Text>
          ))}
        </Box>
        <Box marginTop={1} flexDirection="column">
          <Text>Continue anyway?</Text>
          <SelectInput
            items={[
              { label: "Yes, continue", value: "continue" },
              { label: "No, exit and install tools first", value: "exit" },
            ]}
            onSelect={(item) => {
              if (item.value === "exit") {
                process.exit(1);
              }
              setState((prev) => {
                const next = { ...prev, completed_step_id: "syscheck" };
                saveState(next);
                return next;
              });
              onNext();
            }}
          />
        </Box>
      </Box>
    );
  }

  // All required tools found — auto-advance after showing table
  return (
    <Box flexDirection="column">
      <ToolTable tools={tools} />
      <Box marginTop={1}>
        <StatusMessage level="success">All required tools found.</StatusMessage>
      </Box>
      <Box marginTop={1}>
        <SelectInput
          items={[{ label: "Continue", value: "next" }]}
          onSelect={() => {
            setState((prev) => {
              const next = { ...prev, completed_step_id: "syscheck" };
              saveState(next);
              return next;
            });
            onNext();
          }}
        />
      </Box>
    </Box>
  );
}
