import { forwardRef, useCallback, useEffect, useRef, type TextareaHTMLAttributes } from 'react'
import { cn } from '../../lib/utils'

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  autoResize?: boolean
  maxHeight?: number
}

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, autoResize = true, maxHeight = 200, onChange, ...props }, ref) => {
    const internalRef = useRef<HTMLTextAreaElement>(null)

    const setRef = useCallback((el: HTMLTextAreaElement | null) => {
      (internalRef as React.MutableRefObject<HTMLTextAreaElement | null>).current = el
      if (typeof ref === 'function') ref(el)
      else if (ref) (ref as React.MutableRefObject<HTMLTextAreaElement | null>).current = el
    }, [ref])

    const resize = useCallback(() => {
      if (!autoResize || !internalRef.current) return
      const el = internalRef.current
      el.style.height = 'auto'
      const clampedHeight = Math.min(el.scrollHeight, maxHeight)
      el.style.height = `${clampedHeight}px`
      el.style.overflowY = el.scrollHeight > maxHeight ? 'auto' : 'hidden'
    }, [autoResize, maxHeight])

    useEffect(() => {
      resize()
    }, [props.value, resize])

    const handleChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
      onChange?.(e)
      resize()
    }, [onChange, resize])

    return (
      <textarea
        className={cn(
          'flex w-full rounded-md border border-border bg-transparent px-3 py-2 text-sm',
          'placeholder:text-muted-foreground',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background',
          'disabled:cursor-not-allowed disabled:opacity-50',
          'resize-none',
          className
        )}
        ref={setRef}
        onChange={handleChange}
        {...props}
      />
    )
  }
)
Textarea.displayName = 'Textarea'
