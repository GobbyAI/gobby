import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  SETTINGS_SECTION_REGISTRY,
  getSettingsSectionComponent,
} from "../sections/index";
import { SETTINGS_SECTIONS } from "../sections";
import { SettingsSection } from "../sections/SettingsSection";
import {
  SettingsSectionContext,
  type SettingsSectionContextValue,
} from "../sections/SettingsSectionContext";
import { useSettingsOverlay } from "../useSettingsOverlay";

afterEach(() => {
  cleanup();
});

describe("settings section registry", () => {
  it("lists every IA section once, in audit order", () => {
    expect(SETTINGS_SECTION_REGISTRY.map((entry) => entry.id)).toEqual(
      SETTINGS_SECTIONS.map((section) => section.id),
    );
    expect(SETTINGS_SECTION_REGISTRY).toHaveLength(13);
  });

  it("carries the IA label and a component for each section", () => {
    SETTINGS_SECTION_REGISTRY.forEach((entry, index) => {
      expect(entry.label).toBe(SETTINGS_SECTIONS[index].label);
      expect(typeof entry.component).toBe("function");
      expect(getSettingsSectionComponent(entry.id)).toBe(entry.component);
    });
  });
});

// A stable config slice — a fresh object each render would re-key the draft's
// source memo and clobber edits, so the fixtures are module-level constants.
// Config arrives nested (as `/api/config/values` returns it), and owned paths
// are dotted; the shell resolves them by walking the nested object.
const DEMO_CONFIG: Record<string, unknown> = { demo: { value: "initial" } };
const OWNED_PATHS: readonly string[] = ["demo.value"];

function makeContext(
  overrides: Partial<SettingsSectionContextValue> = {},
): SettingsSectionContextValue {
  return {
    schema: {},
    configValues: DEMO_CONFIG,
    secretKeys: [],
    isLoading: false,
    saveConfig: vi.fn(async () => ({ ok: true })),
    registerDirtyGuard: () => () => {},
    ...overrides,
  };
}

function DemoSection() {
  return (
    <SettingsSection sectionId="appearance" ownedPaths={OWNED_PATHS}>
      {({ getValue, setValue }) => (
        <input
          aria-label="demo field"
          value={String(getValue("demo.value") ?? "")}
          onChange={(event) => setValue("demo.value", event.target.value)}
        />
      )}
    </SettingsSection>
  );
}

// Captures the predicate the shell registers. A holder object is used instead
// of a bare `let` so the closure-assigned value keeps its declared type at the
// read sites (a bare `let` narrows to its `null` initializer across the
// closure, which makes the optional call uncallable under strict typing).
function captureGuard(): {
  holder: { isDirty: (() => boolean) | null };
  register: SettingsSectionContextValue["registerDirtyGuard"];
} {
  const holder: { isDirty: (() => boolean) | null } = { isDirty: null };
  const register: SettingsSectionContextValue["registerDirtyGuard"] = (
    _section,
    isDirty,
  ) => {
    holder.isDirty = isDirty;
    return () => {};
  };
  return { holder, register };
}

describe("SettingsSection dirty draft", () => {
  it("registers a dirty predicate that is clean until an edit", () => {
    const { holder, register } = captureGuard();
    const context = makeContext({ registerDirtyGuard: register });

    render(
      <SettingsSectionContext.Provider value={context}>
        <DemoSection />
      </SettingsSectionContext.Provider>,
    );

    expect(holder.isDirty).not.toBeNull();
    expect(holder.isDirty?.()).toBe(false);

    fireEvent.change(screen.getByLabelText("demo field"), {
      target: { value: "changed" },
    });

    expect(holder.isDirty?.()).toBe(true);
  });

  it("reverts the edit and clears dirty on Discard", () => {
    const { holder, register } = captureGuard();
    const context = makeContext({ registerDirtyGuard: register });

    render(
      <SettingsSectionContext.Provider value={context}>
        <DemoSection />
      </SettingsSectionContext.Provider>,
    );

    const input = screen.getByLabelText("demo field") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "changed" } });
    expect(holder.isDirty?.()).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Discard" }));

    expect(holder.isDirty?.()).toBe(false);
    expect(input.value).toBe("initial");
  });

  it("saves only the owned paths and clears dirty on success", async () => {
    const saveConfig = vi.fn(async () => ({ ok: true as const }));
    const { holder, register } = captureGuard();
    const context = makeContext({ saveConfig, registerDirtyGuard: register });

    render(
      <SettingsSectionContext.Provider value={context}>
        <DemoSection />
      </SettingsSectionContext.Provider>,
    );

    fireEvent.change(screen.getByLabelText("demo field"), {
      target: { value: "changed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(holder.isDirty?.()).toBe(false));
    expect(saveConfig).toHaveBeenCalledWith({ "demo.value": "changed" });
  });
});

// Integration: a real useSettingsOverlay controller consults the shell's
// registered predicate when the user tries to leave the section.
function GuardHarness({
  confirmDiscard,
}: {
  confirmDiscard: (message: string) => boolean;
}) {
  const overlay = useSettingsOverlay({ confirmDiscard });
  const context = makeContext({
    registerDirtyGuard: overlay.registerDirtyGuard,
  });
  return (
    <SettingsSectionContext.Provider value={context}>
      <span data-testid="active-section">{overlay.activeSection}</span>
      <button
        type="button"
        onClick={() => overlay.selectSection("observability")}
      >
        Switch section
      </button>
      <DemoSection />
    </SettingsSectionContext.Provider>
  );
}

describe("SettingsSection blocks navigation while dirty", () => {
  it("blocks a section switch when discard is declined", () => {
    const confirmDiscard = vi.fn(() => false);
    render(<GuardHarness confirmDiscard={confirmDiscard} />);

    fireEvent.change(screen.getByLabelText("demo field"), {
      target: { value: "changed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Switch section" }));

    expect(confirmDiscard).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("active-section").textContent).toBe("appearance");
  });

  it("allows a section switch when discard is confirmed", () => {
    const confirmDiscard = vi.fn(() => true);
    render(<GuardHarness confirmDiscard={confirmDiscard} />);

    fireEvent.change(screen.getByLabelText("demo field"), {
      target: { value: "changed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Switch section" }));

    expect(confirmDiscard).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("active-section").textContent).toBe(
      "observability",
    );
  });

  it("never prompts when the section is clean", () => {
    const confirmDiscard = vi.fn(() => true);
    render(<GuardHarness confirmDiscard={confirmDiscard} />);

    fireEvent.click(screen.getByRole("button", { name: "Switch section" }));

    expect(confirmDiscard).not.toHaveBeenCalled();
    expect(screen.getByTestId("active-section").textContent).toBe(
      "observability",
    );
  });
});
