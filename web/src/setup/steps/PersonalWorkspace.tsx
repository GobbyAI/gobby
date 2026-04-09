import React, { useState, useEffect } from "react";
import { Text, Box } from "ink";
import SelectInput from "ink-select-input";
import Spinner from "ink-spinner";
import { mkdirSync, symlinkSync, existsSync, unlinkSync, lstatSync } from "fs";
import { join } from "path";
import { homedir } from "os";
import { runGobby } from "../utils/gobby.js";
import { getGobbyHome } from "../utils/state.js";
import { StatusMessage } from "../components/StatusMessage.js";
import { saveState } from "../utils/state.js";
import type { StepProps } from "../types.js";

export function PersonalWorkspace({ state: _state, setState, onNext }: StepProps): React.ReactElement {
  const [phase, setPhase] = useState<"init" | "shortcut" | "done">("init");
  const [initMsg, setInitMsg] = useState("");

  const personalDir = join(getGobbyHome(), "personal");

  const finish = (shortcutCreated: boolean): void => {
    setState((prev) => {
      const next = {
        ...prev,
        personal_dir_created: true,
        desktop_shortcut_created: shortcutCreated,
        completed_step_id: "personal" as const,
      };
      saveState(next);
      return next;
    });
    setTimeout(onNext, 300);
  };

  useEffect(() => {
    if (phase !== "init") return;
    // Create personal dir and init project
    mkdirSync(personalDir, { recursive: true });
    const r = runGobby(["init", "--name", "_personal"], {
      cwd: personalDir,
      timeout: 15000,
    });

    const msg = r.success
      ? `  Created: ${personalDir}`
      : `  Personal workspace: ${personalDir}`;
    setInitMsg(msg);
    setPhase("shortcut");
  }, [personalDir, phase]);

  if (phase === "init") {
    return (
      <Text>
        <Spinner type="dots" /> Creating personal workspace...
      </Text>
    );
  }

  if (phase === "shortcut") {
    return (
      <Box flexDirection="column">
        <Text>{initMsg}</Text>
        <Text>
          {"  "}Your personal workspace collects tasks and sessions not tied to
          a project.
        </Text>
        <Text dimColor>
          {"  "}Usage: gobby tasks create "my note" --project _personal
        </Text>
        <Box marginTop={1}>
          <Text>Create a shortcut on your Desktop?</Text>
        </Box>
        <SelectInput
          items={[
            { label: "Yes", value: "yes" },
            { label: "No", value: "no" },
          ]}
          onSelect={(item) => {
            if (item.value === "no") {
              setPhase("done");
              finish(false);
              return;
            }

            const desktop = join(homedir(), "Desktop");
            if (!existsSync(desktop)) {
              setPhase("done");
              finish(false);
              return;
            }

            const shortcut = join(desktop, "Gobby Personal");
            try {
              // Remove existing symlink/file
              if (existsSync(shortcut) || isSymlink(shortcut)) {
                unlinkSync(shortcut);
              }
              symlinkSync(personalDir, shortcut);
              setPhase("done");
              finish(true);
            } catch {
              setPhase("done");
              finish(false);
            }
          }}
        />
      </Box>
    );
  }

  return (
    <Box flexDirection="column">
      <Text>{initMsg}</Text>
      <StatusMessage level="success">Personal workspace ready.</StatusMessage>
    </Box>
  );
}

function isSymlink(p: string): boolean {
  try {
    return lstatSync(p).isSymbolicLink();
  } catch {
    return false;
  }
}
