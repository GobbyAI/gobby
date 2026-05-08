import type { BadgeProps } from '../chat/ui/Badge'

export const dashboardPageClass =
  'flex flex-1 flex-col overflow-hidden px-3 md:px-5'

export const dashboardToolbarClass =
  'flex flex-col gap-3 py-4 md:flex-row md:items-center md:justify-between md:pb-3'

export const dashboardToolbarControlsClass =
  'flex flex-wrap items-center gap-3'

export const dashboardToolbarUpdatedClass =
  'text-[11px] text-muted-foreground'

export const dashboardContentClass =
  'flex-1 overflow-y-auto pb-5'

export const dashboardGridClass =
  'grid grid-cols-1 gap-3 lg:grid-cols-3'

export const dashboardCardClass =
  '@container overflow-hidden rounded-lg border border-border bg-[var(--bg-secondary)]'

export const dashboardFullCardClass =
  'lg:col-span-3'

export const dashboardCardHeaderClass =
  'flex items-start justify-between gap-3 px-4 pb-2 pt-3'

export const dashboardCardTitleClass =
  'm-0 text-[11px] font-semibold uppercase tracking-[0.05em] text-muted-foreground'

export const dashboardCardBodyClass =
  'px-4 pb-4'

export const dashboardCardBodyCenteredClass =
  'flex flex-col items-center gap-4 @sm:flex-row'

export const dashboardCardBodyRowClass =
  'flex flex-col items-start gap-4 @sm:flex-row'

export const dashboardStatGridClass =
  'grid grid-cols-2 gap-3'

export const dashboardSingleStatGridClass =
  'grid grid-cols-1 gap-3'

export const dashboardStatClass =
  'flex flex-col'

export const dashboardStatValueClass =
  'text-[20px] font-semibold leading-[1.2] text-foreground'

export const dashboardStatLabelClass =
  'mt-0.5 text-[11px] text-muted-foreground'

export const dashboardBigStatClass =
  'mb-2 text-[28px] font-semibold leading-none text-foreground'

export const dashboardMetaTextClass =
  'mb-3 text-[11px] text-muted-foreground'

export const dashboardBreakdownClass =
  'mt-3 flex flex-col gap-1 border-t border-border pt-2.5'

export const dashboardBreakdownRowClass =
  'flex items-center justify-between gap-3 text-xs'

export const dashboardBreakdownLabelClass =
  'text-muted-foreground'

export const dashboardBreakdownMonoLabelClass =
  'font-mono text-[11px] text-muted-foreground'

export const dashboardBreakdownValueClass =
  'font-medium text-foreground'

export const dashboardDonutLayoutClass =
  'flex items-center gap-4'

export const dashboardLegendClass =
  'flex flex-1 flex-col gap-1.5'

export const dashboardLegendRowClass =
  'flex items-center gap-2 text-xs'

export const dashboardDotClass =
  'size-2 shrink-0 rounded-full'

export const dashboardLegendLabelClass =
  'flex-1 text-muted-foreground'

export const dashboardLegendValueClass =
  'font-medium text-foreground'

export const dashboardStatusListClass =
  'flex min-w-0 flex-1 flex-col gap-1.5'

export const dashboardStatusRowClass =
  'flex items-center gap-2 text-xs'

export const dashboardStatusRowDimmedClass =
  'opacity-60'

export const dashboardStatusRowLabelClass =
  'flex-1 truncate text-muted-foreground'

export const dashboardStatusRowValueClass =
  'min-w-8 text-right font-medium text-foreground'

export const dashboardLoadingClass =
  'flex items-center justify-center px-10 py-10 text-sm text-muted-foreground'

export const dashboardErrorClass =
  'flex items-center justify-center px-10 py-10 text-sm text-destructive-foreground'

export const dashboardChartGridClass =
  'grid grid-cols-1 gap-4 lg:grid-cols-2'

export const dashboardChartCellClass =
  'min-h-[180px]'

export const dashboardChartLabelClass =
  'mb-2 text-[11px] font-medium text-muted-foreground'

export const dashboardChartEmptyClass =
  'flex h-40 items-center justify-center text-center text-xs text-muted-foreground/60'

export const dashboardHealthHeaderClass =
  'mb-2.5 flex items-center gap-2 text-sm font-medium text-foreground'

export const dashboardHealthGridClass =
  'flex flex-col gap-1.5'

export const dashboardHealthRowClass =
  'flex items-center gap-2 text-xs'

export const dashboardHealthNameClass =
  'flex-1 truncate text-foreground'

export const dashboardServicesClass =
  'mt-3 flex flex-col gap-1.5 border-t border-border pt-2.5'

export const dashboardServiceRowClass =
  'flex items-center gap-2 text-xs text-muted-foreground'

export const dashboardToggleButtonClass =
  'mt-2 block w-full bg-transparent py-1.5 text-left text-[11px] text-muted-foreground transition-colors hover:text-foreground'

export function dashboardHealthDotClass(status: string | null | undefined): string {
  switch (status) {
    case 'healthy':
      return 'bg-success-foreground'
    case 'degraded':
      return 'bg-warning-foreground'
    case 'unhealthy':
      return 'bg-destructive-foreground'
    default:
      return 'bg-muted-foreground'
  }
}

export function dashboardHealthBadgeVariant(
  status: string | null | undefined,
): BadgeProps['variant'] {
  switch (status) {
    case 'healthy':
      return 'success'
    case 'degraded':
      return 'warning'
    case 'unhealthy':
      return 'error'
    default:
      return 'default'
  }
}

export function dashboardTransportBadgeVariant(
  transport: string | null | undefined,
): BadgeProps['variant'] {
  switch (transport) {
    case 'internal':
      return 'success'
    case 'stdio':
      return 'warning'
    case 'http':
    case 'websocket':
      return 'info'
    default:
      return 'default'
  }
}

export function dashboardEfficiencyClass(efficiencyPct: number): string {
  if (efficiencyPct > 20) {
    return 'text-success-foreground'
  }
  if (efficiencyPct > 10) {
    return 'text-warning-foreground'
  }
  return 'text-muted-foreground'
}
