interface PanelIconProps {
  pinned?: boolean
  size?: number
  className?: string
}

export function PanelIcon({ pinned = false, size = 14, className }: PanelIconProps) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <line x1="15" y1="3" x2="15" y2="21" />
      {pinned && <line x1="18" y1="9" x2="21" y2="9" opacity="0.5" />}
    </svg>
  )
}
