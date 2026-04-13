import React, { useState } from "react";
import { Text, Box } from "ink";
import Spinner from "ink-spinner";
import TextInput from "ink-text-input";
import { runGobby } from "../utils/gobby.js";
import { StatusMessage } from "../components/StatusMessage.js";
import { saveState } from "../utils/state.js";
import type { StepProps } from "../types.js";

type Phase = "prompt" | "password" | "installing" | "done";

export function Services({ state: _state, setState, onNext }: StepProps): React.ReactElement {
  const [phase, setPhase] = useState<Phase>("prompt");
  const [customPassword, setCustomPassword] = useState("");
  const [result, setResult] = useState<{ success: boolean; message: string } | null>(null);

  const finish = (installed: boolean, passwordSet: boolean): void => {
    setState((prev) => {
      const next = {
        ...prev,
        neo4j_installed: installed,
        neo4j_password_set: passwordSet,
        completed_step_id: "services" as const,
      };
      saveState(next);
      return next;
    });
    setTimeout(onNext, 300);
  };

  const install = (password?: string): void => {
    setPhase("installing");

    const args = ["install", "--neo4j"];
    if (password) {
      args.push("--neo4j-password", password);
    }

    const r = runGobby(args, { timeout: 120000 });

    setResult({
      success: r.success,
      message: r.success
        ? "Neo4j installed successfully."
        : `Installation failed: ${r.output.trim().slice(0, 200)}`,
    });
    setPhase("done");
    finish(r.success, r.success && !!password);
  };

  if (phase === "prompt") {
    return (
      <Box flexDirection="column">
        <Text>{"  "}Install Neo4j knowledge graph? (requires Docker)</Text>
        <Text> </Text>
        <Text dimColor>
          {"  "}Neo4j enables relationship-based memory search across sessions.
        </Text>
        <Text> </Text>
        <Text>
          {"  "}
          <Text bold>[y]</Text> Yes, install{"  "}
          <Text bold>[n]</Text> No, skip{"  "}
          <Text bold>[p]</Text> Yes, with custom password
        </Text>
        <Box marginTop={1}>
          <Text dimColor>{"  "}</Text>
          <TextInput
            value=""
            onChange={() => {}}
            onSubmit={(val) => {
              const choice = val.trim().toLowerCase();
              if (choice === "y" || choice === "yes") {
                install();
              } else if (choice === "p" || choice === "password") {
                setPhase("password");
              } else {
                finish(false, false);
              }
            }}
          />
        </Box>
      </Box>
    );
  }

  if (phase === "password") {
    return (
      <Box flexDirection="column">
        <Text>{"  "}Enter Neo4j password (leave blank to auto-generate):</Text>
        <Box marginTop={1}>
          <Text dimColor>{"  "}</Text>
          <TextInput
            value={customPassword}
            onChange={setCustomPassword}
            mask="*"
            onSubmit={(val) => {
              install(val.trim() || undefined);
            }}
          />
        </Box>
      </Box>
    );
  }

  if (phase === "installing") {
    return (
      <Text>
        <Spinner type="dots" /> Installing Neo4j (pulling Docker image)...
      </Text>
    );
  }

  // done
  return (
    <Box flexDirection="column">
      {result?.success ? (
        <StatusMessage level="success">{result.message}</StatusMessage>
      ) : (
        <StatusMessage level="error">{result?.message ?? "Unknown error"}</StatusMessage>
      )}
    </Box>
  );
}
