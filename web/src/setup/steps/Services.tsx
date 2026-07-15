import React, { useCallback, useEffect, useState } from "react";
import { Text, Box } from "ink";
import Spinner from "ink-spinner";
import SelectInput from "ink-select-input";
import TextInput from "ink-text-input";
import SelectInput from "ink-select-input";
import { runGobby } from "../utils/gobby.js";
import { StatusMessage } from "../components/StatusMessage.js";
import { saveState } from "../utils/state.js";
import type { StepProps } from "../types.js";

type Phase = "prompt" | "password" | "installing" | "done";

const INSTALL_TIMEOUT_MS = 120000;
const FAILURE_PREVIEW_LENGTH = 200;

type InstallResult = { success: boolean; message: string };

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

function buildInstallArgs(password?: string): string[] {
  const args = ["install", "--falkordb"];
  if (password) {
    args.push("--falkordb-password-stdin");
  }
  return args;
}

function formatInstallFailure(output: string): string {
  return `Installation failed: ${output.trim().slice(0, FAILURE_PREVIEW_LENGTH)}`;
}

export function Services({ state: _state, setState, onNext }: StepProps): React.ReactElement {
  const [phase, setPhase] = useState<Phase>("prompt");
  const [customPassword, setCustomPassword] = useState("");
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [result, setResult] = useState<InstallResult | null>(null);
  const [installPassword, setInstallPassword] = useState<string | undefined>();

  const finish = useCallback((installed: boolean, passwordSet: boolean): void => {
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
  }, [onNext, setState]);

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
    setInstallPassword(password);
    setPhase("installing");
  };

  useEffect(() => {
    if (phase !== "installing") return;

    const r = runGobby(buildInstallArgs(installPassword), {
      timeout: INSTALL_TIMEOUT_MS,
      input: installPassword,
    });

    if (r.success) {
      // The installing phase intentionally owns this synchronous command and its result state.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setResult({ success: true, message: "FalkorDB installed successfully." });
      setPhase("done");
      finish(true, !!installPassword);
      return;
    }

    if (installPassword) {
      setPasswordError(extractFalkorPasswordError(r.output) ?? formatInstallFailure(r.output));
      setPhase("password");
      return;
    }

    setResult({
      success: false,
      message: formatInstallFailure(r.output),
    });
    setPhase("done");
  }, [finish, installPassword, phase]);

  if (phase === "prompt") {
    return (
      <Box flexDirection="column">
        <Text>{"  "}Install FalkorDB knowledge graph? (requires Docker)</Text>
        <Text> </Text>
        <Text dimColor>
          {"  "}FalkorDB enables relationship-based memory search across sessions.
        </Text>
        <Text> </Text>
        <Box marginTop={1}>
          <SelectInput
            items={[
              { label: "Yes, install", value: "install" },
              { label: "No, skip", value: "skip" },
              { label: "Yes, with custom password", value: "password" },
            ]}
            onSelect={(item) => {
              if (item.value === "install") {
                install();
              } else if (item.value === "password") {
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
              install(val === "" ? undefined : val);
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
  if (!result?.success) {
    return (
      <Box flexDirection="column">
        <StatusMessage level="error">{result?.message ?? "Unknown error"}</StatusMessage>
        <SelectInput
          items={[
            { label: "Retry", value: "retry" },
            { label: "Continue without FalkorDB", value: "skip" },
            { label: "Exit setup", value: "exit" },
          ]}
          onSelect={(item) => {
            if (item.value === "retry") {
              install();
            } else if (item.value === "skip") {
              finish(false, false);
            } else {
              process.exit(1);
            }
          }}
        />
      </Box>
    );
  }

  return <StatusMessage level="success">{result.message}</StatusMessage>;
}
