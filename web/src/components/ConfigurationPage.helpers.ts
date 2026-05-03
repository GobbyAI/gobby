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

export function isSecretField(path: string, secretKeys: string[]): boolean {
  if (secretKeys.includes(path)) return true
  const last = path.split('.').pop() || ''
  return SECRET_PATTERNS.some(p => last.includes(p))
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
  if (fieldSchema.anyOf) {
    const types = (fieldSchema.anyOf as Record<string, unknown>[])
      .map(t => t.type as string)
      .filter(t => t !== 'null')
    return types[0] || 'string'
  }
  return (fieldSchema.type as string) || 'string'
}
