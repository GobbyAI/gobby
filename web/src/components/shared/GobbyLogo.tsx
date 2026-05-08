import type { CSSProperties, HTMLAttributes } from 'react'
import { cn } from '../../lib/utils'

interface GobbyLogoProps extends Omit<HTMLAttributes<HTMLSpanElement>, 'children'> {
  label?: string
  size?: number | string
}

export function GobbyLogo({
  className,
  label = 'Gobby logo',
  size = 20,
  style,
  ...props
}: GobbyLogoProps) {
  const logoSize = typeof size === 'number' ? `${size}px` : size
  const logoStyle = {
    ...style,
    '--gobby-logo-size': logoSize,
  } as CSSProperties

  return (
    <span
      role="img"
      aria-label={label}
      className={cn('gobby-logo', className)}
      style={logoStyle}
      {...props}
    />
  )
}
