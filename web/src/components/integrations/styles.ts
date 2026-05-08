// Shared Tailwind class strings for integrations/* components.

export const TYPE_BADGE_CLS =
  'whitespace-nowrap rounded-[10px] px-2 py-0.5 text-[length:var(--text-2xs)] font-medium'

export const STATUS_DOT_CLS = 'inline-block h-2 w-2 shrink-0 rounded-full'
export const STATUS_DOT_ACTIVE_COLOR = 'var(--color-success-foreground)'
export const STATUS_DOT_INACTIVE_COLOR = 'var(--text-secondary)'
export const STATUS_DOT_ERROR_COLOR = 'var(--color-error)'

export const TABS_CLS = 'mb-3 flex border-b border-[var(--border)]'
export const TAB_CLS =
  'cursor-pointer border-0 border-b-2 border-transparent bg-transparent px-4 py-2 text-[length:var(--text-sm)] font-medium text-[var(--text-secondary)] transition-colors duration-150 hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
export const TAB_ACTIVE_CLS = 'border-[var(--accent)] text-[var(--accent)]'

export const FILTER_BAR_CLS = 'pb-3'
export const FILTER_CHIPS_CLS = 'flex flex-wrap gap-1.5'
export const FILTER_CHIP_CLS =
  'cursor-pointer rounded-xl border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-1 text-[length:var(--text-2xs)] text-[var(--text-secondary)] transition-[background-color,color,border-color] duration-150 hover:bg-[rgba(255,255,255,0.05)] pointer-coarse:min-h-11 pointer-coarse:px-3'
export const FILTER_CHIP_ACTIVE_CLS =
  'border-[var(--accent)] bg-[var(--accent)] text-[var(--accent-foreground)]'
export const MESSAGE_FILTER_BAR_CLS = 'flex gap-2 pb-3'
export const FILTER_SELECT_CLS =
  'rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-1.5 text-[length:var(--text-sm)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)] pointer-coarse:min-h-11'
export const LOADING_CLS =
  'flex flex-1 items-center justify-center text-[length:var(--text-sm)] text-[var(--text-secondary)]'

export const MODAL_OVERLAY_CLS =
  'fixed inset-0 z-[900] flex items-center justify-center bg-[var(--surface-scrim)]'
export const MODAL_CLS =
  'flex max-h-[85vh] w-[90vw] max-w-[520px] flex-col rounded-xl border border-[var(--border)] bg-[var(--bg-primary)] [box-shadow:var(--shadow-lg)]'
export const MODAL_HEADER_CLS =
  'flex items-center justify-between border-b border-[var(--border)] px-5 py-4'
export const MODAL_HEADER_TITLE_CLS = 'm-0 text-[length:var(--text-base)] font-semibold'
export const MODAL_CLOSE_CLS =
  'cursor-pointer border-0 bg-transparent px-2 py-1 text-[1.2em] text-[var(--text-secondary)] hover:text-[var(--text-primary)] pointer-coarse:h-11 pointer-coarse:w-11'
export const MODAL_BODY_CLS = 'flex-1 overflow-y-auto px-5 py-4'
export const MODAL_FOOTER_CLS =
  'flex justify-end gap-2 border-t border-[var(--border)] px-5 py-3'

export const FORM_FIELD_CLS = 'mb-3.5'
export const FORM_LABEL_CLS =
  'mb-1 block text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]'
export const FORM_INPUT_CLS =
  'box-border w-full rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-2 text-[length:var(--text-sm)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-60 pointer-coarse:min-h-11'
export const FORM_REQUIRED_CLS = 'text-[var(--color-error)]'
export const FORM_HELP_CLS = 'mb-3 text-[length:var(--text-xs)] text-[var(--text-secondary)]'
export const FORM_ERROR_CLS =
  'mb-3 rounded-md bg-[var(--color-error-soft)] px-3 py-2 text-[length:var(--text-xs)] text-[var(--color-error)]'

export const FORM_CANCEL_CLS =
  'cursor-pointer rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-4 py-2 text-[length:var(--text-sm)] text-[var(--text-primary)] transition-colors duration-150 hover:bg-[rgba(255,255,255,0.05)] disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11'
export const FORM_SUBMIT_CLS =
  'cursor-pointer rounded-md border-0 bg-[var(--accent)] px-4 py-2 text-[length:var(--text-sm)] font-medium text-[var(--accent-foreground)] transition-opacity duration-150 hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11'

export const FORM_CHANGE_BTN_CLS =
  'cursor-pointer rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-0.5 text-[length:var(--text-2xs)] text-[var(--text-primary)] transition-colors duration-150 hover:border-[var(--accent)] pointer-coarse:min-h-11 pointer-coarse:px-3'

export const EMPTY_CARD_CLS =
  'flex cursor-pointer items-center gap-2.5 rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] px-4 py-3 text-[length:var(--text-sm)] font-medium text-[var(--text-primary)] transition-colors duration-150 hover:border-[var(--accent)] hover:bg-[rgba(255,255,255,0.05)] pointer-coarse:min-h-11'
