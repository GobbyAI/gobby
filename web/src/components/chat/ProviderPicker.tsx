import { useState, useCallback, useEffect } from "react";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from "../ui/Dialog";
import { SourceIcon } from "../shared/SourceIcon";
import { Button } from "../ui/Button";
import { Chip } from "../ui/Chip";
import { cn } from "../../lib/utils";
import {
  fetchProviderModelCatalog,
  getOrderedProviders,
  getModelsForProvider,
  getProviderDisplayName,
  getProviderDisplayNameFromEntry,
  inputModalityChips,
  isHiddenProvider,
  type ProviderModelEntry,
  type ProviderModelOption,
} from "../../lib/providerModels";

function getVisibleModelsForProvider(
  catalog: ProviderModelEntry[],
  provider: string,
  currentModel: string,
): ProviderModelOption[] {
  const entry = catalog.find((candidate) => candidate.provider === provider);
  const models = getModelsForProvider(catalog, provider);
  if (models.length > 0) return models;
  if (entry) return [{ value: "default", label: "Default" }];
  return [
    { value: currentModel || "default", label: currentModel || "Default" },
  ];
}

function CapabilityChips({ modalities }: { modalities?: string[] | null }) {
  const chips = inputModalityChips(modalities);
  if (chips.length === 0) return null;
  return (
    <span className="capability-chips" aria-hidden="true">
      {chips.map((label) => (
        <Chip key={label} className="capability-chip">
          {label}
        </Chip>
      ))}
    </span>
  );
}

function getExecutionProvider(
  entry: ProviderModelEntry | undefined,
  catalogProvider: string,
): string {
  return entry?.execution_provider?.trim() || catalogProvider;
}

function getActiveCatalogProvider(
  catalog: ProviderModelEntry[],
  executionProvider: string,
  currentModel: string,
): string {
  const normalizedExecutionProvider = executionProvider.trim().toLowerCase();
  const owner = catalog.find(
    (entry) =>
      getExecutionProvider(entry, entry.provider).toLowerCase() ===
        normalizedExecutionProvider &&
      entry.models.some((model) => model.value === currentModel),
  );
  return (owner?.provider ?? executionProvider).trim().toLowerCase();
}

interface ProviderPickerProps {
  open: boolean;
  onClose: () => void;
  currentProvider: string | null;
  currentModel: string;
  availableProviders: string[];
  onModelChange: (model: string) => void;
  onProviderChange: (provider: string) => void;
  onSwitchProvider?: (provider: string) => void;
  onSelect?: (provider: string, model: string) => void;
  hasMessages: boolean;
}

export function ProviderPicker({
  open,
  onClose,
  currentProvider,
  currentModel,
  availableProviders,
  onModelChange,
  onProviderChange,
  onSwitchProvider,
  onSelect,
  hasMessages,
}: ProviderPickerProps) {
  const [confirmSwitch, setConfirmSwitch] = useState<{
    provider: string;
    model: string;
  } | null>(null);
  const [catalog, setCatalog] = useState<ProviderModelEntry[]>([]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    fetchProviderModelCatalog()
      .then((entries) => {
        if (!cancelled) setCatalog(entries);
      })
      .catch((err) => {
        console.error("[ProviderPicker] failed to load model catalog", err);
        if (!cancelled) setCatalog([]);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const effectiveProvider = currentProvider || "claude";
  const activeCatalogProvider = getActiveCatalogProvider(
    catalog,
    effectiveProvider,
    currentModel,
  );
  const catalogByProvider = new Map(
    catalog.map((entry) => [entry.provider.toLowerCase(), entry]),
  );
  // Hidden providers stay unpickable even when the current session or the
  // availableProviders prop carries one (moat 7f76d568, #20049); providers
  // without a web-chat transport are likewise hidden rather than disabled.
  const visibleProviders = getOrderedProviders(
    Array.from(
      new Set([
        ...(availableProviders.length > 0
          ? availableProviders
          : [effectiveProvider]),
        ...catalog.map((entry) => entry.provider),
      ]),
    ),
  ).filter((provider) => {
    if (isHiddenProvider(provider)) return false;
    return (
      catalogByProvider.get(provider.toLowerCase())?.supports_web_chat !== false
    );
  });

  const applySelection = useCallback(
    (provider: string, model: string) => {
      if (onSelect) {
        onSelect(provider, model);
      } else if (
        provider === effectiveProvider ||
        (!currentProvider && provider === "claude")
      ) {
        onModelChange(model);
      } else {
        onProviderChange(provider);
        onModelChange(model);
        onSwitchProvider?.(provider);
      }
      onClose();
      setConfirmSwitch(null);
    },
    [
      effectiveProvider,
      currentProvider,
      onModelChange,
      onProviderChange,
      onSwitchProvider,
      onSelect,
      onClose,
    ],
  );

  const handleSelect = useCallback(
    (provider: string, model: string) => {
      const isSameExecutionProvider =
        provider === effectiveProvider ||
        (!currentProvider && provider === "claude");
      if (hasMessages && !isSameExecutionProvider) {
        setConfirmSwitch({ provider, model });
        return;
      }
      applySelection(provider, model);
    },
    [applySelection, currentProvider, effectiveProvider, hasMessages],
  );

  const handleConfirm = useCallback(() => {
    if (!confirmSwitch) return;
    applySelection(confirmSwitch.provider, confirmSwitch.model);
  }, [applySelection, confirmSwitch]);

  const handleCancel = useCallback(() => {
    setConfirmSwitch(null);
  }, []);

  const handleOpenChange = useCallback(
    (isOpen: boolean) => {
      if (!isOpen) {
        onClose();
        setConfirmSwitch(null);
      }
    },
    [onClose],
  );

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-sm gap-0 p-0">
        {confirmSwitch ? (
          <div className="p-4">
            <DialogTitle className="mb-2 text-sm font-medium">
              Switch provider?
            </DialogTitle>
            <DialogDescription className="mb-4 text-xs">
              Switching to{" "}
              <span className="font-medium text-foreground">
                {getProviderDisplayName(confirmSwitch.provider)}
              </span>{" "}
              will start a new conversation. Your current chat will be preserved
              in session history.
            </DialogDescription>
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="secondary"
                size="sm"
                className="rounded border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                onClick={handleCancel}
              >
                Cancel
              </Button>
              <Button
                type="button"
                variant="primary"
                size="sm"
                className="rounded bg-accent px-3 py-1.5 text-xs text-accent-foreground transition-colors hover:bg-accent/90"
                onClick={handleConfirm}
              >
                Switch
              </Button>
            </div>
          </div>
        ) : (
          <>
            <div className="px-4 pt-4 pb-2">
              <DialogTitle className="text-sm font-medium">
                Provider & Model
              </DialogTitle>
              <DialogDescription className="mt-0.5 text-xs">
                {visibleProviders.length > 1
                  ? "Select a provider and model for this conversation"
                  : "Select a model for this conversation"}
              </DialogDescription>
            </div>
            <div className="max-h-[60vh] overflow-y-auto px-2 pb-2">
              {visibleProviders.map((provider) => {
                const entry = catalogByProvider.get(provider.toLowerCase());
                const models = getVisibleModelsForProvider(
                  catalog,
                  provider,
                  currentModel,
                );
                const isActive =
                  provider.toLowerCase() === activeCatalogProvider;
                const executionProvider = getExecutionProvider(entry, provider);
                const displayName = getProviderDisplayNameFromEntry(
                  entry,
                  provider,
                );

                return (
                  <div key={provider} className="mb-1">
                    <div className="flex items-center gap-2 px-2 py-1.5 text-xs text-muted-foreground">
                      <SourceIcon
                        source={entry?.provider_type ?? provider}
                        size={14}
                      />
                      <span className="font-medium text-foreground">
                        {displayName}
                      </span>
                      {entry?.deprecated && (
                        <span className="rounded bg-muted px-1 py-0.5 text-[length:var(--text-2xs)] text-muted-foreground">
                          deprecated
                        </span>
                      )}
                      {isActive && (
                        <span className="rounded bg-accent/20 px-1 py-0.5 text-[length:var(--text-2xs)] text-accent">
                          active
                        </span>
                      )}
                    </div>
                    {models.map((model) => {
                      const isSelected =
                        isActive && currentModel === model.value;

                      return (
                        <Button
                          key={`${provider}-${model.value}`}
                          type="button"
                          variant="ghost"
                          size="sm"
                          dense
                          className={cn(
                            "mx-1 min-h-0 w-full justify-start rounded border-0 px-3 py-1.5 text-left text-xs font-normal whitespace-normal transition-colors",
                            "hover:bg-muted",
                            isSelected
                              ? "bg-accent/10 font-medium text-accent"
                              : "text-muted-foreground",
                          )}
                          style={{ width: "calc(100% - 8px)" }}
                          onClick={() =>
                            handleSelect(executionProvider, model.value)
                          }
                        >
                          {model.label}
                          <CapabilityChips
                            modalities={model.input_modalities}
                          />
                          {isSelected && (
                            <span className="ml-2 text-[length:var(--text-2xs)]">
                              ●
                            </span>
                          )}
                        </Button>
                      );
                    })}
                  </div>
                );
              })}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
