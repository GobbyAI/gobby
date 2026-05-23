import React, { useState } from "react";
import { Text, Box } from "ink";
import Spinner from "ink-spinner";
import TextInput from "ink-text-input";
import { runGobby } from "../utils/gobby.js";
import { StatusMessage } from "../components/StatusMessage.js";
import { saveState } from "../utils/state.js";
import type { StepProps } from "../types.js";

type Phase = "prompt" | "password" | "installing" | "done";

function isValidFalkorPassword(password: string): boolean {
  return /^[\x21-\x7E]+$/.test(password);
}

export function Services({ state: _state, setState, onNext }: StepProps): React.ReactElement {
  const [phase, setPhase] = useState<Phase>("prompt");
  const [customPassword, setCustomPassword] = useState("");
  const [result, setResult] = useState<{ success: boolean; message: string } | null>(null);

  const finish = (installed: boolean, passwordSet: boolean): void => {
    setState((prev) => {
      const next = {
        ...prev,
        falkordb_installed: installed,
        falkordb_password_set: passwordSet,
        completed_step_id: "services" as const,
      };
      saveState(next);
      return next;
    });
    setTimeout(onNext, 300);
  };

  const install = (password?: string): void => {
    if (password && !isValidFalkorPassword(password)) {
      setResult({
        success: false,
        message: "FalkorDB password must use printable ASCII without spaces.",
      });
      setPhase("password");
      return;
    }

    setPhase("installing");

    const args = ["install", "--falkordb"];
    if (password) {
      args.push("--falkordb-password", password);
    }

    const r = runGobby(args, { timeout: 120000 });

    if (r.success) {
      setResult({ success: true, message: "FalkorDB installed successfully." });
      setPhase("done");
      finish(true, !!password);
      return;
    }

    setResult({
      success: false,
      message: `Installation failed: ${r.output.trim().slice(0, 200)}`,
    });
    setPhase(password ? "password" : "done");
    if (!password) {
      finish(false, false);
    }
  };

  if (phase === "prompt") {
    return (
      <Box flexDirection="column">
        <Text>{"  "}Install FalkorDB knowledge graph? (requires Docker)</Text>
        <Text> </Text>
        <Text dimColor>
          {"  "}FalkorDB enables relationship-based memory search across sessions.
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
        <Text>{"  "}Enter FalkorDB password (leave blank to auto-generate):</Text>
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
        <Spinner type="dots" /> Installing FalkorDB (pulling Docker image)...
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
