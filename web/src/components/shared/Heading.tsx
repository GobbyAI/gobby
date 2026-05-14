import { createContext, useContext, type ReactNode, type HTMLAttributes } from 'react'

const HeadingLevelContext = createContext<number>(1)

interface HeadingProviderProps {
  level: number
  children: ReactNode
}

export function HeadingProvider({ level, children }: HeadingProviderProps) {
  return (
    <HeadingLevelContext.Provider value={Math.max(1, Math.min(level, 6))}>
      {children}
    </HeadingLevelContext.Provider>
  )
}

interface HeadingProps extends HTMLAttributes<HTMLHeadingElement> {
  level?: number
}

export function Heading({ level: explicit, ...rest }: HeadingProps) {
  const ambient = useContext(HeadingLevelContext)
  const resolved = Math.max(1, Math.min(explicit ?? ambient, 6))
  const Tag = `h${resolved}` as 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6'
  return <Tag {...rest} />
}
