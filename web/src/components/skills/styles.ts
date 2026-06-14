export const SOURCE_BADGE_CLS =
  'inline-flex items-center rounded px-1.5 py-px text-[length:var(--text-2xs)] font-medium uppercase tracking-[0.3px]'

export const SOURCE_BADGE_BG: Record<string, string> = {
  filesystem:
    'bg-[color-mix(in_srgb,var(--color-success-foreground)_15%,transparent)] text-[var(--color-success-foreground)]',
  github: 'bg-[color-mix(in_srgb,var(--color-info)_15%,transparent)] text-[var(--color-info)]',
  hub: 'bg-[color-mix(in_srgb,var(--color-info)_15%,transparent)] text-[var(--color-info)]',
  zip: 'bg-[color-mix(in_srgb,var(--color-warning-foreground)_15%,transparent)] text-[var(--color-warning-foreground)]',
  local: 'bg-[color-mix(in_srgb,var(--text-muted)_15%,transparent)] text-[var(--text-muted)]',
  url: 'bg-[color-mix(in_srgb,var(--color-error)_15%,transparent)] text-[var(--color-error)]',
  unknown: 'bg-[color-mix(in_srgb,var(--text-muted)_10%,transparent)] text-[var(--text-muted)]',
}

export const FORM_CANCEL_BTN_CLS =
  'cursor-pointer rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-3.5 py-1.5 text-[length:var(--text-base)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11'

export const FORM_SAVE_BTN_CLS =
  'cursor-pointer rounded border-0 bg-[var(--accent)] px-3.5 py-1.5 text-[length:var(--text-base)] font-medium text-[var(--accent-foreground)] hover:opacity-85 disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11'

export const SKILLS_PAGE_CLS =
  'flex-1 flex flex-col overflow-hidden px-5 max-md:px-3'

export const SKILLS_TOOLBAR_CLS =
  'flex items-center justify-between gap-4 pt-4 pb-3 max-md:pt-3 max-md:pb-2 max-md:flex-wrap max-md:gap-y-2'
export const SKILLS_TOOLBAR_LEFT_CLS = 'flex items-center gap-3'
export const SKILLS_TOOLBAR_TITLE_CLS = 'text-base font-semibold m-0'
export const SKILLS_TOOLBAR_RIGHT_CLS =
  'flex items-center gap-2 max-md:flex-wrap max-md:gap-y-2'

export const SKILLS_TOOLBAR_BTN_CLS =
  'px-2.5 py-1.5 border border-border rounded-md bg-[var(--bg-secondary)] text-[var(--text-primary)] text-[length:calc(var(--font-size-base)*0.75)] cursor-pointer transition-colors hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11'
export const SKILLS_NEW_BTN_CLS =
  'px-3 py-1.5 border-0 rounded-md bg-[var(--accent)] text-[var(--accent-foreground)] text-[length:calc(var(--font-size-base)*0.75)] font-medium cursor-pointer transition-colors hover:bg-[var(--accent-hover)] pointer-coarse:min-h-11'

export const SKILLS_FILTER_BAR_CLS =
  'flex items-center gap-2 pb-3 max-md:flex-wrap max-md:gap-1.5'
export const SKILLS_FILTER_WRAPPER_CLS = 'relative'
export const SKILLS_FILTER_BTN_CLS =
  'flex items-center gap-1.5 px-2.5 py-1.5 border border-border rounded-md bg-[var(--bg-secondary)] text-[var(--text-primary)] text-[length:calc(var(--font-size-base)*0.75)] cursor-pointer transition-colors hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11'
export const SKILLS_FILTER_BADGE_CLS =
  'inline-flex items-center justify-center min-w-[18px] h-[18px] px-1.5 rounded-[9px] bg-[var(--accent)] text-[var(--accent-foreground)] text-[length:calc(var(--font-size-base)*0.625)] font-semibold leading-none'

export const SKILLS_FILTER_POPOVER_CLS =
  'absolute top-[calc(100%+4px)] right-0 z-50 min-w-[240px] max-w-[320px] p-3 bg-[var(--bg-secondary)] border border-border rounded-lg shadow-[var(--shadow-md)] max-md:fixed max-md:inset-auto max-md:top-auto max-md:bottom-4 max-md:left-4 max-md:right-4 max-md:min-w-0 max-md:max-w-none max-md:w-auto max-md:max-h-[70vh] max-md:overflow-y-auto'
export const SKILLS_FILTER_POPOVER_SECTION_CLS = 'mb-3 last:mb-0'
export const SKILLS_FILTER_POPOVER_LABEL_CLS =
  'text-[length:calc(var(--font-size-base)*0.625)] text-[var(--text-secondary)] uppercase tracking-[0.5px] mb-1.5 font-medium'
export const SKILLS_FILTER_POPOVER_CHIPS_CLS = 'flex flex-wrap gap-1.5'
export const SKILLS_FILTER_CHIP_CLS =
  'px-2.5 py-1 border border-border rounded-xl bg-transparent text-[var(--text-secondary)] text-[length:calc(var(--font-size-base)*0.625)] cursor-pointer transition-colors hover:border-[var(--text-secondary)] pointer-coarse:min-h-11'
export const SKILLS_FILTER_CHIP_ACTIVE_CLS =
  'bg-[var(--accent)] border-[var(--accent)] text-[var(--accent-foreground)]'

export const SKILLS_SEARCH_CLS =
  'px-2.5 py-1.5 text-[length:calc(var(--font-size-base)*0.75)] border border-border rounded-md bg-[var(--bg-secondary)] text-[var(--text-primary)] outline-none w-[200px] focus-visible:border-[var(--accent)] focus-visible:shadow-[0_0_0_2px_var(--accent-soft)] max-md:flex-1 max-md:min-w-[120px] max-md:w-auto pointer-coarse:min-h-11'

export const SKILLS_CONTENT_CLS = 'flex-1 overflow-y-auto pb-5'
export const SKILLS_LOADING_CLS =
  'p-10 text-center text-[var(--text-secondary)] text-[length:calc(var(--font-size-base)*0.875)]'
export const SKILLS_EMPTY_CLS = SKILLS_LOADING_CLS

export const SKILLS_GRID_CLS =
  'grid grid-cols-[repeat(auto-fill,minmax(320px,1fr))] gap-3 max-md:grid-cols-1'
export const SKILLS_CARD_CLS =
  'bg-[var(--bg-secondary)] border border-border rounded-lg p-4 transition-colors hover:border-[var(--text-muted)]'
export const SKILLS_CARD_DELETED_CLS =
  'opacity-50 border-dashed hover:opacity-70'
export const SKILLS_CARD_HEADER_CLS =
  'flex items-center justify-between mb-2'
export const SKILLS_CARD_NAME_CLS =
  'text-[length:calc(var(--font-size-base)*0.875)] font-semibold text-[var(--text-primary)]'
export const SKILLS_CARD_TYPE_CLS =
  'text-[length:calc(var(--font-size-base)*0.625)] px-2 py-0.5 rounded-[10px] font-medium uppercase tracking-[0.5px]'
export const SKILLS_CARD_TYPE_VARIANT_CLS: Record<string, string> = {
  skill: 'bg-[var(--color-info-soft)] text-[var(--color-info)]',
}
export const SKILLS_CARD_DESC_CLS =
  'text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-secondary)] mb-2.5 leading-[1.4] overflow-hidden text-ellipsis [display:-webkit-box] [-webkit-line-clamp:2] [-webkit-box-orient:vertical]'
export const SKILLS_CARD_BADGES_CLS = 'flex flex-wrap gap-1.5 mb-3'
export const SKILLS_CARD_BADGE_CLS =
  'text-[length:calc(var(--font-size-base)*0.625)] px-1.5 py-0.5 rounded bg-[var(--bg-tertiary)] text-[var(--text-secondary)]'
export const SKILLS_CARD_FOOTER_CLS =
  'flex items-center justify-between border-t border-border pt-2.5 max-md:flex-wrap max-md:gap-2'
export const SKILLS_CARD_ACTIONS_CLS =
  'flex gap-1 max-md:flex-wrap'

export const SKILLS_TOGGLE_CLS =
  'flex items-center gap-1.5 cursor-pointer text-[length:calc(var(--font-size-base)*0.625)] text-[var(--text-secondary)]'
export const SKILLS_TOGGLE_TRACK_CLS =
  'w-8 h-[18px] rounded-[9px] bg-[var(--bg-tertiary)] relative transition-colors'
export const SKILLS_TOGGLE_TRACK_ON_CLS = 'bg-[var(--accent)]'
export const SKILLS_TOGGLE_KNOB_CLS =
  'w-3.5 h-3.5 rounded-full bg-[var(--text-primary)] absolute top-0.5 left-0.5 transition-transform'
export const SKILLS_TOGGLE_KNOB_ON_CLS = 'translate-x-3.5'

export const SKILLS_ACTION_BTN_CLS =
  'px-2 py-1 border border-border rounded bg-transparent text-[var(--text-secondary)] text-[length:calc(var(--font-size-base)*0.625)] cursor-pointer transition-colors hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
export const SKILLS_ACTION_BTN_RESTORE_CLS =
  'text-[var(--color-success-foreground)] border-[var(--color-success-foreground)] hover:bg-[var(--color-success-soft)] hover:text-[var(--color-success-foreground)] hover:border-[var(--color-success-foreground)]'
export const SKILLS_ACTION_ICON_CLS =
  'flex items-center justify-center w-7 h-7 border border-border rounded bg-transparent text-[var(--text-secondary)] cursor-pointer transition-colors hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11 pointer-coarse:min-w-11'
export const SKILLS_ACTION_ICON_DANGER_CLS =
  'hover:bg-[var(--color-error-soft)] hover:text-[var(--color-error)] hover:border-[var(--color-error)]'
