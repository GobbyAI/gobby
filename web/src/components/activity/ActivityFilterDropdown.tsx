import { useEffect, useRef } from "react";

export interface ActivityFilterOption<T extends string> {
  value: T;
  label: string;
}

interface ActivityFilterDropdownProps<T extends string> {
  value: T;
  options: readonly ActivityFilterOption<T>[];
  onChange: (value: T) => void;
  onClose: () => void;
  ariaLabel: string;
}

export function ActivityFilterDropdown<T extends string>({
  value,
  options,
  onChange,
  onClose,
  ariaLabel,
}: ActivityFilterDropdownProps<T>) {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  return (
    <>
      <div
        className="fixed inset-0 z-[99]"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        ref={panelRef}
        className="absolute top-full right-2 z-[100] w-[min(220px,calc(100vw-1.5rem))] border border-border rounded-md shadow-xl flex flex-col p-1"
        style={{ background: "var(--bg-secondary)" }}
        role="listbox"
        aria-label={ariaLabel}
      >
        {options.map((option) => {
          const isActive = option.value === value;
          return (
            <button
              key={option.value}
              type="button"
              role="option"
              aria-selected={isActive}
              className={
                isActive
                  ? "activity-filter-dropdown__item activity-filter-dropdown__item--active"
                  : "activity-filter-dropdown__item"
              }
              onClick={() => {
                onChange(option.value);
                onClose();
              }}
            >
              {option.label}
            </button>
          );
        })}
      </div>
    </>
  );
}
