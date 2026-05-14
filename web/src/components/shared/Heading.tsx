import { createContext, useContext, type ReactNode, type HTMLAttributes } from 'react'

const HeadingLevelContext = createContext<number>(1)

type HeadingLevel = 1 | 2 | 3 | 4 | 5 | 6

function normalizeHeadingLevel(level: number): HeadingLevel {
  const finiteLevel = Number.isFinite(level) ? Math.trunc(level) : 1
  return Math.max(1, Math.min(finiteLevel, 6)) as HeadingLevel
}

interface HeadingProviderProps {
  level: number
  children: ReactNode
}

export function HeadingProvider({ level, children }: HeadingProviderProps) {
  return (
    <HeadingLevelContext.Provider value={normalizeHeadingLevel(level)}>
      {children}
    </HeadingLevelContext.Provider>
  )
}

interface HeadingProps extends HTMLAttributes<HTMLHeadingElement> {
  level?: number
}

export function Heading({ level: explicit, ...rest }: HeadingProps) {
  const ambient = useContext(HeadingLevelContext)
  const resolved = normalizeHeadingLevel(explicit ?? ambient)
  const Tag = `h${resolved}` as 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6'
  return <Tag {...rest} />
}
