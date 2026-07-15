import { inputFocusCls } from '../../shared/focusStyles'

export const EDITOR_CLS = 'flex h-full flex-1 flex-col overflow-hidden'
export const EDITOR_SIDEBAR_CLS = '!h-auto !overflow-visible'

export const TOOLBAR_CLS =
  'flex flex-shrink-0 items-center justify-between gap-4 border-b border-[var(--border)] bg-[var(--bg-secondary)] px-4 py-2.5'
export const TOOLBAR_LEFT_CLS = 'flex items-center gap-2.5'
export const TOOLBAR_RIGHT_CLS = 'flex items-center gap-2'

export const BACK_CLS =
  'cursor-pointer rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] px-3 py-1.5 text-[length:var(--text-base)] text-[var(--text-primary)] transition-colors duration-150 hover:bg-[var(--border)] pointer-coarse:min-h-11'

export const NAME_CLS =
  `w-[240px] cursor-text rounded-md border border-transparent bg-transparent px-2.5 py-1 text-[length:var(--text-base)] font-semibold text-[var(--text-primary)] transition-colors duration-150 hover:bg-[var(--bg-tertiary)] focus:bg-[var(--bg-primary)] ${inputFocusCls}`

export const BADGE_CLS =
  'inline-block rounded-[10px] bg-[var(--accent-soft)] px-2 py-0.5 text-[length:var(--text-2xs)] font-medium uppercase tracking-[0.5px] text-[var(--accent)]'

export const BTN_CLS =
  'cursor-pointer rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] px-3 py-1.5 text-[length:var(--text-sm)] text-[var(--text-primary)] transition-colors duration-150 hover:bg-[var(--border)] disabled:cursor-not-allowed disabled:opacity-60 pointer-coarse:min-h-11'

export const BTN_PRIMARY_CLS =
  'border-[var(--accent)] bg-[var(--accent)] font-medium text-[var(--accent-foreground)] hover:border-[var(--accent-hover)] hover:bg-[var(--accent-hover)]'

export const META_CLS = 'flex-shrink-0 border-b border-[var(--border)] px-4 py-3'

export const LABEL_CLS =
  'mb-1 block text-[length:var(--text-xs)] font-semibold uppercase tracking-[0.5px] text-[var(--text-secondary)]'

export const DESCRIPTION_CLS =
  `box-border min-h-[40px] w-full resize-y rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-2 font-[inherit] text-[length:var(--text-md)] text-[var(--text-primary)] ${inputFocusCls}`

export const STEPS_CLS = 'flex-1 overflow-y-auto px-4 pt-3 pb-5'
export const STEPS_SIDEBAR_CLS = '!overflow-visible !pb-0'

export const SECTION_HEADER_CLS =
  'mb-2.5 flex items-center gap-2 text-[length:var(--text-xs)] font-semibold uppercase tracking-[0.5px] text-[var(--text-secondary)]'

export const STEP_COUNT_CLS =
  'rounded-[10px] bg-[var(--bg-tertiary)] px-1.5 py-px text-[length:var(--text-2xs)] text-[var(--text-secondary)]'

export const EMPTY_CLS = 'p-6 text-center text-[length:var(--text-md)] text-[var(--text-secondary)]'

export const SAVE_ERROR_CLS =
  'mx-3 mb-1 rounded-md bg-[color-mix(in_srgb,var(--color-error)_12%,transparent)] px-3 py-2 text-[length:var(--text-sm)] text-[var(--color-error)]'

export const STEP_CLS =
  'mb-2 overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)]'

export const STEP_HEADER_CLS =
  'flex w-full cursor-pointer items-center gap-2 border-0 bg-transparent px-3 py-2.5 text-left transition-colors duration-100 hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11'

export const TYPE_BADGE_CLS =
  'inline-block flex-shrink-0 rounded-[10px] px-2 py-0.5 text-[length:var(--text-2xs)] font-medium'

export const STEP_ID_CLS =
  'flex-shrink-0 text-[length:var(--text-md)] font-medium text-[var(--text-primary)]'

export const STEP_PREVIEW_CLS =
  'min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-[length:var(--text-sm)] text-[var(--text-secondary)]'

export const STEP_CHEVRON_CLS =
  'ml-auto flex-shrink-0 text-[length:var(--text-sm)] text-[var(--text-secondary)]'

export const STEP_BODY_CLS = 'border-t border-[var(--border)] px-3 pb-3'

export const STEP_ACTIONS_CLS = 'flex gap-1.5 py-2'

export const STEP_ACTION_CLS =
  'cursor-pointer rounded border border-[var(--border)] bg-transparent px-2.5 py-1 text-[length:var(--text-xs)] text-[var(--text-secondary)] transition-[background-color,color,border-color,opacity] duration-150 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] disabled:cursor-not-allowed disabled:opacity-40 pointer-coarse:min-h-11'

export const STEP_ACTION_DANGER_CLS =
  'hover:border-[var(--color-destructive)] hover:bg-[var(--color-destructive)] hover:text-[var(--color-destructive-foreground)]'

export const FIELD_CLS = 'mb-2.5'

export const FIELD_LABEL_CLS =
  'mb-1 block text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]'

export const FIELD_INPUT_CLS =
  `box-border w-full rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1.5 text-[length:var(--text-md)] text-[var(--text-primary)] ${inputFocusCls}`

export const FIELD_TEXTAREA_CLS = `${FIELD_INPUT_CLS} min-h-[50px] resize-y font-[inherit]`

export const FIELD_TEXTAREA_MONO_CLS = `${FIELD_TEXTAREA_CLS} font-mono text-[length:var(--text-sm)]`

export const FIELD_SELECT_CLS = `${FIELD_INPUT_CLS} cursor-pointer`

export const CHECKBOX_LABEL_CLS =
  'flex cursor-pointer items-center gap-1.5 text-[length:var(--text-sm)] [&>input]:w-auto'

export const COMMON_CLS = 'mt-2 border-t border-[var(--border)] pt-2'

export const KV_CLS = 'flex flex-col gap-1'
export const KV_ROW_CLS = 'flex items-center gap-1'
export const KV_INPUT_CLS =
  `box-border flex-1 rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1 text-[length:var(--text-sm)] text-[var(--text-primary)] ${inputFocusCls}`

export const KV_REMOVE_CLS =
  'flex-shrink-0 cursor-pointer rounded border border-[var(--border)] bg-transparent px-1.5 py-0.5 text-[length:var(--text-base)] leading-none text-[var(--text-secondary)] hover:border-[var(--color-destructive)] hover:bg-[var(--color-destructive)] hover:text-[var(--color-destructive-foreground)] pointer-coarse:min-h-11 pointer-coarse:min-w-11'

export const KV_ADD_CLS =
  'cursor-pointer rounded border border-dashed border-[var(--border)] bg-transparent px-2 py-1 text-left text-[length:var(--text-xs)] text-[var(--text-secondary)] hover:border-[var(--accent)] hover:text-[var(--text-primary)]'

export const ADD_CLS = 'relative mt-2'

export const ADD_BTN_CLS =
  'w-full cursor-pointer rounded-lg border border-dashed border-[var(--border)] bg-transparent p-2.5 text-[length:var(--text-md)] text-[var(--text-secondary)] transition-[background-color,color,border-color] duration-150 hover:border-[var(--accent)] hover:bg-[var(--bg-secondary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'

export const ADD_DROPDOWN_CLS =
  'absolute bottom-full left-0 z-10 mb-1 min-w-[160px] rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-1 shadow-[var(--shadow-md)]'

export const ADD_OPTION_CLS =
  'flex w-full cursor-pointer items-center gap-2 rounded-md border-0 bg-transparent px-2.5 py-2 text-left text-[length:var(--text-md)] text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11'

export const ADD_DOT_CLS = 'h-2 w-2 flex-shrink-0 rounded-full'
