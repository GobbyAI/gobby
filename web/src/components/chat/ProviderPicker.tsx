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
  const visibleProviders = getOrderedProviders(
    availableProviders.length > 0 ? availableProviders : [effectiveProvider],
  );

  const handleSelect = useCallback(
    (provider: string, model: string) => {
      if (onSelect) {
        onSelect(provider, model);
        onClose();
        setConfirmSwitch(null);
        return;
      }

      const isSameProvider =
        provider === effectiveProvider ||
        (!currentProvider && provider === "claude");
      const requiresFreshConversation =
        hasMessages &&
        isSameProvider &&
        model !== currentModel &&
        (model === "local" || currentModel === "local");

      if (isSameProvider && !requiresFreshConversation) {
        // Same provider — just switch model, no new chat needed
        onModelChange(model);
        onClose();
        setConfirmSwitch(null);
      } else if (hasMessages) {
        // Different provider with existing messages — confirm first
        setConfirmSwitch({ provider, model });
      } else {
        // Different provider but no messages — switch directly
        onProviderChange(provider);
        onModelChange(model);
        onSwitchProvider?.(provider);
        onClose();
        setConfirmSwitch(null);
      }
    },
    [
      effectiveProvider,
      currentProvider,
      currentModel,
      hasMessages,
      onModelChange,
      onProviderChange,
      onSwitchProvider,
      onSelect,
      onClose,
    ],
  );

  const handleConfirm = useCallback(() => {
    if (!confirmSwitch) return;
    onProviderChange(confirmSwitch.provider);
    onModelChange(confirmSwitch.model);
    onSwitchProvider?.(confirmSwitch.provider);
    onClose();
    setConfirmSwitch(null);
  }, [confirmSwitch, onModelChange, onProviderChange, onSwitchProvider, onClose]);

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
                const models = getVisibleModelsForProvider(
                  catalog,
                  provider,
                  currentModel,
                );
                const isActive =
                  provider === effectiveProvider ||
                  (!currentProvider && provider === "claude");

                return (
                  <div key={provider} className="mb-1">
                    <div className="flex items-center gap-2 px-2 py-1.5 text-xs text-muted-foreground">
                      <SourceIcon source={provider} size={14} />
                      <span className="font-medium text-foreground">
                        {getProviderDisplayName(provider)}
                      </span>
                      {isActive && (
                        <span className="text-[10px] px-1 py-0.5 rounded bg-accent/20 text-accent">
                          active
                        </span>
                      )}
                    </div>
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
                          )}
                          style={{ width: "calc(100% - 8px)" }}
                          onClick={() => handleSelect(provider, model.value)}
                        >
                          {model.label}
                          {isSelected && (
                            <span className="ml-2 text-[10px]">●</span>
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
