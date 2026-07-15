import React, { useState, useEffect, useCallback } from "react";
import { Text, Box } from "ink";
import SelectInput from "ink-select-input";
import Spinner from "ink-spinner";
import { basename } from "path";
import { findRepos, displayPath } from "../utils/repos.js";
import { runGobby } from "../utils/gobby.js";
import { MultiSelect } from "../components/MultiSelect.js";
import { StatusMessage } from "../components/StatusMessage.js";
import { saveState } from "../utils/state.js";
import type { StepProps } from "../types.js";

export function ProjectDiscovery({ state: _state, setState, onNext }: StepProps): React.ReactElement {
  const [scanning, setScanning] = useState(true);
  const [repos, setRepos] = useState<string[]>([]);
  const [phase, setPhase] = useState<"scan" | "select" | "init" | "done">("scan");
  const [initResults, setInitResults] = useState<string[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [initialized, setInitialized] = useState<string[]>([]);
  const [failed, setFailed] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;
    findRepos().then((found) => {
      if (cancelled) return;
      setRepos(found);
      setScanning(false);
      setPhase(found.length > 0 ? "select" : "done");
    });
    return () => { cancelled = true; };
  }, []);

  const finish = useCallback((projects: string[]): void => {
    setState((prev) => {
      const next = {
        ...prev,
        projects,
        completed_step_id: "projects" as const,
      };
      saveState(next);
      return next;
    });
    setTimeout(onNext, 300);
  }, [onNext, setState]);

  // Run init after render so the spinner is visible
  useEffect(() => {
    if (phase !== "init") return;
    const results: string[] = [];
    const succeeded: string[] = [];
    const failedRepos: string[] = [];
    for (const repoPath of selected) {
      const r = runGobby(["init"], { cwd: repoPath, timeout: 15000 });
      if (r.success) {
        results.push(`  Initialized: ${basename(repoPath)}`);
        succeeded.push(repoPath);
      } else {
        results.push(`  Failed: ${basename(repoPath)}: ${r.output.trim().slice(0, 80)}`);
        failedRepos.push(repoPath);
      }
    }
    const completed = [...new Set([...initialized, ...succeeded])];
    setInitResults(results);
    setInitialized(completed);
    setFailed(failedRepos);
    setPhase("done");
    if (failedRepos.length === 0) finish(completed);
  }, [finish, initialized, phase, selected]);

  if (scanning) {
    return (
      <Text>
        <Spinner type="dots" /> Scanning for git repositories...
      </Text>
    );
  }

  if (phase === "select" && repos.length > 0) {
    return (
      <Box flexDirection="column">
        <Text>
          {"  "}Found {repos.length} git repositor{repos.length === 1 ? "y" : "ies"}:
        </Text>
        <Text> </Text>
        <MultiSelect
          items={repos.map((r) => ({
            label: `${basename(r)}  ${displayPath(r)}`,
            value: r,
          }))}
          onSubmit={(sel) => {
            if (sel.length === 0) {
              finish([]);
              return;
            }
            setSelected(sel);
            setPhase("init");
          }}
        />
      </Box>
    );
  }

  if (phase === "init") {
    return (
      <Text>
        <Spinner type="dots" /> Initializing projects...
      </Text>
    );
  }

  if (repos.length === 0) {
    return (
      <Box flexDirection="column">
        <Text dimColor>{"  "}No uninitialized git repositories found.</Text>
        <SelectInput
          items={[{ label: "Continue", value: "next" }]}
          onSelect={() => finish([])}
        />
      </Box>
    );
  }

  return (
    <Box flexDirection="column">
      {initResults.map((line, i) => (
        <Text key={i}>{line}</Text>
      ))}
      {failed.length === 0 && initResults.length > 0 && (
        <Box marginTop={1}>
          <StatusMessage level="success">
            Projects initialized.
          </StatusMessage>
        </Box>
      )}
      {failed.length > 0 && (
        <Box flexDirection="column" marginTop={1}>
          <StatusMessage level="error">
            {failed.length} project initialization{failed.length === 1 ? "" : "s"} failed.
          </StatusMessage>
          <SelectInput
            items={[
              { label: "Retry failed projects", value: "retry" },
              { label: "Continue without failed projects", value: "skip" },
              { label: "Exit setup", value: "exit" },
            ]}
            onSelect={(item) => {
              if (item.value === "retry") {
                setSelected(failed);
                setPhase("init");
              } else if (item.value === "skip") {
                finish(initialized);
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
