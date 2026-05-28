export type ApprovalRuleRow = { id: string; value: string }

export function createApprovalRuleRow(value = ''): ApprovalRuleRow {
  const id =
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `project-approval-rule-${Date.now()}-${Math.random().toString(36).slice(2)}`
  return { id, value }
}

export function toApprovalRuleRows(rules: string[]): ApprovalRuleRow[] {
  return rules.map((rule) => createApprovalRuleRow(rule))
}

const SECRET_PATTERNS = ['api_key', 'api_token', 'api_secret', 'password', 'access_token', 'auth_token', 'secret_key', 'secret', 'credentials', 'private_key', 'client_secret']
const UNSAFE_PATH_SEGMENTS = new Set(['__proto__', 'prototype', 'constructor'])

export function isSecretField(path: string, secretKeys: string[]): boolean {
  if (secretKeys.includes(path)) return true
  const last = path.split('.').pop() || ''
  return SECRET_PATTERNS.some(p => last.includes(p))
}

export function splitSafeConfigPath(path: string): string[] | null {
  const parts = path.split('.')
  if (parts.length === 0) return null
  if (parts.some(part => !part || UNSAFE_PATH_SEGMENTS.has(part))) return null
  return parts
}

export function formatFieldName(name: string): string {
  return name
    .replace(/_/g, ' ')
    .replace(/-/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase())
}

export function getSchemaProperties(schema: Record<string, unknown>): Record<string, unknown> {
  const props = schema.properties as Record<string, unknown> | undefined
  return props || {}
}

export function getSchemaType(fieldSchema: Record<string, unknown>): string {
  const types = collectSchemaTypes(fieldSchema)
  return types[0] || 'string'
}

function collectSchemaTypes(schema: Record<string, unknown>): string[] {
  const types = new Set<string>()
  addSchemaType(schema.type, types)

  for (const key of ['anyOf', 'oneOf', 'allOf']) {
    const variants = schema[key]
    if (!Array.isArray(variants)) continue
    for (const variant of variants) {
      if (variant && typeof variant === 'object') {
        for (const type of collectSchemaTypes(variant as Record<string, unknown>)) {
          types.add(type)
        }
      }
    }
  }

  return [...types]
}

function addSchemaType(value: unknown, types: Set<string>): void {
  if (Array.isArray(value)) {
    for (const item of value) addSchemaType(item, types)
    return
  }
  if (typeof value === 'string' && value !== 'null') {
    types.add(value)
  }
}
