import { forwardRef, type HTMLAttributes } from 'react'
import { cn } from '../../../lib/utils'

export interface ScrollAreaProps extends HTMLAttributes<HTMLDivElement> {}

export const ScrollArea = forwardRef<HTMLDivElement, ScrollAreaProps>(
  ({ className, children, ...props }, ref) => {
    return (
      <div
        ref={ref}
        tabIndex={0}
        className={cn(
          'overflow-y-auto outline-none',
          '[&::-webkit-scrollbar]:w-2',
          '[&::-webkit-scrollbar-track]:bg-transparent',
          '[&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border',
          '[scrollbar-width:thin] [scrollbar-color:var(--border)_transparent]',
          className
        )}
        {...props}
      >
        {children}
      </div>
    )
  }
)
ScrollArea.displayName = 'ScrollArea'
