import type { ReactNode } from 'react'
import { cn } from '../../lib/utils'
import {
  dashboardCardBodyClass,
  dashboardCardClass,
  dashboardCardHeaderClass,
  dashboardCardTitleClass,
} from './dashboardStyles'
import { Heading } from '../shared/Heading'

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
        <Heading level={3} className={dashboardCardTitleClass}>{title}</Heading>
        {action}
      </div>
      <div className={cn(dashboardCardBodyClass, bodyClassName)}>{children}</div>
    </section>
  )
}
