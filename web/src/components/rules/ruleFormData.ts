export interface RuleFormData {
  name: string
  event: string
  description: string
  priority: number
  enabled: boolean
  group: string
  tags: string[]
  when: string
  effect: { type: string; [key: string]: unknown }
}

export const DEFAULT_RULE_FORM: RuleFormData = {
  name: '',
  event: 'before_tool',
  description: '',
  priority: 100,
  enabled: true,
  group: '',
  tags: [],
  when: '',
  effect: { type: 'block', reason: '' },
}
