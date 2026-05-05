import { memo, type ReactNode } from 'react'

interface ActivityPanelEmptyProps {
  icon?: ReactNode
  heading?: string
  body: string
  footer?: ReactNode
}

export const ActivityPanelEmpty = memo(function ActivityPanelEmpty({
  icon,
  heading,
  body,
  footer,
}: ActivityPanelEmptyProps) {
  return (
    <div className="activity-tab-empty">
      {icon && (
        <div className="activity-tab-empty__icon" aria-hidden="true">
          {icon}
        </div>
      )}
      {heading && <div className="activity-tab-empty__heading">{heading}</div>}
      <p className="activity-tab-empty__body">{body}</p>
      {footer}
    </div>
  )
})

const ICON_PROPS = {
  width: 48,
  height: 48,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.5,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
} as const

export function PlansEmptyIcon() {
  return (
    <svg {...ICON_PROPS}>
      <rect x="6" y="4" width="12" height="16" rx="2" />
      <path d="M9 4v2h6V4" />
      <path d="m9 11 1.5 1.5L13 10" />
      <path d="M9 16h6" />
    </svg>
  )
}

export function ArtifactsEmptyIcon() {
  return (
    <svg {...ICON_PROPS}>
      <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
      <path d="m3.27 6.96 8.73 5.05 8.73-5.05" />
      <path d="M12 22.08V12" />
    </svg>
  )
}

export function ChangesEmptyIcon() {
  return (
    <svg {...ICON_PROPS}>
      <circle cx="6" cy="6" r="2.25" />
      <circle cx="18" cy="18" r="2.25" />
      <path d="M9 6h7a2 2 0 0 1 2 2v7" />
      <path d="m13 11 3-3-3-3" />
      <path d="M15 18H8a2 2 0 0 1-2-2V9" />
      <path d="m11 13-3 3 3 3" />
    </svg>
  )
}

export function CanvasEmptyIcon() {
  return (
    <svg {...ICON_PROPS}>
      <path d="M12 19l7-7 3 3-7 7-3-3z" />
      <path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z" />
      <path d="M2 2l7.586 7.586" />
      <circle cx="11" cy="11" r="2" />
    </svg>
  )
}

export function TasksEmptyIcon() {
  return (
    <svg {...ICON_PROPS}>
      <path d="m3 7 2 2 4-4" />
      <path d="m3 14 2 2 4-4" />
      <path d="M13 8h8" />
      <path d="M13 15h8" />
    </svg>
  )
}

export function SessionsEmptyIcon() {
  return (
    <svg {...ICON_PROPS}>
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  )
}

export function PipelinesEmptyIcon() {
  return (
    <svg {...ICON_PROPS}>
      <circle cx="5" cy="12" r="2" />
      <circle cx="12" cy="12" r="2" />
      <circle cx="19" cy="12" r="2" />
      <path d="M7 12h3" />
      <path d="M14 12h3" />
    </svg>
  )
}

export function CronEmptyIcon() {
  return (
    <svg {...ICON_PROPS}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </svg>
  )
}

export function FilesEmptyIcon() {
  return (
    <svg {...ICON_PROPS}>
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
    </svg>
  )
}

export function TracesEmptyIcon() {
  return (
    <svg {...ICON_PROPS}>
      <path d="M3 12h3l3-7 4 14 3-7h5" />
    </svg>
  )
}
