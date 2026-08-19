import { useId, useState } from "react";
import {
  AUTO_REASONING_EFFORT,
  getModelsForProvider,
  getOrderedProviders,
  getReasoningOptionsForModel,
  type ProviderModelEntry,
} from "../../lib/providerModels";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { NativeSelect } from "../ui/NativeSelect";
import { coarseHitAreaCls } from "../ui/controlStyles";
import type { AgentFormData } from "./AgentEditForm.types";
import { AgentMetaRow as MetaRow } from "./AgentMetaRow";

const FALLBACK_PROVIDER_OPTIONS = ["claude", "codex", "qwen", "droid"];

interface AgentProviderSettingsProps {
  form: AgentFormData;
  onChange: (form: AgentFormData) => void;
  providerCatalog: ProviderModelEntry[];
  branches: string[];
  isGitProject: boolean;
  pipelines?: { id: string; name: string }[];
  agentNames: string[];
}

export function AgentProviderSettings({
  form,
  onChange,
  providerCatalog,
  branches,
  isGitProject,
  pipelines,
  agentNames,
}: AgentProviderSettingsProps) {
  const checkboxIdPrefix = useId();
  const [customModelInput, setCustomModelInput] = useState(false);
  const [customBranchInput, setCustomBranchInput] = useState(false);

  const set = <K extends keyof AgentFormData>(
    key: K,
    value: AgentFormData[K],
  ) => onChange({ ...form, [key]: value });

  const toggleSurface = (surface: string) => {
    const next = form.surfaces.includes(surface)
      ? form.surfaces.filter((item) => item !== surface)
      : [...form.surfaces, surface];
    onChange({ ...form, surfaces: next.length > 0 ? next : ["spawn"] });
  };

  const isInheritProvider = form.provider === "inherit";
  const discoveredProviders = getOrderedProviders(
    providerCatalog.map((entry) => entry.provider),
  );
  const providerOptions =
    discoveredProviders.length > 0
      ? discoveredProviders
      : FALLBACK_PROVIDER_OPTIONS;
  const models = isInheritProvider
    ? [{ value: "", label: "(default)" }]
    : [
        { value: "", label: "(default)" },
        ...getModelsForProvider(providerCatalog, form.provider),
      ];
  const isKnownModel = models.some((model) => model.value === form.model);
  const showCustomModel =
    !isInheritProvider &&
    (customModelInput || (!isKnownModel && form.model !== ""));
  const reasoningOptions = isInheritProvider
    ? [{ value: AUTO_REASONING_EFFORT, label: "Auto", disabled: true }]
    : getReasoningOptionsForModel(providerCatalog, form.provider, form.model);
  const reasoningDisabled =
    reasoningOptions.length === 1 && Boolean(reasoningOptions[0]?.disabled);

  const resolveReasoningEffort = (
    provider: string,
    model: string,
    currentReasoning: string,
  ): string => {
    if (provider === "inherit") return AUTO_REASONING_EFFORT;
    const options = getReasoningOptionsForModel(
      providerCatalog,
      provider,
      model,
    );
    return options.some((option) => option.value === currentReasoning)
      ? currentReasoning
      : AUTO_REASONING_EFFORT;
  };

  const branchKnown =
    form.base_branch === "inherit" || branches.includes(form.base_branch);
  const showCustomBranch =
    isGitProject &&
    (customBranchInput || (!branchKnown && form.base_branch !== ""));

  return (
    <div className="border-b border-border px-5 py-3">
      <MetaRow label="Provider">
        <NativeSelect
          aria-label="Provider"
          value={form.provider}
          onChange={(event) => {
            const provider = event.target.value;
            setCustomModelInput(false);
            if (provider === "inherit") {
              onChange({
                ...form,
                provider,
                model: "",
                reasoning_effort: AUTO_REASONING_EFFORT,
                reasoning_required: false,
              });
              return;
            }

            const providerModels = getModelsForProvider(
              providerCatalog,
              provider,
            );
            const model = providerModels.some(
              (option) => option.value === form.model,
            )
              ? form.model
              : "";
            const reasoningEffort = resolveReasoningEffort(
              provider,
              model,
              form.reasoning_effort,
            );
            onChange({
              ...form,
              provider,
              model,
              reasoning_effort: reasoningEffort,
              reasoning_required:
                reasoningEffort === AUTO_REASONING_EFFORT
                  ? false
                  : form.reasoning_required,
            });
          }}
        >
          <option value="inherit">(default)</option>
          {providerOptions.map((provider) => (
            <option key={provider} value={provider}>
              {provider}
            </option>
          ))}
        </NativeSelect>
      </MetaRow>

      <MetaRow label="Model">
        {showCustomModel ? (
          <div className="flex items-center gap-1">
            <Input
              value={form.model}
              onChange={(event) => set("model", event.target.value)}
              placeholder="e.g. claude-sonnet-4-5-20250929"
              aria-label="Custom model"
              autoFocus={customModelInput}
            />
            <Button
              type="button"
              variant="ghost"
              size="icon"
              dense
              className={`${coarseHitAreaCls} shrink-0`}
              onClick={() => {
                setCustomModelInput(false);
                set("model", "");
              }}
              aria-label="Use discovered models"
            >
              &times;
            </Button>
          </div>
        ) : (
          <NativeSelect
            aria-label="Model"
            value={form.model}
            onChange={(event) => {
              if (event.target.value === "__custom__") {
                setCustomModelInput(true);
                set("model", "");
                return;
              }
              const model = event.target.value;
              const reasoningEffort = resolveReasoningEffort(
                form.provider,
                model,
                form.reasoning_effort,
              );
              onChange({
                ...form,
                model,
                reasoning_effort: reasoningEffort,
                reasoning_required:
                  reasoningEffort === AUTO_REASONING_EFFORT
                    ? false
                    : form.reasoning_required,
              });
            }}
          >
            {models.map((model) => (
              <option key={model.value} value={model.value}>
                {model.label}
              </option>
            ))}
            {!isInheritProvider && (
              <option value="__custom__">Custom...</option>
            )}
          </NativeSelect>
        )}
      </MetaRow>

      <MetaRow label="Fallback">
        {form.fallback_agent ? (
          <div className="flex items-center gap-1">
            <NativeSelect
              aria-label="Fallback agent"
              value={form.fallback_agent}
              onChange={(event) => set("fallback_agent", event.target.value)}
            >
              {agentNames
                .filter((name) => name !== form.name)
                .includes(form.fallback_agent) ? null : (
                <option value={form.fallback_agent}>
                  {form.fallback_agent} (missing)
                </option>
              )}
              {agentNames
                .filter((name) => name !== form.name)
                .map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
            </NativeSelect>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              dense
              className={`${coarseHitAreaCls} shrink-0`}
              onClick={() => set("fallback_agent", "")}
              aria-label="Remove fallback agent"
            >
              &times;
            </Button>
          </div>
        ) : (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            dense
            className={`${coarseHitAreaCls} h-auto min-h-0 p-0 text-[var(--accent)] underline hover:text-[var(--accent-hover)]`}
            disabled={!agentNames.some((name) => name !== form.name)}
            onClick={() => {
              const fallback = agentNames.find((name) => name !== form.name);
              if (fallback) set("fallback_agent", fallback);
            }}
          >
            + Add fallback agent
          </Button>
        )}
      </MetaRow>

      <MetaRow label="Reasoning">
        <NativeSelect
          aria-label="Reasoning"
          value={form.reasoning_effort}
          onChange={(event) => {
            const reasoningEffort = event.target.value;
            onChange({
              ...form,
              reasoning_effort: reasoningEffort,
              reasoning_required:
                reasoningEffort === AUTO_REASONING_EFFORT
                  ? false
                  : form.reasoning_required,
            });
          }}
          disabled={reasoningDisabled}
        >
          {reasoningOptions.map((option) => (
            <option
              key={option.value}
              value={option.value}
              disabled={option.disabled}
            >
              {option.label}
            </option>
          ))}
        </NativeSelect>
      </MetaRow>

      <MetaRow label="Require support">
        <label
          htmlFor={`${checkboxIdPrefix}-reasoning-required`}
          className="flex items-center justify-end gap-1.5 text-sm text-[var(--text-primary)] select-none"
        >
          <Input
            id={`${checkboxIdPrefix}-reasoning-required`}
            type="checkbox"
            wrapperClassName="w-auto"
            className="size-4 h-4 shrink-0 p-0"
            aria-label="Require reasoning support"
            checked={form.reasoning_required}
            disabled={form.reasoning_effort === AUTO_REASONING_EFFORT}
            onChange={(event) =>
              set("reasoning_required", event.target.checked)
            }
          />
          <span>
            {form.reasoning_effort === AUTO_REASONING_EFFORT
              ? "Disabled on Auto"
              : "Fail if unsupported"}
          </span>
        </label>
      </MetaRow>

      <MetaRow label="Mode">
        <NativeSelect
          aria-label="Mode"
          value={form.mode}
          onChange={(event) => set("mode", event.target.value)}
        >
          <option value="inherit">(default)</option>
          <option value="interactive">Interactive</option>
          <option value="embedded">Embedded</option>
          <option value="headless">Headless</option>
        </NativeSelect>
      </MetaRow>

      <MetaRow label="Surfaces">
        <div className="flex flex-col gap-1.5">
          {["spawn", "persona"].map((surface) => (
            <label
              key={surface}
              htmlFor={`${checkboxIdPrefix}-surface-${surface}`}
              className="flex items-center justify-end gap-1.5 text-sm text-[var(--text-primary)] select-none"
            >
              <Input
                id={`${checkboxIdPrefix}-surface-${surface}`}
                type="checkbox"
                wrapperClassName="w-auto"
                className="size-4 h-4 shrink-0 p-0"
                aria-label={`${surface === "spawn" ? "Spawn" : "Persona"} surface`}
                checked={form.surfaces.includes(surface)}
                onChange={() => toggleSurface(surface)}
              />
              <span>{surface === "spawn" ? "Spawn" : "Persona"}</span>
            </label>
          ))}
        </div>
      </MetaRow>

      <MetaRow label="Isolation">
        <NativeSelect
          aria-label="Isolation"
          value={isGitProject ? form.isolation : "inherit"}
          onChange={(event) => set("isolation", event.target.value)}
          disabled={!isGitProject}
        >
          <option value="inherit">(default)</option>
          <option value="none">None</option>
          <option value="worktree">Worktree</option>
          <option value="clone">Clone</option>
        </NativeSelect>
      </MetaRow>

      <MetaRow label="Base branch">
        {!isGitProject ? (
          <NativeSelect aria-label="Base branch" disabled value="inherit">
            <option value="inherit">(default)</option>
          </NativeSelect>
        ) : showCustomBranch ? (
          <div className="flex items-center gap-1">
            <Input
              value={form.base_branch}
              onChange={(event) => set("base_branch", event.target.value)}
              placeholder="branch name"
              aria-label="Custom base branch"
              autoFocus={customBranchInput}
            />
            <Button
              type="button"
              variant="ghost"
              size="icon"
              dense
              className={`${coarseHitAreaCls} shrink-0`}
              onClick={() => {
                setCustomBranchInput(false);
                set("base_branch", "inherit");
              }}
              aria-label="Use known branches"
            >
              &times;
            </Button>
          </div>
        ) : (
          <NativeSelect
            aria-label="Base branch"
            value={form.base_branch}
            onChange={(event) => {
              if (event.target.value === "__custom__") {
                setCustomBranchInput(true);
                set("base_branch", "");
              } else {
                set("base_branch", event.target.value);
              }
            }}
          >
            <option value="inherit">(default)</option>
            {branches.map((branch) => (
              <option key={branch} value={branch}>
                {branch}
              </option>
            ))}
            <option value="__custom__">Custom...</option>
          </NativeSelect>
        )}
      </MetaRow>

      {pipelines && (
        <MetaRow label="Pipeline">
          <NativeSelect
            aria-label="Pipeline"
            value={form.pipeline}
            onChange={(event) => set("pipeline", event.target.value)}
          >
            <option value="">(none)</option>
            {pipelines.map((pipeline) => (
              <option key={pipeline.id} value={pipeline.name}>
                {pipeline.name}
              </option>
            ))}
          </NativeSelect>
        </MetaRow>
      )}

      <MetaRow label="Timeout (s)">
        <Input
          type="number"
          min={0}
          aria-label="Timeout in seconds"
          value={form.timeout}
          onChange={(event) => {
            const timeout = Number(event.target.value);
            set(
              "timeout",
              Number.isFinite(timeout) && timeout >= 0 ? timeout : 0,
            );
          }}
        />
      </MetaRow>
    </div>
  );
}
