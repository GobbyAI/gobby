import React, { useState } from "react";
import { Text, Box } from "ink";
import SelectInput from "ink-select-input";
import { Banner } from "../components/Banner.js";
import { saveState } from "../utils/state.js";
import type { StepProps } from "../types.js";

// Read version from package.json at build time, fallback to env
const VERSION = process.env.GOBBY_VERSION || "0.2.20";

export function Welcome({ state, setState, onNext }: StepProps): React.ReactElement {
  const hasProgress = state.completed_step_id !== null;
  const [askingResume, setAskingResume] = useState(hasProgress);

  if (askingResume) {
    return (
      <Box flexDirection="column">
        <Banner version={VERSION} />
        <Box marginTop={1} borderStyle="round" borderColor="blue" paddingX={2} paddingY={1} flexDirection="column">
          <Text>
            You have a previous setup in progress (completed through:{" "}
            <Text bold>{state.completed_step_id}</Text>).
          </Text>
          <Text>Resume where you left off?</Text>
        </Box>
        <Box marginTop={1}>
          <SelectInput
            items={[
              { label: "Yes, resume", value: "resume" },
              { label: "No, start fresh", value: "fresh" },
            ]}
            onSelect={(item) => {
              if (item.value === "fresh") {
                setState((prev) => {
                  const next = {
                    ...prev,
                    completed_step_id: null,
                    started_at: new Date().toISOString(),
                  };
                  saveState(next);
                  return next;
                });
              }
              setAskingResume(false);
              onNext();
            }}
          />
        </Box>
      </Box>
    );
  }

  return (
    <Box flexDirection="column">
      <Banner version={VERSION} />
      <Box
        marginTop={1}
        borderStyle="round"
        borderColor="blue"
        paddingX={2}
        paddingY={1}
        flexDirection="column"
      >
        <Text bold>Welcome to Gobby</Text>
        <Text> </Text>
        <Text>
          Gobby is a local-first daemon that unifies your AI coding assistants
        </Text>
        <Text>
          under one persistent platform — session tracking, task management,
        </Text>
        <Text>memory, and more.</Text>
        <Text> </Text>
        <Text dimColor>This setup takes about 5 minutes.</Text>
      </Box>
      <Box marginTop={1}>
        <SelectInput
          items={[{ label: "Let's go!", value: "start" }]}
          onSelect={() => {
            setState((prev) => {
              const next = { ...prev, completed_step_id: "welcome" };
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
