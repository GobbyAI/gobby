import { getBaseUrl, type ActivitySkill } from "./SkillsTabData";

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

export async function updateSkill(
  skillId: string,
  updates: SkillUpdatePayload,
): Promise<ActivitySkill | null> {
  const response = await fetch(`${getBaseUrl()}/api/skills/${encodeURIComponent(skillId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  });
  return parseSkillResponse(response);
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

export async function exportSkill(skillId: string): Promise<{ filename: string; content: string } | null> {
  const response = await fetch(`${getBaseUrl()}/api/skills/${encodeURIComponent(skillId)}/export`);
  if (!response.ok) return null;
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
