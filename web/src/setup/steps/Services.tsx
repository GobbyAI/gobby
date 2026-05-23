import React, { useState } from "react";
import { Text, Box } from "ink";
import Spinner from "ink-spinner";
import TextInput from "ink-text-input";
import { runGobby } from "../utils/gobby.js";
import { StatusMessage } from "../components/StatusMessage.js";
import { saveState } from "../utils/state.js";
import type { StepProps } from "../types.js";

type Phase = "prompt" | "password" | "installing" | "done";

function validateFalkorPassword(value: string): string | null {
  if (!value) {
    return "FalkorDB password must not be empty";
  }
  if (/\s/.test(value)) {
    return "FalkorDB password must not contain whitespace";
  }
  if ([...value].some((ch) => ch.charCodeAt(0) < 0x20 || ch.charCodeAt(0) === 0x7f)) {
    return "FalkorDB password must not contain ASCII control characters";
  }
  if (/[^\x21-\x7E]/.test(value)) {
    return "FalkorDB password must use printable ASCII only (Docker round-trip constraint)";
  }
  return null;
}

function extractFalkorPasswordError(output: string): string | null {
  const valueErrorMatch = output.match(/ValueError:\s*(FalkorDB password[^\r\n]*)/);
  if (valueErrorMatch) {
    return valueErrorMatch[1];
  }

  const directMatch = output.match(/(FalkorDB password[^\r\n]*)/);
  return directMatch?.[1] ?? null;
}

export function Services({ state: _state, setState, onNext }: StepProps): React.ReactElement {
  const [phase, setPhase] = useState<Phase>("prompt");
  const [customPassword, setCustomPassword] = useState("");
  const [passwordError, setPasswordError] = useState<string | null>(null);
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
    if (password) {
      const validationError = validateFalkorPassword(password);
      if (validationError) {
        setPasswordError(validationError);
        setPhase("password");
        return;
      }
    }

    setPasswordError(null);
    setResult(null);
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

    if (password) {
      setPasswordError(
        extractFalkorPasswordError(r.output) ??
          `Installation failed: ${r.output.trim().slice(0, 200)}`,
      );
      setPhase("password");
      return;
    }

    setResult({
      success: false,
      message: `Installation failed: ${r.output.trim().slice(0, 200)}`,
    });
    setPhase("done");
    finish(false, false);
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
                setPasswordError(null);
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
        {passwordError ? <StatusMessage level="error">{passwordError}</StatusMessage> : null}
        <Box marginTop={1}>
          <Text dimColor>{"  "}</Text>
          <TextInput
            value={customPassword}
            onChange={(value) => {
              setCustomPassword(value);
              setPasswordError(null);
            }}
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
        <Spinner type="dots" /> Installing FalkorDB via Docker...
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
