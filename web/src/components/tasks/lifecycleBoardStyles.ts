import type { StageRowState } from '../../lib/stageActions'

export const lifecycleBoardStyles = {
  board:
    'lifecycle-board flex min-h-0 flex-1 flex-col overflow-hidden rounded-md border border-border bg-[var(--bg-secondary)]',
  toolbar:
    'lifecycle-board__toolbar flex flex-wrap items-center justify-between gap-3 border-b border-border bg-background px-3 py-2',
  switch:
    'lifecycle-board__switch inline-flex min-h-8 items-center gap-1.5 text-sm leading-none text-muted-foreground pointer-coarse:min-h-11',
  categories:
    'lifecycle-board__categories flex flex-wrap items-center gap-2.5',
  category:
    'lifecycle-board__category inline-flex min-h-8 items-center gap-1.5 text-sm leading-none text-muted-foreground pointer-coarse:min-h-11',
  lanes:
    'lifecycle-board__lanes flex min-h-0 flex-1 flex-col overflow-auto',
  lane:
    'lifecycle-board__lane flex min-h-0 flex-col border-b border-border last:border-b-0',
  laneHeader:
    'lifecycle-board__lane-header flex items-center justify-between gap-3 bg-muted/60 px-3 py-2 text-sm font-semibold capitalize text-muted-foreground',
  lanePager:
    'sticky top-0 z-10 flex gap-1 overflow-x-auto border-b border-border bg-background/95 px-2 py-2 md:hidden',
  lanePagerButton:
    'h-8 shrink-0 rounded-md px-2 text-xs text-muted-foreground pointer-coarse:min-h-11',
  lanePagerButtonActive:
    'bg-accent/15 font-semibold text-accent',
  columns:
    'lifecycle-board__columns flex min-h-[17rem] snap-x snap-mandatory gap-3 overflow-x-auto scroll-smooth p-3 motion-reduce:scroll-auto',
  column:
    'lifecycle-column flex w-[calc(100vw-2rem)] min-w-[calc(100vw-2rem)] max-w-[calc(100vw-2rem)] snap-start flex-col overflow-hidden rounded-md border border-border bg-background md:w-[17rem] md:min-w-[17rem] md:max-w-[17rem]',
  columnOver:
    'ring-2 ring-accent ring-offset-2 ring-offset-background',
  columnHeader:
    'lifecycle-column__header grid grid-cols-[minmax(0,1fr)_auto] gap-x-2 gap-y-1 border-b border-border px-3 py-2.5',
  columnTitle:
    'lifecycle-column__title overflow-hidden text-ellipsis whitespace-nowrap text-sm font-semibold leading-tight text-foreground',
  columnCategory:
    'lifecycle-column__category text-xs capitalize leading-tight text-muted-foreground',
  columnCount:
    'lifecycle-column__count row-span-2 inline-flex min-w-6 items-center justify-center rounded-full bg-muted px-1 text-xs font-medium text-muted-foreground',
  groups:
    'lifecycle-column__groups flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto p-2',
  group:
    'lifecycle-stage-group flex flex-col gap-1.5 rounded-md bg-muted/50 p-2',
  groupHeading:
    'lifecycle-stage-group__heading flex w-full items-center justify-between gap-2 text-xs font-semibold leading-tight text-muted-foreground',
  groupSummary:
    'lifecycle-stage-group__summary flex min-h-7 w-full cursor-pointer items-center justify-between gap-2 rounded-md border-0 bg-transparent text-left text-xs font-semibold leading-tight text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent pointer-coarse:min-h-11',
  cards:
    'lifecycle-stage-group__cards flex flex-col gap-1.5',
  card:
    'lifecycle-card relative flex w-full cursor-grab flex-col gap-2 rounded-md border border-border bg-background p-2 text-left text-foreground transition-[border-color,background,box-shadow,opacity] duration-150 hover:border-accent hover:bg-muted/60 motion-reduce:transition-none',
  cardDragging:
    'lifecycle-card--dragging opacity-[0.45]',
  cardBlocked:
    'lifecycle-card--blocked bg-destructive/10',
  cardTopline:
    'lifecycle-card__topline flex items-start justify-between gap-2',
  cardOpenButton:
    'lifecycle-card__open h-auto min-h-0 justify-start px-0 py-0 text-left text-sm font-medium leading-snug hover:bg-transparent focus-visible:ring-offset-0',
  cardTitle:
    'lifecycle-card__title min-w-0 overflow-hidden text-ellipsis text-sm font-medium leading-snug text-foreground',
  blockedBadge:
    'lifecycle-card__blocked-badge shrink-0 rounded-full bg-destructive/15 px-1.5 py-0.5 text-[0.7rem] font-semibold leading-none text-destructive',
  cardMeta:
    'lifecycle-card__meta flex flex-wrap gap-1.5 text-xs capitalize leading-tight text-muted-foreground',
  cardActions:
    'lifecycle-card__actions flex items-center gap-2',
  moveSelect:
    'lifecycle-card__move min-h-8 min-w-0 flex-1 cursor-pointer rounded-md border border-border bg-muted/40 px-2 py-1 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent pointer-coarse:min-h-11',
  tooltip:
    'lifecycle-card__tooltip absolute bottom-[calc(100%+0.35rem)] right-2 z-10 max-w-56 rounded-md border border-border bg-[var(--bg-tertiary)] px-2 py-1.5 text-xs leading-snug text-[var(--text-primary)] shadow-md',
}

export const lifecycleGroupStateStyles: Record<StageRowState, string> = {
  ready: 'shadow-[inset_0_0_0_1px_var(--color-info-soft)]',
  in_progress: 'shadow-[inset_0_0_0_1px_var(--color-warning-soft)]',
  needs_review: 'shadow-[inset_0_0_0_1px_var(--color-review-soft)]',
  review_approved: 'shadow-[inset_0_0_0_1px_var(--color-review-soft)]',
  done: 'shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--text-muted)_18%,transparent)]',
}
