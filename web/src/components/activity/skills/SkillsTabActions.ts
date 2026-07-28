import {
  encodeSkillFilePath,
  getBaseUrl,
  type ActivitySkill,
  type SkillHub,
  type SkillHubResult,
  type SkillScanFinding,
  type SkillScanResult,
} from "./SkillsTabData";

export type SkillUpdatePayload = Partial<
  Pick<
    ActivitySkill,
    | "description"
    | "content"
    | "version"
    | "license"
    | "compatibility"
    | "allowed_tools"
    | "enabled"
    | "always_apply"
    | "injection_format"
    | "project_id"
  >
>;

async function parseSkillResponse(response: Response): Promise<ActivitySkill | null> {
  if (!response.ok) return null;
  const data = await response.json();
  if (data?.skill) return data.skill as ActivitySkill;
  if (data?.id) return data as ActivitySkill;
  return null;
}

async function responseError(response: Response, fallback: string): Promise<Error> {
  const body = await response.json().catch(() => null);
  return new Error(body?.detail || body?.error || fallback);
}

export async function loadSkillHubs(): Promise<SkillHub[]> {
  const response = await fetch(`${getBaseUrl()}/api/skills/hubs`);
  if (!response.ok) throw await responseError(response, "Failed to load skill hubs");
  const data = await response.json();
  return Array.isArray(data?.hubs) ? (data.hubs as SkillHub[]) : [];
}

export async function searchSkillHubs(
  query: string,
  hubName?: string,
): Promise<{ results: SkillHubResult[]; hubErrors: Record<string, string> }> {
  const params = new URLSearchParams({ q: query });
  if (hubName) params.set("hub_name", hubName);

  const response = await fetch(`${getBaseUrl()}/api/skills/hubs/search?${params}`);
  if (!response.ok) throw await responseError(response, "Failed to search skill hubs");

  const data = await response.json();
  return {
    results: Array.isArray(data?.results) ? (data.results as SkillHubResult[]) : [],
    hubErrors:
      data?.hub_errors && typeof data.hub_errors === "object"
        ? (data.hub_errors as Record<string, string>)
        : {},
  };
}

export async function scanHubSkill(content: string, name: string): Promise<SkillScanResult> {
  const response = await fetch(`${getBaseUrl()}/api/skills/scan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, name }),
  });
  if (!response.ok) throw await responseError(response, "Failed to scan hub skill");

  const data = await response.json();
  const findings = Array.isArray(data?.findings) ? (data.findings as SkillScanFinding[]) : [];
  const isSafe = Boolean(data?.is_safe ?? data?.safe ?? findings.length === 0);

  return {
    is_safe: isSafe,
    max_severity: String(data?.max_severity ?? (isSafe ? "info" : "unknown")),
    scan_duration_seconds: Number(data?.scan_duration_seconds ?? 0),
    findings,
    findings_count: Number(data?.findings_count ?? findings.length),
  };
}

export async function installHubSkill(params: {
  hubName: string;
  slug: string;
  version?: string | null;
  projectId?: string | null;
}): Promise<ActivitySkill | null> {
  const response = await fetch(`${getBaseUrl()}/api/skills/hubs/install`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      hub_name: params.hubName,
      slug: params.slug,
      version: params.version,
      project_id: params.projectId ?? null,
    }),
  });
  if (!response.ok) throw await responseError(response, "Failed to install hub skill");
  return parseSkillResponse(response);
}

export async function updateSkill(
  skillId: string,
  updates: SkillUpdatePayload,
): Promise<ActivitySkill | null> {
  const response = await fetch(`${getBaseUrl()}/api/skills/${encodeURIComponent(skillId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  });
  if (!response.ok) throw await responseError(response, "Failed to update skill");
  return parseSkillResponse(response);
}

export async function saveSkillFile(
  skillId: string,
  path: string,
  content: string,
): Promise<boolean> {
  const response = await fetch(
    `${getBaseUrl()}/api/skills/${encodeURIComponent(skillId)}/files/${encodeSkillFilePath(path)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    },
  );
  if (!response.ok) throw await responseError(response, "Failed to save skill file");
  return true;
}

export async function deleteSkill(skillId: string): Promise<boolean> {
  const response = await fetch(`${getBaseUrl()}/api/skills/${encodeURIComponent(skillId)}`, {
    method: "DELETE",
  });
  return response.ok;
}

export async function toggleSkill(skill: ActivitySkill): Promise<ActivitySkill | null> {
  return updateSkill(skill.id, { enabled: !skill.enabled });
}

export async function moveSkillToProject(
  skillId: string,
  projectId: string,
): Promise<ActivitySkill | null> {
  const params = new URLSearchParams({ project_id: projectId });
  const response = await fetch(
    `${getBaseUrl()}/api/skills/${encodeURIComponent(skillId)}/move-to-project?${params}`,
    { method: "POST" },
  );
  return parseSkillResponse(response);
}

export async function moveSkillToInstalled(skillId: string): Promise<ActivitySkill | null> {
  const response = await fetch(
    `${getBaseUrl()}/api/skills/${encodeURIComponent(skillId)}/move-to-installed`,
    { method: "POST" },
  );
  return parseSkillResponse(response);
}

export async function exportSkill(skillId: string): Promise<{ filename: string; content: string }> {
  const response = await fetch(`${getBaseUrl()}/api/skills/${encodeURIComponent(skillId)}/export`);
  if (!response.ok) throw await responseError(response, "Failed to export skill");
  return response.json() as Promise<{ filename: string; content: string }>;
}

export function downloadSkillExport(result: { filename: string; content: string }): void {
  const blob = new Blob([result.content], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = result.filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
