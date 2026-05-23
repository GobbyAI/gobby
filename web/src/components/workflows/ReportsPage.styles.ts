export const PAGE_CLS =
  "reports-page flex flex-1 flex-col overflow-hidden px-6 py-4 max-md:p-3";

export const TOOLBAR_CLS =
  "flex flex-wrap items-center justify-between gap-4 border-b border-[var(--border)] pb-3 mb-2 max-md:flex-col max-md:items-stretch max-md:gap-2";

export const TOOLBAR_LEFT_CLS =
  "flex flex-wrap items-center gap-2 max-md:justify-between max-sm:flex-col max-sm:items-stretch";

export const TOOLBAR_RIGHT_CLS =
  "flex items-center gap-2 max-md:flex-col max-md:gap-2";

export const TITLE_CLS =
  "text-[length:calc(var(--font-size-base)*1.1)] font-semibold mr-1";

export const SEARCH_CLS =
  "w-[180px] rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] px-2.5 py-1.5 font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:border-[var(--accent)] focus:outline-none max-md:w-full";

export const FILTER_BAR_CLS =
  "flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] py-2 mb-2 max-md:flex-col max-md:items-stretch";

export const FILTER_CHIPS_CLS =
  "flex flex-wrap gap-1.5 max-md:flex-nowrap max-md:overflow-x-auto max-md:pb-0.5";

export const STAT_CHIP_BASE_CLS =
  "inline-flex cursor-pointer items-center gap-1.5 rounded-full border border-[var(--border)] bg-transparent px-2.5 py-0.5 font-[inherit] text-[length:var(--text-sm)] text-[var(--text-secondary)] transition-colors duration-150 hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11";

export const STAT_CHIP_ACTIVE_CLS =
  "bg-[var(--bg-tertiary)] border-[var(--accent)] text-[var(--text-primary)]";

export const TABLE_CONTAINER_CLS =
  "flex-1 overflow-y-auto max-sm:overflow-x-visible";

export const TABLE_CLS =
  "reports-table w-full border-collapse text-[length:calc(var(--font-size-base)*0.85)]";

export const TH_BASE_CLS =
  "reports-th sticky top-0 z-[1] border-b border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-2 text-left text-[length:calc(var(--font-size-base)*0.7)] font-medium uppercase tracking-[0.05em] text-[var(--text-muted)]";

export const TH_SORTABLE_CLS =
  "cursor-pointer select-none whitespace-nowrap hover:text-[var(--text-primary)]";

export const TH_ID_CLS = "whitespace-nowrap max-md:hidden";

export const ROW_BASE_CLS =
  "cursor-pointer transition-colors duration-100 hover:bg-[var(--bg-tertiary)]";

export const ROW_SELECTED_CLS =
  "bg-[color-mix(in_srgb,var(--accent)_8%,transparent)] hover:bg-[color-mix(in_srgb,var(--accent)_8%,transparent)]";

export const CELL_BASE_CLS =
  "reports-cell border-b border-[var(--border)] px-2.5 py-2 whitespace-nowrap";

export const CELL_NAME_CLS = "whitespace-normal break-words";

export const CELL_ID_CLS =
  "reports-cell--id font-[inherit] text-[length:var(--text-sm)] text-[var(--text-muted)] max-md:hidden";

export const CELL_STATUS_CLS =
  "capitalize text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-secondary)]";

export const CELL_DURATION_CLS =
  "reports-cell--duration font-[inherit] text-[length:var(--text-sm)] text-[var(--text-muted)] max-md:hidden";

export const CELL_TIME_CLS =
  "text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-secondary)] max-md:text-[length:calc(var(--font-size-base)*0.7)]";

export const STATUS_TEXT_CLS = CELL_STATUS_CLS;

export const LOADING_EMPTY_CLS =
  "flex flex-1 items-center justify-center text-[length:calc(var(--font-size-base)*0.9)] text-[var(--text-muted)]";

export const TYPE_BADGE_BASE_CLS =
  "reports-type-badge inline-block rounded px-1.5 py-0.5 text-[length:calc(var(--font-size-base)*0.7)] font-medium";

export const TYPE_BADGE_AGENT_CLS =
  "reports-type-badge--agent bg-[var(--accent-soft)] text-[var(--accent)]";

export const DETAIL_BACKDROP_CLS =
  "fixed inset-0 z-[90] bg-[var(--surface-scrim)]";

export const DETAIL_PANEL_BASE_CLS =
  "fixed top-0 right-0 z-[100] flex h-full max-w-[90vw] translate-x-full flex-col overflow-y-auto border-l border-[var(--border)] bg-[var(--bg-secondary)] transition-transform duration-[0.25s] ease max-md:!w-screen max-md:!max-w-screen";

export const DETAIL_PANEL_OPEN_CLS = "translate-x-0";

export const DETAIL_RESIZE_HANDLE_CLS =
  "absolute top-0 left-[-3px] z-[101] h-full w-[6px] cursor-col-resize transition-colors duration-150 hover:bg-[var(--accent)] hover:opacity-50 active:bg-[var(--accent)] active:opacity-50 max-md:hidden";

export const DETAIL_HEADER_CLS = "border-b border-[var(--border)] px-5 py-4";

export const DETAIL_HEADER_TOP_CLS = "mb-2 flex items-center justify-between";

export const DETAIL_ID_CLS =
  "font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-muted)]";

export const DETAIL_CLOSE_CLS =
  "flex h-8 w-8 cursor-pointer items-center justify-center rounded border-0 bg-transparent text-[var(--text-muted)] transition-colors duration-150 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11 pointer-coarse:min-w-11";

export const DETAIL_TITLE_CLS =
  "my-1 text-[length:calc(var(--font-size-base)*1.05)] font-semibold";

export const DETAIL_STATUS_CLS = "mt-1 inline-flex items-center gap-1.5";

export const DETAIL_TRIGGER_CLS =
  "flex items-center gap-1.5 text-[length:calc(var(--font-size-base)*0.85)] text-[var(--text-secondary)]";

export const DETAIL_BODY_CLS = "flex flex-1 flex-col gap-4 px-5 py-4";

export const DETAIL_SECTION_CLS = "flex flex-col gap-1.5";

export const DETAIL_LABEL_CLS =
  "text-[length:calc(var(--font-size-base)*0.7)] font-medium uppercase tracking-[0.05em] text-[var(--text-muted)]";

export const DETAIL_VALUE_CLS =
  "text-[length:calc(var(--font-size-base)*0.85)] text-[var(--text-primary)]";

export const DETAIL_MONO_CLS =
  "font-[inherit] text-[length:calc(var(--font-size-base)*0.8)]";

export const DETAIL_CODE_CLS =
  "reports-detail-code max-h-[300px] overflow-x-auto overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] p-3 font-[inherit] text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-secondary)]";

export const DETAIL_TOGGLE_CLS =
  "flex cursor-pointer items-center gap-1.5 border-0 bg-transparent p-0 font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-secondary)] hover:text-[var(--text-primary)]";

export const DETAIL_STATS_CLS =
  "grid grid-cols-3 gap-2 max-md:grid-cols-2 max-sm:grid-cols-1";

export const DETAIL_STAT_CLS =
  "reports-detail-stat flex flex-col gap-0.5 rounded-md bg-[var(--bg-tertiary)] p-2";

export const DETAIL_STAT_LABEL_CLS =
  "text-[length:calc(var(--font-size-base)*0.65)] uppercase tracking-[0.03em] text-[var(--text-muted)]";

export const DETAIL_STAT_VALUE_CLS =
  "font-[inherit] text-[length:calc(var(--font-size-base)*0.9)] font-semibold text-[var(--text-primary)]";

export const DETAIL_TAG_CLS =
  "reports-detail-tag inline-flex items-center rounded px-2 py-0.5 text-[length:calc(var(--font-size-base)*0.7)] font-medium bg-[var(--bg-tertiary)] text-[var(--text-secondary)]";

export const DETAIL_TAGS_CLS = "flex flex-wrap gap-1.5";

export const DETAIL_STEPS_CLS = "flex flex-col gap-1";

export const APPROVAL_CLS =
  "flex items-center justify-between gap-3 rounded-md border border-[color-mix(in_srgb,var(--color-warning-foreground)_30%,transparent)] bg-[color-mix(in_srgb,var(--color-warning-foreground)_8%,transparent)] p-3";

export const APPROVAL_MESSAGE_CLS =
  "flex items-center gap-1.5 text-[length:calc(var(--font-size-base)*0.85)] text-[var(--color-warning-foreground)]";

export const APPROVAL_ACTIONS_CLS = "flex gap-2";

export const BTN_BASE_CLS =
  "cursor-pointer rounded-md border-0 px-3 py-1.5 font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] font-medium transition-opacity duration-150 disabled:cursor-default disabled:opacity-50 pointer-coarse:min-h-11";

export const BTN_APPROVE_CLS =
  "bg-[var(--color-success-foreground)] text-[var(--text-on-success)]";

export const BTN_REJECT_CLS = "bg-[var(--color-error)] text-[var(--text-on-error)]";

export const DETAIL_ERROR_CLS =
  "rounded-md border border-[color-mix(in_srgb,var(--color-error)_30%,transparent)] bg-[color-mix(in_srgb,var(--color-error)_8%,transparent)] p-3 text-[length:calc(var(--font-size-base)*0.85)] text-[var(--color-error)]";

export const GROUP_TOGGLE_CLS = "flex items-center gap-1.5";

export const GROUP_LABEL_CLS =
  "whitespace-nowrap text-[length:var(--text-sm)] text-[var(--text-muted)]";

export const GROUP_SELECT_CLS =
  "cursor-pointer rounded border border-[var(--border)] bg-[var(--bg-tertiary)] px-1.5 py-0.5 font-[inherit] text-[length:var(--text-sm)] text-[var(--text-primary)] focus:border-[var(--accent)] focus:outline-none";

export const GROUP_CLS = "mb-3";

export const GROUP_HEADER_CLS =
  "sticky top-0 z-[2] border-b border-[var(--border)] bg-[var(--bg-primary)] px-2.5 pt-2 pb-1 text-[length:calc(var(--font-size-base)*0.8)] font-semibold text-[var(--text-secondary)]";

export const GROUP_COUNT_CLS =
  "font-normal text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)]";
