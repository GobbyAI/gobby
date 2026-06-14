export type SkillSegment = "installed" | "hub";
export type SkillSourceFilter = "all" | "installed" | "project" | "hub" | "deleted";

export interface ActivitySkill {
  id: string;
  name: string;
  description: string;
  content: string;
  version: string | null;
  license: string | null;
  compatibility: string | null;
  allowed_tools: string[] | null;
  metadata: Record<string, unknown> | null;
  source_path: string | null;
  source_type: string | null;
  source_ref: string | null;
  source: string | null;
  hub_name: string | null;
  hub_slug: string | null;
  hub_version: string | null;
  enabled: boolean;
  always_apply: boolean;
  injection_format: string;
  project_id: string | null;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface SkillFilters {
  search: string;
  source: SkillSourceFilter;
  category: string;
}

export interface SkillHub {
  name: string;
  type: string;
  base_url: string | null;
  repo: string | null;
  auth_required?: boolean | null;
  auth_configured?: boolean | null;
  auth_key_name?: string | null;
}

export interface SkillHubResult {
  slug: string;
  display_name: string;
  description: string;
  hub_name: string;
  version: string | null;
  score: number | null;
  license?: string | null;
  content?: string | null;
}

export interface SkillScanFinding {
  severity: string;
  title: string;
  description: string;
  category?: string;
  remediation: string;
  location: string;
}

export interface SkillScanResult {
  is_safe: boolean;
  max_severity: string;
  scan_duration_seconds: number;
  findings: SkillScanFinding[];
  findings_count: number;
}

export const SKILL_SEGMENT_OPTIONS: Array<{ value: SkillSegment; label: string }> = [
  { value: "installed", label: "Installed" },
  { value: "hub", label: "Hub" },
];

export const SKILL_SOURCE_OPTIONS: Array<{ value: SkillSourceFilter; label: string }> = [
  { value: "all", label: "All sources" },
  { value: "installed", label: "Installed" },
  { value: "project", label: "Project" },
  { value: "hub", label: "Hub" },
  { value: "deleted", label: "Deleted" },
];

export function getBaseUrl(): string {
  return "";
}

export function skillCategory(skill: ActivitySkill): string {
  const direct = skill.metadata?.category;
  if (typeof direct === "string" && direct.trim()) return direct.trim();

  const gobby = skill.metadata?.gobby;
  if (gobby && typeof gobby === "object" && "category" in gobby) {
    const nested = (gobby as { category?: unknown }).category;
    if (typeof nested === "string" && nested.trim()) return nested.trim();
  }

  return "Uncategorized";
}

export function skillSourceKey(skill: ActivitySkill): Exclude<SkillSourceFilter, "all"> {
  if (skill.deleted_at) return "deleted";
  if (skill.project_id || skill.source === "project") return "project";
  if (skill.hub_name || skill.source === "hub") return "hub";
  return "installed";
}

export function skillSourceLabel(skill: ActivitySkill): string {
  const source = skillSourceKey(skill);
  if (source === "hub") return "hub";
  return source.toUpperCase();
}

export function filterInstalledSkills(
  skills: ActivitySkill[],
  filters: SkillFilters,
): ActivitySkill[] {
  const query = filters.search.trim().toLowerCase();

  return skills.filter((skill) => {
    if (filters.source !== "all" && skillSourceKey(skill) !== filters.source) return false;
    if (filters.category !== "all" && skillCategory(skill) !== filters.category) return false;
    if (!query) return true;

    const haystack = [
      skill.name,
      skill.description,
      skillCategory(skill),
      skill.version ?? "",
      skill.license ?? "",
      skill.compatibility ?? "",
      ...(skill.allowed_tools ?? []),
    ]
      .join(" ")
      .toLowerCase();
    return haystack.includes(query);
  });
}

export function skillCategories(skills: ActivitySkill[]): string[] {
  return Array.from(new Set(skills.map(skillCategory))).sort((left, right) =>
    left.localeCompare(right, undefined, { sensitivity: "base" }),
  );
}

export async function loadInstalledSkills(): Promise<ActivitySkill[]> {
  const params = new URLSearchParams({
    limit: "200",
    include_deleted: "true",
  });
  const response = await fetch(`${getBaseUrl()}/api/skills?${params}`);
  if (!response.ok) {
    throw new Error(`Failed to load skills (${response.status})`);
  }
  const data = (await response.json()) as { skills?: ActivitySkill[] };
  return data.skills ?? [];
}
