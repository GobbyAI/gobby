import type { ReactNode } from 'react'
import { cn } from '../../lib/utils'
import {
  dashboardCardBodyClass,
  dashboardCardClass,
  dashboardCardHeaderClass,
  dashboardCardTitleClass,
} from './dashboardStyles'

interface DashboardCardProps {
  title: string
  children: ReactNode
  action?: ReactNode
  className?: string
  bodyClassName?: string
}

export function DashboardCard({
  title,
  children,
  action,
  className,
  bodyClassName,
}: DashboardCardProps) {
  return (
    <section className={cn(dashboardCardClass, className)}>
      <div className={dashboardCardHeaderClass}>
        <h3 className={dashboardCardTitleClass}>{title}</h3>
        {action}
      </div>
      <div className={cn(dashboardCardBodyClass, bodyClassName)}>{children}</div>
    </section>
  )
}
