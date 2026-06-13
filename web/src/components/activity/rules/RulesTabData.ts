import { useMemo, useState } from "react";

import { useRules, type RuleDetail, type RuleSummary } from "../../../hooks/useRules";

export type RuleStatusSegment = "enabled" | "disabled";
export type RuleSourceFilter = "all" | "project" | "installed" | "template" | "gobby";

export interface RulesFilters {
  event: string;
  group: string;
  source: RuleSourceFilter;
  tag: string;
}

export interface RuleDraft {
  name: string;
  description: string;
  event: string;
  group: string;
  priority: number;
  tags: string[];
  audience: string;
  agent_scope: string[];
  enabled: boolean;
  when: string | null;
  match: Record<string, unknown> | null;
  effects: Array<Record<string, unknown>> | null;
}

export const DEFAULT_RULE_FILTERS: RulesFilters = {
  event: "",
  group: "",
  source: "all",
  tag: "",
};

export const RULE_STATUS_OPTIONS = [
  { value: "enabled", label: "Enabled" },
  { value: "disabled", label: "Disabled" },
] as const;

export const RULE_SOURCE_OPTIONS: Array<{ value: RuleSourceFilter; label: string }> = [
  { value: "all", label: "All sources" },
  { value: "project", label: "Project" },
  { value: "installed", label: "Installed" },
  { value: "template", label: "Template" },
  { value: "gobby", label: "Bundled" },
];

export const RULE_AUDIENCE_OPTIONS = [
  { value: "all", label: "All" },
  { value: "interactive", label: "Interactive" },
  { value: "autonomous", label: "Autonomous" },
  { value: "custom", label: "Custom" },
] as const;

export const RULE_EVENT_OPTIONS = [
  "turn_start",
  "turn_end",
  "session_start",
  "session_end",
  "before_agent",
  "after_agent",
  "stop",
  "before_tool",
  "after_tool",
  "before_tool_selection",
  "before_model",
  "after_model",
  "pre_compact",
  "post_compact",
  "subagent_start",
  "subagent_stop",
  "permission_request",
  "permission_denied",
  "notification",
  "stop_failure",
  "task_created",
  "task_completed",
  "teammate_idle",
  "instructions_loaded",
  "config_change",
].map((value) => ({ value, label: value }));

export function nextCopyName(name: string, existing: Iterable<string>): string {
  const existingNames = new Set(existing);
  const match = name.match(/^(.*)-copy(?:-\d+)?$/);
  const baseName = match?.[1] || name;
  let candidate = `${baseName}-copy`;
  let suffix = 2;
  while (existingNames.has(candidate)) {
    candidate = `${baseName}-copy-${suffix}`;
    suffix += 1;
  }
  return candidate;
}

export function ruleEffects(rule: RuleSummary | RuleDetail): Array<Record<string, unknown>> | null {
  if (Array.isArray(rule.effects)) return rule.effects;
  if (rule.effect) return [rule.effect];
  return null;
}

export function isBundledRule(rule: RuleSummary | RuleDetail | null): boolean {
  if (!rule) return false;
  return rule.source === "template" || rule.source === "gobby" || Boolean(rule.tags?.includes("gobby"));
}

export function detailToDraft(detail: RuleDetail): RuleDraft {
  return {
    name: detail.name,
    description: detail.description ?? "",
    event: detail.event ?? "",
    group: detail.group ?? "",
    priority: detail.priority,
    tags: detail.tags ?? [],
    audience: detail.audience ?? "all",
    agent_scope: detail.agent_scope ?? [],
    enabled: detail.enabled,
    when: detail.when,
    match: detail.match,
    effects: ruleEffects(detail),
  };
}

export function draftToDefinition(draft: RuleDraft): Record<string, unknown> {
  const definition: Record<string, unknown> = {
    event: draft.event,
    enabled: draft.enabled,
    priority: draft.priority,
    description: draft.description || null,
    tags: draft.tags,
    effects: draft.effects ?? [],
  };

  if (draft.group) definition.group = draft.group;
  if (draft.when) definition.when = draft.when;
  if (draft.match) definition.match = draft.match;
  if (draft.audience) definition.audience = draft.audience;
  if (draft.agent_scope.length > 0) definition.agent_scope = draft.agent_scope;

  return definition;
}

export function formatRuleSummaryValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "None";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

function matchesSearch(rule: RuleSummary, query: string): boolean {
  if (!query) return true;
  const haystack = [
    rule.name,
    rule.description ?? "",
    rule.event ?? "",
    rule.group ?? "",
    rule.source,
    ...(rule.tags ?? []),
  ].join(" ").toLowerCase();
  return haystack.includes(query);
}

export function useRulesTabData() {
  const rulesApi = useRules();
  const [statusSegment, setStatusSegment] = useState<RuleStatusSegment>("enabled");
  const [search, setSearch] = useState("");
  const [filters, setFilters] = useState<RulesFilters>(DEFAULT_RULE_FILTERS);

  const normalizedSearch = search.trim().toLowerCase();
  const filteredRules = useMemo(() => {
    return rulesApi.rules.filter((rule) => {
      if (rule.enabled !== (statusSegment === "enabled")) return false;
      if (!matchesSearch(rule, normalizedSearch)) return false;
      if (filters.event && rule.event !== filters.event) return false;
      if (filters.group && rule.group !== filters.group) return false;
      if (filters.source !== "all" && rule.source !== filters.source) return false;
      if (filters.tag && !rule.tags?.includes(filters.tag)) return false;
      return true;
    });
  }, [filters, normalizedSearch, rulesApi.rules, statusSegment]);

  const eventOptions = useMemo(() => {
    const events = new Set<string>();
    for (const rule of rulesApi.rules) {
      if (rule.event) events.add(rule.event);
    }
    return Array.from(events).sort();
  }, [rulesApi.rules]);

  const groupOptions = useMemo(() => {
    const fromRules: string[] = [];
    for (const rule of rulesApi.rules) {
      if (rule.group) fromRules.push(rule.group);
    }
    return Array.from(new Set([...rulesApi.groups, ...fromRules])).sort();
  }, [rulesApi.groups, rulesApi.rules]);

  const tagOptions = useMemo(() => {
    const tags = new Set<string>();
    for (const rule of rulesApi.rules) {
      for (const tag of rule.tags ?? []) tags.add(tag);
    }
    return Array.from(tags).sort();
  }, [rulesApi.rules]);

  const activeFilterCount = [
    filters.event,
    filters.group,
    filters.source === "all" ? "" : filters.source,
    filters.tag,
  ].filter(Boolean).length;

  return {
    ...rulesApi,
    search,
    setSearch,
    statusSegment,
    setStatusSegment,
    filters,
    setFilters,
    filteredRules,
    eventOptions,
    groupOptions,
    tagOptions,
    activeFilterCount,
  };
}
