import React, { useState, useCallback, useRef } from "react";
import { Box, Text } from "ink";
import { StepHeader } from "./components/StepHeader.js";
import { loadState, type SetupState } from "./utils/state.js";
import type { StepProps } from "./types.js";

// Step components
import { Bootstrap } from "./steps/Bootstrap.js";
import { Welcome } from "./steps/Welcome.js";
import { AboutYou } from "./steps/AboutYou.js";
import { SystemCheck } from "./steps/SystemCheck.js";
import { Configuration } from "./steps/Configuration.js";
import { NetworkSecurity } from "./steps/NetworkSecurity.js";
import { Tailscale } from "./steps/Tailscale.js";
import { ProjectDiscovery } from "./steps/ProjectDiscovery.js";
import { CliHooks } from "./steps/CliHooks.js";
import { Integrations } from "./steps/Integrations.js";
import { Services } from "./steps/Services.js";
import { PersonalWorkspace } from "./steps/PersonalWorkspace.js";
import { Launch } from "./steps/Launch.js";

interface StepDef {
  id: string;
  label: string;
  component: React.ComponentType<StepProps>;
  skipIf?: (state: SetupState) => boolean;
  /** If true, this step doesn't show a numbered header (e.g. welcome, bootstrap) */
  hideHeader?: boolean;
}

const STEPS: StepDef[] = [
  {
    id: "bootstrap",
    label: "Bootstrap",
    component: Bootstrap,
    skipIf: () => !!process.env.GOBBY_SKIP_BOOTSTRAP,
    hideHeader: true,
  },
  { id: "welcome", label: "Welcome", component: Welcome, hideHeader: true },
  { id: "about-you", label: "About You", component: AboutYou },
  { id: "syscheck", label: "System Check", component: SystemCheck },
  { id: "config", label: "Configuration", component: Configuration },
  { id: "firewall", label: "Network Security", component: NetworkSecurity },
  {
    id: "tailscale",
    label: "Tailscale",
    component: Tailscale,
    skipIf: (s) => !s.detected_tools?.tailscale,
  },
  { id: "projects", label: "Project Discovery", component: ProjectDiscovery },
  { id: "hooks", label: "CLI Hooks", component: CliHooks },
  { id: "integrations", label: "Integrations", component: Integrations },
  {
    id: "services",
    label: "Services",
    component: Services,
    skipIf: (s) => !s.detected_tools?.docker,
  },
  { id: "personal", label: "Personal Workspace", component: PersonalWorkspace },
  { id: "launch", label: "Launch", component: Launch },
];

/** Find the next non-skipped step index starting from `from` (inclusive). */
function findNextActive(from: number, state: SetupState): number {
  for (let i = from; i < STEPS.length; i++) {
    const step = STEPS[i];
    if (!step.skipIf?.(state)) return i;
  }
  return STEPS.length; // past end = done
}

/** Compute the display number for a step (only counting non-hidden, non-skipped steps). */
function computeStepDisplay(
  stepIdx: number,
  state: SetupState,
): { stepNumber: number; totalSteps: number } {
  let count = 0;
  let stepNumber = 0;
  for (let i = 0; i < STEPS.length; i++) {
    const s = STEPS[i];
    if (s.hideHeader || s.skipIf?.(state)) continue;
    count++;
    if (i === stepIdx) stepNumber = count;
  }
  return { stepNumber, totalSteps: count };
}

export function App(): React.ReactElement {
  const [initialState] = useState<SetupState>(() => loadState());
  const [state, setStateRaw] = useState<SetupState>(initialState);
  const stateRef = useRef(state);

  const setState = useCallback(
    (fn: (prev: SetupState) => SetupState) => {
      const next = fn(stateRef.current);
      stateRef.current = next;
      setStateRaw(next);
    },
    [],
  );

  const [currentIdx, setCurrentIdx] = useState<number>(() =>
    findNextActive(0, initialState),
  );

  const onNext = useCallback(() => {
    setCurrentIdx((prev) => {
      const currentStep = STEPS[prev];
      if (currentStep?.id === "welcome") {
        const completedIdx = STEPS.findIndex(
          (step) => step.id === stateRef.current.completed_step_id,
        );
        if (completedIdx > prev) {
          return findNextActive(completedIdx + 1, stateRef.current);
        }
      }
      return findNextActive(prev + 1, stateRef.current);
    });
  }, []);

  if (initialState.completed_at) {
    return (
      <Box flexDirection="column">
        <Text bold color="green">
          Setup is already complete.
        </Text>
        <Text>Web UI: http://localhost:{state.ports.ui}</Text>
        <Text>
          Run <Text bold>gobby status</Text> to check the daemon.
        </Text>
      </Box>
    );
  }

  // Done?
  if (currentIdx >= STEPS.length) {
    return <Box />;
  }

  const step = STEPS[currentIdx];
  const StepComponent = step.component;
  const { stepNumber, totalSteps } = computeStepDisplay(currentIdx, state);

  return (
    <Box flexDirection="column">
      {!step.hideHeader && (
        <StepHeader
          stepNumber={stepNumber}
          totalSteps={totalSteps}
          title={step.label}
        />
      )}
      <StepComponent state={state} setState={setState} onNext={onNext} />
    </Box>
  );
}
