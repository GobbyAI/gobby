export interface TaskOption<T extends string | number = string> {
  value: T
  label: string
}

export const TASK_CATEGORY_OPTIONS = [
  { value: '', label: 'Uncategorized' },
  { value: 'code', label: 'Code' },
  { value: 'config', label: 'Config' },
  { value: 'docs', label: 'Docs' },
  { value: 'refactor', label: 'Refactor' },
  { value: 'test', label: 'Test' },
  { value: 'research', label: 'Research' },
  { value: 'planning', label: 'Planning' },
  { value: 'manual', label: 'Manual' },
] as const satisfies readonly TaskOption<string>[]

export const TASK_PRIORITY_OPTIONS = [
  { value: 0, label: 'Critical' },
  { value: 1, label: 'High' },
  { value: 2, label: 'Medium' },
  { value: 3, label: 'Low' },
  { value: 4, label: 'Backlog' },
] as const satisfies readonly TaskOption<number>[]

export function taskPriorityLabel(priority: number): string {
  return TASK_PRIORITY_OPTIONS.find(option => option.value === priority)?.label ?? 'Medium'
}
