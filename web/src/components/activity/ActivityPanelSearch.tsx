import { memo, type ChangeEvent, type KeyboardEvent } from "react";

import { Input } from "../ui/Input";

interface ActivityPanelSearchProps {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  ariaLabel?: string;
  /** Focus the input on mount — used by the hidden-by-default search bar. */
  autoFocus?: boolean;
  /** Escape key handler — closes the toggleable search bar from the keyboard. */
  onEscape?: () => void;
}

function ActivityPanelSearchImpl({
  value,
  onChange,
  placeholder,
  ariaLabel,
  autoFocus,
  onEscape,
}: ActivityPanelSearchProps) {
  const inputLabel = ariaLabel ?? placeholder;
  const inputName =
    inputLabel
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "") || "activity-panel-search";

  return (
    <Input
      type="search"
      wrapperClassName="min-w-0 flex-1"
      className="activity-panel-search"
      value={value}
      name={inputName}
      autoComplete="off"
      data-1p-ignore
      autoFocus={autoFocus}
      onChange={(event: ChangeEvent<HTMLInputElement>) =>
        onChange(event.target.value)
      }
      onKeyDown={
        onEscape
          ? (event: KeyboardEvent<HTMLInputElement>) => {
              if (event.key === "Escape") onEscape();
            }
          : undefined
      }
      placeholder={placeholder}
      aria-label={inputLabel}
    />
  );
}

export const ActivityPanelSearch = memo(ActivityPanelSearchImpl);

interface ActivityToolbarSearchRowProps {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  ariaLabel: string;
  /** Close the bar (Escape does the same) — callers also clear the query. */
  onClose: () => void;
}

/**
 * The hidden-by-default secondary search bar toggled by the header Search
 * button. Tabs render it above their list only while search is open, so the
 * row costs no vertical space otherwise.
 */
export function ActivityToolbarSearchRow({
  value,
  onChange,
  placeholder,
  ariaLabel,
  onClose,
}: ActivityToolbarSearchRowProps) {
  return (
    <div className="activity-panel-toolbar" role="search">
      <ActivityPanelSearch
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        ariaLabel={ariaLabel}
        autoFocus
        onEscape={onClose}
      />
    </div>
  );
}
