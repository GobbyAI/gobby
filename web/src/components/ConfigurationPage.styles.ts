export const BACKEND_SECRET_MASK = '********'

export const PAGE_CLS = 'flex flex-1 flex-col overflow-hidden'
export const TOOLBAR_CLS =
  'flex min-h-11 items-center justify-between gap-3 border-b border-[var(--border)] bg-[var(--bg-secondary)] px-4 py-2 max-md:flex-wrap max-md:px-3'
export const TOOLBAR_LEFT_CLS = 'flex min-w-0 flex-[1_1_0] items-center gap-3 overflow-hidden'
export const TOOLBAR_RIGHT_CLS = 'flex shrink-0 items-center gap-2'
export const TABS_CLS =
  'flex min-w-0 gap-0.5 overflow-x-auto rounded-md bg-[var(--bg-tertiary)] p-0.5 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden'
export const TAB_CLS =
  'cursor-pointer whitespace-nowrap rounded border-0 bg-transparent px-3.5 py-1.5 text-[length:var(--text-md)] font-medium text-[var(--text-secondary)] transition-[background-color,color] duration-150 hover:bg-[rgba(255,255,255,0.05)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
export const TAB_ACTIVE_CLS = 'bg-[var(--bg-secondary)] text-[var(--text-primary)] shadow-[var(--shadow-sm)]'

export const TOOLBAR_BTN_CLS =
  'flex cursor-pointer items-center gap-1.5 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-1.5 text-[length:var(--text-sm)] text-[var(--text-secondary)] transition-[background-color,color,border-color] duration-150 hover:border-[var(--border-active)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
export const TOOLBAR_BTN_PRIMARY_CLS =
  'border-[var(--accent)] bg-[var(--accent)] text-[var(--accent-foreground)] hover:border-[var(--accent)] hover:bg-[var(--accent)] hover:text-[var(--accent-foreground)] hover:opacity-90'
export const TOOLBAR_BTN_DANGER_CLS =
  'border-[color-mix(in_srgb,var(--color-error)_20%,transparent)] text-[var(--color-error)] hover:border-[color-mix(in_srgb,var(--color-error)_40%,transparent)] hover:bg-[color-mix(in_srgb,var(--color-error)_8%,transparent)] hover:text-[var(--color-error)]'

export const CONTENT_CLS = 'flex-1 overflow-y-auto'

export const RESTART_BANNER_CLS =
  'flex items-center justify-between border-b border-[color-mix(in_srgb,var(--color-warning-foreground)_20%,transparent)] bg-[color-mix(in_srgb,var(--color-warning-foreground)_8%,transparent)] px-4 py-2.5 text-[length:var(--text-md)] text-[var(--color-warning-foreground)]'
export const RESTART_BTN_CLS =
  'cursor-pointer rounded border-0 bg-[var(--color-warning-foreground)] px-3 py-1 text-[length:var(--text-sm)] font-semibold text-[var(--text-on-warning)] pointer-coarse:min-h-11'

export const FORM_CLS = 'max-w-[800px] p-4 max-md:p-3'
export const FORM_SECTION_CLS = 'mb-5 overflow-hidden rounded-lg border border-[var(--border)]'
export const SECTION_HEADER_CLS =
  'flex w-full cursor-pointer select-none items-center justify-between border-0 border-b border-[var(--border)] bg-[var(--bg-secondary)] px-3.5 py-2.5 text-left font-[inherit] hover:bg-[var(--bg-tertiary)]'
export const SECTION_HEADER_STATIC_CLS =
  'flex select-none items-center justify-between border-b border-[var(--border)] bg-[var(--bg-secondary)] px-3.5 py-2.5'
export const SECTION_TITLE_CLS = 'text-[length:var(--text-base)] font-semibold text-[var(--text-primary)]'
export const SECTION_TOGGLE_CLS =
  'text-[length:var(--text-xs)] text-[var(--text-tertiary)] transition-transform duration-200'
export const SECTION_TOGGLE_OPEN_CLS = 'rotate-90'
export const SECTION_BODY_CLS = 'flex flex-col gap-3 px-3.5 py-3'
export const SECTION_BODY_COLLAPSED_CLS = 'hidden'

export const FORM_FIELD_CLS = 'flex flex-col gap-1'
export const FIELD_LABEL_CLS = 'text-[length:var(--text-md)] font-medium text-[var(--text-primary)]'
export const FIELD_HELP_CLS = 'text-[length:var(--text-xs)] leading-[1.4] text-[var(--text-tertiary)]'
export const INPUT_CLS =
  'rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-1.5 font-mono text-[length:var(--text-md)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)] pointer-coarse:min-h-11'
export const SELECT_CLS =
  'rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-1.5 text-[length:var(--text-md)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)] pointer-coarse:min-h-11'

export const TOGGLE_ROW_CLS = 'flex items-center justify-between py-1'
export const TOGGLE_CLS =
  'relative h-5 w-9 shrink-0 cursor-pointer rounded-[10px] border-0 bg-[var(--bg-tertiary)] transition-colors duration-200 after:absolute after:left-0.5 after:top-0.5 after:h-4 after:w-4 after:rounded-full after:bg-[var(--text-primary)] after:transition-transform after:duration-200 after:content-[""] pointer-coarse:h-11 pointer-coarse:w-[88px] pointer-coarse:rounded-[22px] pointer-coarse:after:h-10 pointer-coarse:after:w-10'
export const TOGGLE_ON_CLS = 'bg-[var(--accent)] after:translate-x-4 pointer-coarse:after:translate-x-[44px]'

export const FORM_FOOTER_CLS =
  'sticky bottom-0 flex justify-end gap-2 border-t border-[var(--border)] bg-[var(--bg-secondary)] px-4 py-3'

export const SECRET_BADGE_CLS =
  'ml-1.5 inline-block rounded-sm bg-[var(--bg-tertiary)] px-1.5 py-px align-middle text-[length:var(--text-2xs)] font-medium text-[var(--text-tertiary)]'

export const SECRETS_CLS = 'max-w-[800px] p-4 max-md:p-3'
export const SECRETS_HEADER_CLS = 'mb-4 flex items-center justify-between'
export const SECRETS_HEADER_H3_CLS = 'm-0 text-[length:var(--text-base)] font-semibold'

export const SECRETS_TABLE_CLS =
  'w-full border-collapse text-[length:var(--text-md)] max-md:text-[length:var(--text-sm)] max-sm:block [&_thead]:max-sm:hidden [&_tbody]:max-sm:block [&_tr]:max-sm:mb-2 [&_tr]:max-sm:block [&_tr]:max-sm:rounded-md [&_tr]:max-sm:border [&_tr]:max-sm:border-[var(--border)] [&_tr]:max-sm:bg-[var(--bg-secondary)] [&_tr]:max-sm:px-2.5 [&_tr]:max-sm:py-2 [&_td]:max-sm:block [&_td]:max-sm:border-b-0 [&_td]:max-sm:px-0 [&_td]:max-sm:py-1 [&_td]:max-sm:before:mb-0.5 [&_td]:max-sm:before:block [&_td]:max-sm:before:text-[length:var(--text-xs)] [&_td]:max-sm:before:uppercase [&_td]:max-sm:before:tracking-[0.5px] [&_td]:max-sm:before:text-[var(--text-tertiary)] [&_td]:max-sm:before:[content:attr(data-label)]'
export const SECRETS_TH_CLS =
  'border-b border-[var(--border)] px-2.5 py-2 text-left text-[length:var(--text-xs)] font-medium uppercase tracking-[0.5px] text-[var(--text-tertiary)] max-md:px-1.5 max-md:py-1.5'
export const SECRETS_TD_CLS = 'border-b border-[var(--border)] px-2.5 py-2 text-[var(--text-primary)] max-md:px-1.5 max-md:py-1.5'

export const SECRET_MASKED_CLS = 'text-[length:var(--text-sm)] italic text-[var(--text-tertiary)]'
export const SECRET_ACTIONS_CLS = 'flex gap-1.5 max-sm:flex-wrap'
export const SECRET_ACTION_BTN_CLS =
  'cursor-pointer rounded-sm border border-[var(--border)] bg-transparent px-2 py-0.5 text-[length:var(--text-xs)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
export const SECRET_ACTION_DELETE_CLS =
  'hover:border-[color-mix(in_srgb,var(--color-error)_40%,transparent)] hover:text-[var(--color-error)]'

export const SECRET_HINT_CLS =
  'mt-4 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-3.5 py-2.5 text-[length:var(--text-sm)] leading-[1.5] text-[var(--text-secondary)] [&_code]:rounded-sm [&_code]:bg-[var(--bg-tertiary)] [&_code]:px-1.5 [&_code]:py-px [&_code]:font-mono [&_code]:text-[length:var(--text-xs)]'

export const SECRET_FORM_CLS =
  'mb-4 flex flex-col gap-2.5 rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-3.5'
export const SECRET_FORM_ROW_CLS = 'flex gap-2.5 max-sm:flex-col [&>*]:flex-1'
export const SECRET_FORM_ACTIONS_CLS = 'flex justify-end gap-2'

export const PROMPTS_CLS = 'flex flex-1 overflow-hidden max-sm:flex-col'
export const PROMPTS_SIDEBAR_CLS =
  'flex w-[220px] min-w-[220px] flex-col overflow-y-auto border-r border-[var(--border)] bg-[var(--bg-secondary)] max-sm:w-full max-sm:min-w-0 max-sm:flex-row max-sm:overflow-x-auto max-sm:border-b max-sm:border-r-0 max-sm:border-b-[var(--border)]'
export const PROMPTS_SIDEBAR_TITLE_CLS =
  'border-b border-[var(--border)] px-3.5 py-2.5 text-[length:var(--text-sm)] font-semibold uppercase tracking-[0.5px] text-[var(--text-tertiary)] max-sm:hidden'
export const PROMPT_CATEGORY_CLS =
  'flex cursor-pointer items-center justify-between px-3.5 py-2 text-[length:var(--text-md)] text-[var(--text-secondary)] transition-[background-color,color] duration-100 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] max-sm:whitespace-nowrap max-sm:px-3 pointer-coarse:min-h-11'
export const PROMPT_CATEGORY_ACTIVE_CLS = 'bg-[var(--bg-tertiary)] font-medium text-[var(--text-primary)]'
export const PROMPT_CATEGORY_COUNT_CLS =
  'rounded-[10px] bg-[var(--bg-primary)] px-1.5 py-px text-[length:var(--text-xs)] text-[var(--text-tertiary)]'

export const PROMPTS_MAIN_CLS = 'flex flex-1 flex-col overflow-hidden'
export const PROMPTS_LIST_CLS = 'flex flex-1 flex-col gap-1.5 overflow-y-auto p-3'
export const PROMPT_CARD_CLS =
  'flex cursor-pointer items-center justify-between rounded-md border border-[var(--border)] px-3 py-2 transition-[background-color,border-color] duration-100 hover:border-[var(--border-active)] hover:bg-[var(--bg-secondary)]'
export const PROMPT_CARD_NAME_CLS = 'text-[length:var(--text-md)] font-medium text-[var(--text-primary)]'
export const PROMPT_CARD_DESC_CLS = 'mt-0.5 text-[length:var(--text-xs)] text-[var(--text-tertiary)]'

export const PROMPT_BADGE_CLS =
  'shrink-0 rounded-[10px] px-2 py-0.5 text-[length:var(--text-2xs)] font-semibold uppercase tracking-[0.3px]'
export const PROMPT_BADGE_BG: Record<'bundled' | 'overridden', string> = {
  bundled:
    'bg-[color-mix(in_srgb,var(--color-success-foreground)_8%,transparent)] text-[var(--color-success-foreground)]',
  overridden:
    'bg-[color-mix(in_srgb,var(--color-warning-foreground)_8%,transparent)] text-[var(--color-warning-foreground)]',
}

export const PROMPT_DETAIL_CLS = 'flex flex-1 flex-col overflow-hidden'
export const PROMPT_DETAIL_HEADER_CLS =
  'flex items-center justify-between border-b border-[var(--border)] px-3.5 py-2.5 max-sm:flex-col max-sm:items-start max-sm:gap-2'
export const PROMPT_DETAIL_TITLE_CLS = 'text-[length:var(--text-base)] font-semibold'
export const PROMPT_DETAIL_ACTIONS_CLS = 'flex gap-1.5'
export const PROMPT_EDITOR_CLS = 'flex-1 overflow-hidden [&_.codemirror-container]:h-full'
export const PROMPT_EMPTY_CLS = 'flex flex-1 items-center justify-center text-[length:var(--text-md)] text-[var(--text-tertiary)]'

export const YAML_CLS = 'flex flex-1 flex-col overflow-hidden'
export const YAML_EDITOR_CLS = 'flex-1 overflow-hidden [&_.codemirror-container]:h-full'
export const YAML_FOOTER_CLS =
  'flex items-center justify-between border-t border-[var(--border)] bg-[var(--bg-secondary)] px-4 py-2'
export const YAML_ERRORS_CLS = 'text-[length:var(--text-sm)] text-[var(--color-error)]'

export const EMPTY_CLS = 'flex flex-1 items-center justify-center text-[length:var(--text-base)] text-[var(--text-tertiary)]'
export const LOADING_CLS = 'flex flex-1 items-center justify-center text-[length:var(--text-md)] text-[var(--text-tertiary)]'

export const ERRORS_CLS = 'mb-3 text-[length:var(--text-sm)] text-[var(--color-error)]'
