import { useMemo, useState } from "react";
import * as yaml from "js-yaml";

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
  extra: Record<string, unknown>;
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
    extra: {},
  };
}

export function draftToDefinition(draft: RuleDraft): Record<string, unknown> {
  const definition: Record<string, unknown> = {
    ...draft.extra,
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

const RULE_YAML_DUMP_OPTIONS: yaml.DumpOptions = {
  lineWidth: 120,
  noRefs: true,
  sortKeys: false,
};

const RULE_DRAFT_KEYS = new Set([
  "name",
  "description",
  "event",
  "group",
  "priority",
  "enabled",
  "tags",
  "audience",
  "agent_scope",
  "when",
  "match",
  "effect",
  "effects",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function optionalString(
  source: Record<string, unknown>,
  key: string,
  fallback: string,
): string {
  const value = source[key];
  if (value === undefined || value === null) return fallback;
  if (typeof value !== "string") throw new Error(`"${key}" must be a string`);
  return value;
}

function optionalStringArray(
  source: Record<string, unknown>,
  key: string,
  fallback: string[],
): string[] {
  const value = source[key];
  if (value === undefined || value === null) return fallback;
  if (!Array.isArray(value) || !value.every((entry) => typeof entry === "string")) {
    throw new Error(`"${key}" must be a string array`);
  }
  return value;
}

function optionalNumber(
  source: Record<string, unknown>,
  key: string,
  fallback: number,
): number {
  const value = source[key];
  if (value === undefined || value === null) return fallback;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number.parseInt(value, 10);
    if (Number.isFinite(parsed)) return parsed;
  }
  throw new Error(`"${key}" must be a number`);
}

function optionalBoolean(
  source: Record<string, unknown>,
  key: string,
  fallback: boolean,
): boolean {
  const value = source[key];
  if (value === undefined || value === null) return fallback;
  if (typeof value === "boolean") return value;
  throw new Error(`"${key}" must be true or false`);
}

function optionalRecord(
  source: Record<string, unknown>,
  key: string,
  fallback: Record<string, unknown> | null,
): Record<string, unknown> | null {
  const value = source[key];
  if (value === undefined || value === null) return fallback;
  if (!isRecord(value)) throw new Error(`"${key}" must be an object`);
  return value;
}

function optionalEffects(
  source: Record<string, unknown>,
  fallback: Array<Record<string, unknown>> | null,
): Array<Record<string, unknown>> | null {
  if (source.effects === undefined && source.effect === undefined) return fallback;
  if (Array.isArray(source.effects)) {
    if (!source.effects.every(isRecord)) throw new Error('"effects" must contain objects');
    return source.effects;
  }
  if (isRecord(source.effect)) return [source.effect];
  throw new Error('"effects" must be an array of objects');
}

function extraDefinitionFields(source: Record<string, unknown>): Record<string, unknown> {
  const extra: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(source)) {
    if (!RULE_DRAFT_KEYS.has(key)) extra[key] = value;
  }
  return extra;
}

export function draftToYaml(draft: RuleDraft): string {
  return yaml.dump(
    {
      name: draft.name,
      ...draftToDefinition(draft),
    },
    RULE_YAML_DUMP_OPTIONS,
  );
}

export function yamlToDraft(content: string, fallback: RuleDraft): RuleDraft {
  const parsed = yaml.load(content, { schema: yaml.JSON_SCHEMA });
  if (!isRecord(parsed)) throw new Error("Invalid YAML: expected an object");

  return {
    name: optionalString(parsed, "name", fallback.name),
    description: optionalString(parsed, "description", fallback.description),
    event: optionalString(parsed, "event", fallback.event),
    group: optionalString(parsed, "group", fallback.group),
    priority: optionalNumber(parsed, "priority", fallback.priority),
    tags: optionalStringArray(parsed, "tags", fallback.tags),
    audience: optionalString(parsed, "audience", fallback.audience),
    agent_scope: optionalStringArray(parsed, "agent_scope", fallback.agent_scope),
    enabled: optionalBoolean(parsed, "enabled", fallback.enabled),
    when: optionalString(parsed, "when", fallback.when ?? "") || null,
    match: optionalRecord(parsed, "match", fallback.match),
    effects: optionalEffects(parsed, fallback.effects),
    extra: extraDefinitionFields(parsed),
  };
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
