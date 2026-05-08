import { useEffect, useState } from 'react'
import {
  normalizeStagesRegistryResponse,
  type StageRegistryEntry,
  type StagesRegistryWireResponse,
} from '../lib/taskNormalization'

export type { StageRegistryEntry }

interface UseStagesRegistryResult {
  registry: StageRegistryEntry[]
  isLoading: boolean
  error: string | null
}

let cachedRegistry: StageRegistryEntry[] | null = null
let pendingRegistryRequest: Promise<StageRegistryEntry[]> | null = null

function getBaseUrl(): string {
  return ''
}

async function fetchStagesRegistry(): Promise<StageRegistryEntry[]> {
  const response = await fetch(`${getBaseUrl()}/api/stages/registry`)
  if (!response.ok) {
    throw new Error(`Failed to fetch stages registry (${response.status})`)
  }
  const data: StagesRegistryWireResponse = await response.json()
  return normalizeStagesRegistryResponse(data)
}

function registryRequest(): Promise<StageRegistryEntry[]> {
  if (cachedRegistry) return Promise.resolve(cachedRegistry)
  if (!pendingRegistryRequest) {
    pendingRegistryRequest = fetchStagesRegistry()
      .then(registry => {
        cachedRegistry = registry
        return registry
      })
      .finally(() => {
        pendingRegistryRequest = null
      })
  }
  return pendingRegistryRequest
}

export function useStagesRegistry(): UseStagesRegistryResult {
  const [registry, setRegistry] = useState<StageRegistryEntry[]>(() => cachedRegistry ?? [])
  const [isLoading, setIsLoading] = useState(() => cachedRegistry === null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    registryRequest()
      .then(nextRegistry => {
        if (cancelled) return
        setRegistry(nextRegistry)
        setError(null)
      })
      .catch(err => {
        if (cancelled) return
        console.error('Failed to fetch stages registry:', err)
        setError(err instanceof Error ? err.message : 'Failed to fetch stages registry')
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [])

  return { registry, isLoading, error }
}
