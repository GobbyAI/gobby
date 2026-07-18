import { useState, useCallback, useEffect } from "react";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from "./ui/Dialog";
import { SourceIcon } from "../shared/SourceIcon";
import { cn } from "../../lib/utils";
import {
  fetchProviderModelCatalog,
  getOrderedProviders,
  getModelsForProvider,
  getProviderDisplayName,
  getProviderDisplayNameFromEntry,
  type ProviderModelEntry,
} from "../../lib/providerModels";

function getVisibleModelsForProvider(
  catalog: ProviderModelEntry[],
  provider: string,
  currentModel: string,
): { value: string; label: string }[] {
  const entry = catalog.find((candidate) => candidate.provider === provider);
  const models = getModelsForProvider(catalog, provider);
  if (models.length > 0) return models;
  if (entry) return [{ value: "default", label: "Default" }];
  return [{ value: currentModel || "default", label: currentModel || "Default" }];
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
  const visibleProviders = getOrderedProviders(
    Array.from(
      new Set([
        ...(availableProviders.length > 0 ? availableProviders : [effectiveProvider]),
        ...catalog.map((entry) => entry.provider),
      ]),
    ),
  );

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
      <DialogContent className="max-w-sm p-0 gap-0">
        {confirmSwitch ? (
          <div className="p-4">
            <DialogTitle className="text-sm font-medium mb-2">
              Switch provider?
            </DialogTitle>
            <DialogDescription className="text-xs mb-4">
              Switching to{" "}
              <span className="text-foreground font-medium">
                {getProviderDisplayName(confirmSwitch.provider)}
              </span>{" "}
              will start a new conversation. Your current chat will be preserved
              in session history.
            </DialogDescription>
            <div className="flex gap-2 justify-end">
              <button
                className="px-3 py-1.5 text-xs rounded border border-border text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
                onClick={handleCancel}
              >
                Cancel
              </button>
              <button
                className="px-3 py-1.5 text-xs rounded bg-accent text-accent-foreground hover:bg-accent/90 transition-colors"
                onClick={handleConfirm}
              >
                Switch
              </button>
            </div>
          </div>
        ) : (
          <>
            <div className="px-4 pt-4 pb-2">
              <DialogTitle className="text-sm font-medium">
                Provider & Model
              </DialogTitle>
              <DialogDescription className="text-xs mt-0.5">
                {visibleProviders.length > 1
                  ? "Select a provider and model for this conversation"
                  : "Select a model for this conversation"}
              </DialogDescription>
            </div>
            <div className="px-2 pb-2 max-h-[60vh] overflow-y-auto">
              {visibleProviders.map((provider) => {
                const entry = catalogByProvider.get(provider.toLowerCase());
                const models = getVisibleModelsForProvider(
                  catalog,
                  provider,
                  currentModel,
                );
                const unavailableReason =
                  entry?.supports_web_chat === false
                    ? entry.unavailable_reason || "Unavailable for web chat"
                    : null;
                const isDisabled = Boolean(unavailableReason);
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
                      <SourceIcon source={provider} size={14} />
                      <span className="font-medium text-foreground">
                        {displayName}
                      </span>
                      {entry?.deprecated && (
                        <span className="text-[length:var(--text-2xs)] px-1 py-0.5 rounded bg-muted text-muted-foreground">
                          deprecated
                        </span>
                      )}
                      {unavailableReason && (
                        <span className="text-[length:var(--text-2xs)] px-1 py-0.5 rounded bg-muted text-muted-foreground">
                          unavailable
                        </span>
                      )}
                      {isActive && (
                        <span className="text-[length:var(--text-2xs)] px-1 py-0.5 rounded bg-accent/20 text-accent">
                          active
                        </span>
                      )}
                    </div>
                    {unavailableReason && (
                      <div className="px-3 pb-1 text-[length:var(--text-2xs)] text-muted-foreground">
                        {unavailableReason}
                      </div>
                    )}
                    {models.map((model) => {
                      const isSelected =
                        isActive && currentModel === model.value;

                      return (
                        <button
                          key={`${provider}-${model.value}`}
                          className={cn(
                            "w-full text-left px-3 py-1.5 text-xs rounded mx-1 transition-colors",
                            "hover:bg-muted",
                            isSelected
                              ? "bg-accent/10 text-accent font-medium"
                              : "text-muted-foreground",
                            isDisabled && "opacity-50 cursor-not-allowed hover:bg-transparent",
                          )}
                          style={{ width: "calc(100% - 8px)" }}
                          disabled={isDisabled}
                          onClick={() => handleSelect(executionProvider, model.value)}
                        >
                          {model.label}
                          {isSelected && (
                            <span className="ml-2 text-[length:var(--text-2xs)]">●</span>
                          )}
                        </button>
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
