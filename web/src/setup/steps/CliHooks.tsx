import React, { useState, useEffect, useCallback } from "react";
import { Text, Box } from "ink";
import Spinner from "ink-spinner";
import { runGobby } from "../utils/gobby.js";
import { MultiSelect } from "../components/MultiSelect.js";
import { saveState } from "../utils/state.js";
import type { StepProps } from "../types.js";

const CLI_LABELS: Record<string, string> = {
  claude: "Claude Code",
  gemini: "Gemini CLI",
  qwen: "Qwen CLI",
  codex: "Codex",
  droid: "Droid CLI",
};

const CLI_FLAGS: Record<string, string> = {
  claude: "--claude",
  gemini: "--gemini",
  qwen: "--qwen",
  codex: "--codex",
  droid: "--droid",
};

export function CliHooks({ state, setState, onNext }: StepProps): React.ReactElement {
  const [phase, setPhase] = useState<"select" | "installing" | "done">("select");
  const [results, setResults] = useState<string[]>([]);
  const [selected, setSelected] = useState<string[]>([]);

  const detected = state.detected_tools;
  const available = Object.keys(CLI_LABELS).filter((k) => detected[k]);

  const finish = useCallback((installed: string[]): void => {
    setState((prev) => {
      const next = {
        ...prev,
        installed_clis: installed,
        completed_step_id: "hooks" as const,
      };
      saveState(next);
      return next;
    });
    setTimeout(onNext, 300);
  }, [onNext, setState]);

  // Auto-advance when no CLIs detected
  useEffect(() => {
    if (available.length > 0 || phase !== "select") return;
    const timer = setTimeout(() => finish([]), 300);
    return () => clearTimeout(timer);
  }, [available.length, finish, phase]);

  // Run install after render so the spinner is visible
  useEffect(() => {
    if (phase !== "installing") return;

    const flags = selected.map((k) => CLI_FLAGS[k]).filter(Boolean);
    const r = runGobby(["install", ...flags], { timeout: 60000 });

    const lines: string[] = [];
    const installed: string[] = [];
    if (r.success) {
      for (const k of selected) {
        lines.push(`  Installed: ${CLI_LABELS[k]}`);
        installed.push(k);
      }
    } else {
      lines.push(`  Install output: ${r.output.trim().slice(0, 200)}`);
    }

    setResults(lines);
    setPhase("done");
    finish(installed);
  }, [finish, phase, selected]);

  if (available.length === 0) {
    if (phase === "select") {
      return (
        <Box flexDirection="column">
          <Text dimColor>{"  "}No AI coding CLIs detected. Skipping hook installation.</Text>
          <Box marginTop={1}>
            <Text dimColor>
              {"  "}Run gobby install later when you have CLIs installed.
            </Text>
          </Box>
        </Box>
      );
    }
    return <Text dimColor>{"  "}Skipped.</Text>;
  }

  if (phase === "select") {
    return (
      <Box flexDirection="column">
        <Text>{"  "}Detected CLIs — select which to install hooks for:</Text>
        <Text> </Text>
        <MultiSelect
          items={available.map((k) => ({
            label: CLI_LABELS[k],
            value: k,
          }))}
          onSubmit={(sel) => {
            if (sel.length === 0) {
              finish([]);
              return;
            }
            setSelected(sel);
            setPhase("installing");
          }}
        />
      </Box>
    );
  }

  if (phase === "installing") {
    return (
      <Text>
        <Spinner type="dots" /> Installing hooks...
      </Text>
    );
  }

  return (
    <Box flexDirection="column">
      {results.map((line, i) => (
        <Text key={i}>{line}</Text>
      ))}
    </Box>
  );
}
