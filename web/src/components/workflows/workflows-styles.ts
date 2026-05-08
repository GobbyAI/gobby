// Shared Tailwind class constants for the workflows surface.
//
// Migrated from WorkflowsPage.css under #13899. Consumers (WorkflowsPage,
// RulesTab, AgentsTab, PipelinesTab, SkillsPage, SkillsGrid, ReportingTab,
// PipelineExecutionsView) import these names instead of relying on a side-
// effect CSS import. Mobile reflow uses Tailwind's `max-md:` breakpoint
// variant (matches the prior `@media (max-width: 768px)` rule).

// ── Page chrome ──

export const WORKFLOWS_PAGE_CLS =
  "flex-1 flex flex-col overflow-hidden px-5 max-md:px-3";

export const WORKFLOWS_TOOLBAR_CLS =
  "flex items-center justify-between gap-4 pt-4 pb-3 max-md:pt-3 max-md:pb-2 max-md:flex-wrap max-md:gap-y-2";
export const WORKFLOWS_TOOLBAR_LEFT_CLS = "flex items-center gap-3";
export const WORKFLOWS_TOOLBAR_TITLE_CLS = "text-base font-semibold m-0";
export const WORKFLOWS_TOOLBAR_COUNT_CLS =
  "bg-[var(--bg-tertiary)] text-[var(--text-secondary)] text-[length:calc(var(--font-size-base)*0.625)] px-2 py-0.5 rounded-[10px]";
export const WORKFLOWS_TOOLBAR_RIGHT_CLS =
  "flex items-center gap-2 max-md:flex-wrap max-md:gap-y-2";

// ── Tab row ──

export const WORKFLOWS_TAB_ROW_CLS =
  "flex items-end gap-3 max-md:flex-wrap";
export const WORKFLOWS_TAB_ROW_RIGHT_CLS =
  "flex items-center gap-2 ml-auto mb-1.5 max-md:flex-wrap max-md:ml-0 max-md:w-full";

// ── Filter icon button ──

export const WORKFLOWS_FILTER_ICON_BTN_CLS =
  "flex items-center justify-center w-8 h-8 border border-border rounded-md bg-[var(--bg-secondary)] text-[var(--text-secondary)] cursor-pointer transition-colors hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11 pointer-coarse:min-w-11";
export const WORKFLOWS_FILTER_ICON_BTN_ACTIVE_CLS =
  "text-[var(--accent)] border-[var(--accent)] hover:text-[var(--accent)]";

// ── Search input ──

export const WORKFLOWS_SEARCH_CLS =
  "px-2.5 py-1.5 text-[length:calc(var(--font-size-base)*0.75)] border border-border rounded-md bg-[var(--bg-secondary)] text-[var(--text-primary)] outline-none w-[200px] focus-visible:border-[var(--accent)] focus-visible:shadow-[0_0_0_2px_var(--accent-soft)] max-md:flex-1 max-md:min-w-[120px] max-md:w-auto";

// ── Toolbar / new buttons ──

export const WORKFLOWS_TOOLBAR_BTN_CLS =
  "px-2.5 py-1.5 border border-border rounded-md bg-[var(--bg-secondary)] text-[var(--text-primary)] text-[length:calc(var(--font-size-base)*0.75)] cursor-pointer transition-colors hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11";
// One-shot rotation for the refresh button click feedback. Reuses
// Tailwind's built-in `spin` keyframe (same 0deg→360deg shape) with
// 1-iteration easing so it stops after a single revolution.
export const WORKFLOWS_TOOLBAR_BTN_SPINNING_CLS =
  "animate-[spin_0.6s_ease_1]";
export const WORKFLOWS_NEW_BTN_CLS =
  "px-3 py-1.5 border-0 rounded-md bg-[var(--accent)] text-[var(--accent-foreground)] text-[length:calc(var(--font-size-base)*0.75)] font-medium cursor-pointer transition-colors hover:bg-[var(--accent-hover)] pointer-coarse:min-h-11";

// ── Overview cards ──

export const WORKFLOWS_OVERVIEW_CLS =
  "flex gap-3 pb-3 max-md:flex-wrap";
export const WORKFLOWS_OVERVIEW_CARD_CLS =
  "flex-1 px-4 py-3 bg-[var(--bg-secondary)] border border-border rounded-lg cursor-pointer transition-colors hover:border-[var(--accent)] max-md:min-w-[calc(50%-6px)]";
export const WORKFLOWS_OVERVIEW_CARD_ACTIVE_CLS =
  "border-[var(--accent)] bg-[var(--bg-active,var(--color-info-soft))]";
export const WORKFLOWS_OVERVIEW_VALUE_CLS =
  "text-[length:calc(var(--font-size-base)*1.25)] font-bold text-[var(--text-primary)]";
export const WORKFLOWS_OVERVIEW_LABEL_CLS =
  "text-[length:calc(var(--font-size-base)*0.625)] text-[var(--text-secondary)] uppercase tracking-[0.5px] mt-0.5";

// ── Filter chips ──

export const WORKFLOWS_FILTER_BAR_CLS =
  "flex items-center gap-2 pb-3 max-md:flex-wrap max-md:gap-1.5";
export const WORKFLOWS_FILTER_CHIPS_CLS = "flex gap-1.5 flex-1";
export const WORKFLOWS_FILTER_CHIP_CLS =
  "px-2.5 py-1 border border-border rounded-xl bg-transparent text-[var(--text-secondary)] text-[length:calc(var(--font-size-base)*0.625)] cursor-pointer transition-colors hover:border-[var(--text-secondary)] pointer-coarse:min-h-11";
export const WORKFLOWS_FILTER_CHIP_ACTIVE_CLS =
  "bg-[var(--accent)] border-[var(--accent)] text-[var(--accent-foreground)]";

// ── Content area ──

export const WORKFLOWS_CONTENT_CLS = "flex-1 overflow-y-auto pb-5";
export const WORKFLOWS_LOADING_CLS =
  "p-10 text-center text-[var(--text-secondary)] text-[length:calc(var(--font-size-base)*0.875)]";
export const WORKFLOWS_EMPTY_CLS = WORKFLOWS_LOADING_CLS;

// ── Card grid ──

export const WORKFLOWS_GRID_CLS =
  "grid grid-cols-[repeat(auto-fill,minmax(320px,1fr))] gap-3 max-md:grid-cols-1";
export const WORKFLOWS_CARD_CLS =
  "bg-[var(--bg-secondary)] border border-border rounded-lg p-4 transition-colors hover:border-[var(--text-muted)]";
export const WORKFLOWS_CARD_TEMPLATE_CLS = "opacity-75 border-dashed";
export const WORKFLOWS_CARD_DELETED_CLS =
  "opacity-50 border-dashed hover:opacity-70";
export const WORKFLOWS_CARD_NAME_DELETED_CLS = "line-through";

export const WORKFLOWS_CARD_HEADER_CLS =
  "flex items-center justify-between mb-2";
export const WORKFLOWS_CARD_HEADER_CLICKABLE_CLS =
  "w-full bg-none border-0 p-0 font-[inherit] text-inherit text-left cursor-pointer rounded-md hover:opacity-80 disabled:cursor-default disabled:opacity-100";
export const WORKFLOWS_CARD_NAME_CLS =
  "text-[length:calc(var(--font-size-base)*0.875)] font-semibold text-[var(--text-primary)]";

export const WORKFLOWS_CARD_TYPE_CLS =
  "text-[length:calc(var(--font-size-base)*0.625)] px-2 py-0.5 rounded-[10px] font-medium uppercase tracking-[0.5px]";
export const WORKFLOWS_CARD_TYPE_VARIANT_CLS: Record<string, string> = {
  workflow:
    "bg-[var(--color-success-soft)] text-[var(--color-success-foreground)]",
  pipeline: "bg-[var(--accent-soft)] text-[var(--accent)]",
  rule: "bg-[var(--color-error-soft)] text-[var(--color-error)]",
  agent: "bg-[var(--accent-soft)] text-[var(--accent)]",
  skill: "bg-[var(--color-info-soft)] text-[var(--color-info)]",
};

export const WORKFLOWS_CARD_DESC_CLS =
  "text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-secondary)] mb-2.5 leading-[1.4] overflow-hidden text-ellipsis [display:-webkit-box] [-webkit-line-clamp:2] [-webkit-box-orient:vertical]";

export const WORKFLOWS_CARD_BADGES_CLS = "flex flex-wrap gap-1.5 mb-3";
export const WORKFLOWS_CARD_BADGE_CLS =
  "text-[length:calc(var(--font-size-base)*0.625)] px-1.5 py-0.5 rounded bg-[var(--bg-tertiary)] text-[var(--text-secondary)]";
export const WORKFLOWS_CARD_BADGE_SOURCE_CLS =
  "bg-[var(--color-info-soft)] text-[var(--color-info)]";
export const WORKFLOWS_CARD_BADGE_PRIORITY_CLS =
  "bg-[var(--color-warning-soft)] text-[var(--color-warning-foreground)]";
export const WORKFLOWS_CARD_BADGE_DRIFT_CLS =
  "bg-[var(--color-warning-soft)] text-[var(--color-warning-foreground)] font-medium";

// ── Card footer / toggle ──

export const WORKFLOWS_CARD_FOOTER_CLS =
  "flex items-center justify-between border-t border-border pt-2.5 max-md:flex-wrap max-md:gap-2";
export const WORKFLOWS_TOGGLE_CLS =
  "flex items-center gap-1.5 cursor-pointer text-[length:calc(var(--font-size-base)*0.625)] text-[var(--text-secondary)]";
export const WORKFLOWS_TOGGLE_TRACK_CLS =
  "w-8 h-[18px] rounded-[9px] bg-[var(--bg-tertiary)] relative transition-colors";
export const WORKFLOWS_TOGGLE_TRACK_ON_CLS = "bg-[var(--accent)]";
export const WORKFLOWS_TOGGLE_KNOB_CLS =
  "w-3.5 h-3.5 rounded-full bg-[var(--text-primary)] absolute top-0.5 left-0.5 transition-transform";
export const WORKFLOWS_TOGGLE_KNOB_ON_CLS = "translate-x-3.5";

export const WORKFLOWS_CARD_ACTIONS_CLS =
  "flex gap-1 max-md:flex-wrap";

// ── Action buttons ──

export const WORKFLOWS_ACTION_BTN_CLS =
  "px-2 py-1 border border-border rounded bg-transparent text-[var(--text-secondary)] text-[length:calc(var(--font-size-base)*0.625)] cursor-pointer transition-colors hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11";
export const WORKFLOWS_ACTION_BTN_DRIFT_CLS =
  "text-[var(--color-warning-foreground)] font-medium hover:text-[var(--color-warning-foreground)] hover:bg-[color-mix(in_srgb,var(--color-warning-foreground)_10%,transparent)]";
export const WORKFLOWS_ACTION_BTN_RESTORE_CLS =
  "text-[var(--color-success-foreground)] border-[var(--color-success-foreground)] hover:bg-[var(--color-success-soft)] hover:text-[var(--color-success-foreground)] hover:border-[var(--color-success-foreground)]";
export const WORKFLOWS_ACTION_BTN_DANGER_CLS =
  "hover:bg-[var(--color-error-soft)] hover:text-[var(--color-error)] hover:border-[var(--color-error)]";

export const WORKFLOWS_ACTION_ICON_CLS =
  "flex items-center justify-center w-7 h-7 border border-border rounded bg-transparent text-[var(--text-secondary)] cursor-pointer transition-colors hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11 pointer-coarse:min-w-11";
export const WORKFLOWS_ACTION_ICON_DANGER_CLS =
  "hover:bg-[var(--color-error-soft)] hover:text-[var(--color-error)] hover:border-[var(--color-error)]";

// ── Filter button + popover ──

export const WORKFLOWS_FILTER_WRAPPER_CLS = "relative";
export const WORKFLOWS_FILTER_BTN_CLS =
  "flex items-center gap-1.5 px-2.5 py-1.5 border border-border rounded-md bg-[var(--bg-secondary)] text-[var(--text-primary)] text-[length:calc(var(--font-size-base)*0.75)] cursor-pointer transition-colors hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11";
export const WORKFLOWS_FILTER_BADGE_CLS =
  "inline-flex items-center justify-center min-w-[18px] h-[18px] px-1.5 rounded-[9px] bg-[var(--accent)] text-[var(--accent-foreground)] text-[length:calc(var(--font-size-base)*0.625)] font-semibold leading-none";

export const WORKFLOWS_FILTER_POPOVER_CLS =
  "absolute top-[calc(100%+4px)] right-0 z-50 min-w-[240px] max-w-[320px] p-3 bg-[var(--bg-secondary)] border border-border rounded-lg shadow-[var(--shadow-md)] max-md:fixed max-md:inset-auto max-md:top-auto max-md:bottom-4 max-md:left-4 max-md:right-4 max-md:min-w-0 max-md:max-w-none max-md:w-auto max-md:max-h-[70vh] max-md:overflow-y-auto";
export const WORKFLOWS_FILTER_POPOVER_SECTION_CLS = "mb-3 last:mb-0";
export const WORKFLOWS_FILTER_POPOVER_SECTION_BOTTOM_CLS =
  "border-t border-border pt-3";
export const WORKFLOWS_FILTER_POPOVER_LABEL_CLS =
  "text-[length:calc(var(--font-size-base)*0.625)] text-[var(--text-secondary)] uppercase tracking-[0.5px] mb-1.5 font-medium";
export const WORKFLOWS_FILTER_POPOVER_CHIPS_CLS = "flex flex-wrap gap-1.5";
export const WORKFLOWS_FILTER_POPOVER_CHECKBOX_CLS =
  "flex items-center gap-1.5 cursor-pointer text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-secondary)] select-none";

// ── Modals ──

export const WORKFLOWS_MODAL_OVERLAY_CLS =
  "fixed inset-0 bg-[var(--surface-scrim)] flex items-center justify-center z-[100]";
export const WORKFLOWS_MODAL_CLS =
  "bg-[var(--bg-secondary)] border border-border rounded-xl p-6 w-[480px] max-h-[80vh] overflow-y-auto max-md:w-[95vw] max-md:max-h-[90vh] max-md:p-4";
export const WORKFLOWS_MODAL_HEADING_CLS =
  "m-0 mb-4 text-lg text-[var(--text-primary)]";
export const WORKFLOWS_MODAL_FIELD_CLS = "mb-3";
export const WORKFLOWS_MODAL_FIELD_LABEL_CLS =
  "block text-sm text-[var(--text-secondary)] mb-1";
export const WORKFLOWS_MODAL_FIELD_INPUT_CLS =
  "w-full px-2.5 py-2 text-md border border-border rounded-md bg-[var(--bg-primary)] text-[var(--text-primary)] outline-none box-border focus:border-[var(--accent)]";
export const WORKFLOWS_MODAL_FIELD_TEXTAREA_CLS = `${WORKFLOWS_MODAL_FIELD_INPUT_CLS} min-h-[80px] resize-y font-mono`;
export const WORKFLOWS_MODAL_ACTIONS_CLS =
  "flex justify-end gap-2 mt-4";
export const WORKFLOWS_MODAL_CANCEL_CLS =
  "px-4 py-2 border border-border rounded-md bg-transparent text-[var(--text-secondary)] text-md cursor-pointer pointer-coarse:min-h-11";
export const WORKFLOWS_MODAL_SUBMIT_CLS =
  "px-4 py-2 border-0 rounded-md bg-[var(--accent)] text-[var(--accent-foreground)] text-md font-medium cursor-pointer hover:bg-[var(--accent-hover)] pointer-coarse:min-h-11";

// ── YAML editor modal ──

export const WORKFLOWS_YAML_MODAL_CLS =
  "bg-[var(--bg-secondary)] border border-border rounded-xl w-[800px] max-w-[90vw] h-[80vh] flex flex-col overflow-hidden max-md:w-screen max-md:max-w-[100vw] max-md:h-[90vh] max-md:rounded-t-xl max-md:rounded-b-none";
export const WORKFLOWS_YAML_HEADER_CLS =
  "flex items-center justify-between px-5 py-4 border-b border-border";
export const WORKFLOWS_YAML_HEADER_HEADING_CLS =
  "m-0 text-base text-[var(--text-primary)] font-semibold";
export const WORKFLOWS_YAML_HEADER_ACTIONS_CLS =
  "flex items-center gap-2";
export const WORKFLOWS_YAML_ERROR_CLS = "text-sm text-[var(--color-error)]";
export const WORKFLOWS_YAML_EDITOR_CLS = "flex-1 overflow-hidden";

// pipeline-edit-yaml-view used by workflows/PipelinesTab.tsx for inline YAML
// editing (not the modal). The host CodeMirror instance fills the panel.
export const PIPELINE_EDIT_YAML_VIEW_CLS = "h-full [&_.codemirror-container]:h-full";
