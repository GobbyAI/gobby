import { createContext, useContext } from "react";
import type { SettingsSectionId } from "../sections";
import type { UseSettingsReturn } from "../../../hooks/useSettings";
import type {
  ConfigApplyFailure,
  ConfigExportBundle,
  PromptDetail,
  PromptInfo,
  SecretInfo,
} from "../../../hooks/useConfiguration";

/** Predicate reporting whether a section currently holds unsaved edits. */
export type SectionDirtyGuard = () => boolean;

/**
 * The app-owned default-provider selection, threaded so the Providers & Models
 * section can drive the same `selectedProvider` state (persisted by App) that
 * the chat ProviderPicker uses. The model half lives in `clientSettings`
 * (`ui_settings.model`); provider is App-local state, not a `useSettings` field.
 */
export interface ProviderSelectionContextValue {
  selectedProvider: string | null;
  onSelectProvider: (provider: string) => void;
}

/**
 * The app-owned active-project selection, threaded so the Projects & Sessions
 * section can drive the same `selectedProjectId` state (persisted by App to
 * `ui_settings.selectedProjectId`) that the chrome's ProjectSelector uses.
 * Project selection is App-local state, not a `useSettings` field.
 */
export interface ProjectSelectionContextValue {
  selectedProjectId: string | null;
  onSelectProject: (projectId: string) => void;
}

export interface SaveConfigResult {
  ok: boolean;
  errors?: string[];
  conflict?: boolean;
  terminal?: boolean;
}

/**
 * Everything a settings section needs from the overlay: the loaded config
 * surface (shared so it loads once per overlay open) plus the dirty-guard
 * registry from `useSettingsOverlay`. Sections read this via
 * `useSettingsSectionContext`; the provider lives in SettingsSectionProvider
 * and tests inject a fake value through the context provider directly.
 */
export interface SettingsSectionContextValue {
  schema: Record<string, unknown> | null;
  configValues: Record<string, unknown>;
  activeConfigValues?: Record<string, unknown>;
  secretKeys: string[];
  pendingRestartKeys?: string[];
  failedLiveKeys?: Record<string, ConfigApplyFailure>;
  mutationError?: { message: string; terminal: boolean } | null;
  isLoading: boolean;
  saveConfig: (values: Record<string, unknown>) => Promise<SaveConfigResult>;
  registerDirtyGuard: (
    section: SettingsSectionId,
    isDirty: SectionDirtyGuard,
  ) => () => void;
  /**
   * The single app-wide `useSettings` instance, shared so client-side sections
   * (Appearance, Chat & Voice, Providers, Projects) read and mutate the same
   * ui_settings state the app chrome uses. Absent only when no provider is
   * mounted (the fallback below), which sections must tolerate.
   */
  clientSettings?: UseSettingsReturn;
  /**
   * App-owned default-provider selection. Absent when no provider is mounted
   * (the fallback below) or the host does not supply it; sections must tolerate
   * its absence.
   */
  providerSelection?: ProviderSelectionContextValue;
  /**
   * App-owned active-project selection. Absent when no provider is mounted
   * (the fallback below) or the host does not supply it; sections must tolerate
   * its absence.
   */
  projectSelection?: ProjectSelectionContextValue;
  /**
   * Live rules-engine enforcement flag. Unlike the draft-backed config rows,
   * this is a standalone surface (`/api/rules`) the provider reads/writes
   * directly via `useConfiguration`; the Automation & Workflows section renders
   * it as an immediate toggle. Absent when no provider is mounted (the fallback
   * below); sections must tolerate its absence.
   */
  rulesEnforcement?: boolean;
  /** Persist the rules-enforcement flag; resolves true on a successful write. */
  setRulesEnforcement?: (enabled: boolean) => Promise<boolean>;
  /**
   * Live global tool-approval rules: the daemon-wide auto-allow list. Like
   * `rulesEnforcement` this is a standalone surface
   * (`/api/config/tool-approvals/global`) the provider reads/writes directly via
   * `useConfiguration`; the Tool Approvals section renders it as an
   * immediate-save editor, distinct from the draft-backed `tool_approval.*`
   * rows. Absent when no provider is mounted (the fallback below); sections must
   * tolerate its absence.
   */
  globalApprovalRules?: string[];
  /** Read-only recommended default auto-allow rules, shown as context. */
  defaultApprovalRules?: string[];
  /** Read-only built-in exemptions that are always auto-allowed. */
  builtInApprovalExemptions?: string[];
  /** Persist the global auto-allow rules; resolves true on a successful write. */
  saveGlobalApprovalRules?: (rules: string[]) => Promise<boolean>;
  /**
   * Live `$secret:` store entries (metadata only — encrypted values never leave
   * the daemon). Like `rulesEnforcement` this is a standalone surface
   * (`/api/config/secrets`) the provider reads/writes directly via
   * `useConfiguration`; the Secrets & Auth section renders it as an
   * immediate-save CRUD list, distinct from the draft-backed secret-typed config
   * rows. Absent when no provider is mounted (the fallback below); sections must
   * tolerate its absence.
   */
  secrets?: SecretInfo[];
  /** Categories offered when creating a new secret-store entry. */
  secretCategories?: string[];
  /** Create or update a secret-store entry; resolves true on a successful write. */
  saveSecret?: (
    name: string,
    value: string,
    category?: string,
    description?: string,
  ) => Promise<boolean>;
  /** Delete a secret-store entry by name; resolves true on a successful write. */
  deleteSecret?: (name: string) => Promise<boolean>;
  /**
   * Bundled/overridden prompt list ($secret-style standalone surface,
   * `/api/config/prompts`). Like the secret store this is read/written directly
   * via `useConfiguration`; the Prompts & Templates section renders a per-prompt
   * markdown override editor. Absent when no provider is mounted (the fallback
   * below); sections must tolerate its absence.
   */
  prompts?: PromptInfo[];
  /** Prompt category -> count map, for the section's category filter. */
  promptCategories?: Record<string, number>;
  /** Fetch one prompt's detail (content + bundled default); null on failure. */
  getPromptDetail?: (path: string) => Promise<PromptDetail | null>;
  /** Save a prompt override body; resolves true on a successful write. */
  savePromptOverride?: (path: string, content: string) => Promise<boolean>;
  /** Delete a prompt override, reverting to bundled; true on success. */
  deletePromptOverride?: (path: string) => Promise<boolean>;
  /**
   * Full defaults-plus-overrides configuration document as YAML
   * (`/api/config/template`). Saved through `saveTemplate`; requires a daemon
   * restart to apply. Absent when no provider is mounted (the fallback below).
   */
  templateContent?: string;
  /** Persist the full YAML template; ok=false carries validation errors. */
  saveTemplate?: (
    content: string,
  ) => Promise<{ ok: boolean; errors?: string[] }>;
  /** Export desired daemon configuration as YAML; null on failure. */
  exportConfig?: () => Promise<ConfigExportBundle | null>;
  /** Replace desired daemon configuration from YAML; provider then refetches. */
  importConfig?: (
    content: string,
  ) => Promise<{ success: boolean; summary: string }>;
  /** Execute the explicit coordinator action for a registry-managed key. */
  runManagedAction?: (
    action: string,
    payload: Record<string, unknown>,
  ) => Promise<boolean>;
}

export const noopRegister: SettingsSectionContextValue["registerDirtyGuard"] =
  () => () => {};

const FALLBACK_CONTEXT: SettingsSectionContextValue = {
  schema: null,
  configValues: {},
  activeConfigValues: {},
  secretKeys: [],
  pendingRestartKeys: [],
  failedLiveKeys: {},
  isLoading: false,
  saveConfig: async () => ({
    ok: false,
    errors: ["Settings context unavailable"],
  }),
  registerDirtyGuard: noopRegister,
};

export const SettingsSectionContext =
  createContext<SettingsSectionContextValue>(FALLBACK_CONTEXT);

export function useSettingsSectionContext(): SettingsSectionContextValue {
  return useContext(SettingsSectionContext);
}
