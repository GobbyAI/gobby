import { useState, useCallback, useEffect } from "react";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from "./ui/Dialog";
import { SourceIcon } from "../shared/SourceIcon";
import { cn } from "../../lib/utils";

/** Static fallback catalog used when the daemon API is unavailable. */
const STATIC_MODELS: Record<string, { value: string; label: string }[]> = {
  claude: [
    { value: "opus", label: "Opus" },
    { value: "sonnet", label: "Sonnet" },
    { value: "haiku", label: "Haiku" },
  ],
  gemini: [
    { value: "pro", label: "Pro" },
    { value: "flash", label: "Flash" },
  ],
  codex: [{ value: "default", label: "Default" }],
};

interface ProviderModelEntry {
  provider: string;
  available: boolean;
  models: { value: string; label: string }[];
  source: "static" | "dynamic";
}

/** Fetch model catalog from daemon, falling back to static catalog. */
let _cachedModels: ProviderModelEntry[] | null = null;

async function fetchModelCatalog(): Promise<ProviderModelEntry[]> {
  if (_cachedModels) return _cachedModels;
  try {
    const res = await fetch("/api/providers/models");
    if (res.ok) {
      const data = await res.json();
      if (Array.isArray(data?.providers)) {
        _cachedModels = data.providers;
        return _cachedModels!;
      }
    }
  } catch {
    // Fall through to static
  }
  return Object.entries(STATIC_MODELS).map(([provider, models]) => ({
    provider,
    available: true,
    models,
    source: "static" as const,
  }));
}

function getModelsForProvider(
  catalog: ProviderModelEntry[],
  provider: string,
): { value: string; label: string }[] {
  const entry = catalog.find((e) => e.provider === provider);
  return entry?.models ?? STATIC_MODELS[provider] ?? [{ value: "default", label: "Default" }];
}

interface ProviderPickerProps {
  open: boolean;
  onClose: () => void;
  currentProvider: string | null;
  currentModel: string;
  availableProviders: string[];
  onModelChange: (model: string) => void;
  onProviderChange: (provider: string) => void;
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
  hasMessages,
}: ProviderPickerProps) {
  const [confirmSwitch, setConfirmSwitch] = useState<{
    provider: string;
    model: string;
  } | null>(null);
  const [catalog, setCatalog] = useState<ProviderModelEntry[]>([]);

  useEffect(() => {
    if (open) {
      fetchModelCatalog().then(setCatalog);
    }
  }, [open]);

  const effectiveProvider = currentProvider || "claude";

  const handleSelect = useCallback(
    (provider: string, model: string) => {
      const isSameProvider =
        provider === effectiveProvider ||
        (!currentProvider && provider === "claude");

      if (isSameProvider) {
        // Same provider — just switch model, no new chat needed
        onModelChange(model);
        onClose();
        setConfirmSwitch(null);
      } else if (hasMessages) {
        // Different provider with existing messages — confirm first
        setConfirmSwitch({ provider, model });
      } else {
        // Different provider but no messages — switch directly
        onModelChange(model);
        onProviderChange(provider);
        onClose();
        setConfirmSwitch(null);
      }
    },
    [
      effectiveProvider,
      currentProvider,
      hasMessages,
      onModelChange,
      onProviderChange,
      onClose,
    ],
  );

  const handleConfirm = useCallback(() => {
    if (!confirmSwitch) return;
    onModelChange(confirmSwitch.model);
    onProviderChange(confirmSwitch.provider);
    onClose();
    setConfirmSwitch(null);
  }, [confirmSwitch, onModelChange, onProviderChange, onClose]);

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
                {confirmSwitch.provider.charAt(0).toUpperCase() +
                  confirmSwitch.provider.slice(1)}
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
                Select a provider and model for this conversation
              </DialogDescription>
            </div>
            <div className="px-2 pb-2 max-h-[60vh] overflow-y-auto">
              {availableProviders.map((provider) => {
                const models = getModelsForProvider(catalog, provider);
                const isActive =
                  provider === effectiveProvider ||
                  (!currentProvider && provider === "claude");

                return (
                  <div key={provider} className="mb-1">
                    <div className="flex items-center gap-2 px-2 py-1.5 text-xs text-muted-foreground">
                      <SourceIcon source={provider} size={14} />
                      <span className="font-medium text-foreground">
                        {provider.charAt(0).toUpperCase() + provider.slice(1)}
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
