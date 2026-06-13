import type { ReactNode } from "react";

export interface FieldOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface DraftFieldBaseProps {
  label: string;
  disabled?: boolean;
  placeholder?: string;
  ariaLabel: string;
}

export interface DetailPaneHeaderProps {
  title: ReactNode;
  dirty: boolean;
  onSave: () => void | Promise<void>;
  onDiscard: () => void;
  saving?: boolean;
  serverChanged?: boolean;
  actions?: ReactNode;
}
