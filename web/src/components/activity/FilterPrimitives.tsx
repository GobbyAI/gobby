import type { ReactNode } from 'react'

export function FilterSection({
  label,
  children,
}: {
  label: string
  children: ReactNode
}) {
  return (
    <div className="flex flex-col gap-0.5 py-0.5">
      <div className="px-2 py-1 text-[length:var(--text-sm)] font-medium uppercase tracking-wide text-muted-foreground/80">
        {label}
      </div>
      {children}
    </div>
  )
}

export function FilterCheckboxRow({
  label,
  checked,
  onToggle,
  leading,
}: {
  label: string
  checked: boolean
  onToggle: () => void
  leading?: ReactNode
}) {
  return (
    <label
      className={`flex min-w-0 items-center gap-1.5 px-2 py-1 rounded text-[length:var(--text-md)] cursor-pointer hover:bg-muted/50 ${
        checked ? 'text-foreground bg-muted/50' : 'text-muted-foreground'
      }`}
    >
      <input
        type="checkbox"
        className="w-3 h-3"
        checked={checked}
        onChange={onToggle}
      />
      {leading}
      <span className="truncate">{label}</span>
    </label>
  )
}
