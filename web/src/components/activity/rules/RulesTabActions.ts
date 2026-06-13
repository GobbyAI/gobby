import { RuleApiError, type RuleDetail, type RuleSummary } from "../../../hooks/useRules";
import {
  detailToDraft,
  draftToDefinition,
  nextCopyName,
  type RuleDraft,
} from "./RulesTabData";

interface CopyRuleApi {
  fetchRuleDetail: (name: string) => Promise<RuleDetail | null>;
  createRule: (
    name: string,
    definition: Record<string, unknown>,
    options?: { throwOnError?: boolean },
  ) => Promise<RuleDetail | null>;
  fetchRules: () => Promise<void>;
}

interface SaveRuleApi {
  fetchRuleDetail: (name: string) => Promise<RuleDetail | null>;
  updateRule: (
    name: string,
    definition: Record<string, unknown>,
    options?: { newName?: string; throwOnError?: boolean },
  ) => Promise<boolean>;
  fetchRules: () => Promise<void>;
}

const ruleSaveQueues = new Map<string, Promise<unknown>>();

export function formatRuleError(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

function shouldRetryCopy(error: unknown): boolean {
  return error instanceof RuleApiError && (error.status === 400 || error.status === 409);
}

export async function copyRuleWithRetry(
  rule: RuleSummary,
  existingNames: Iterable<string>,
  api: CopyRuleApi,
): Promise<string> {
  const detail = await api.fetchRuleDetail(rule.name);
  if (!detail) throw new Error(`Failed to load ${rule.name}`);

  const definition = draftToDefinition(detailToDraft(detail));
  const names = new Set(existingNames);

  for (let attempt = 0; attempt < 2; attempt += 1) {
    const candidate = nextCopyName(rule.name, names);
    try {
      await api.createRule(candidate, definition, { throwOnError: true });
      await api.fetchRules();
      return candidate;
    } catch (error) {
      names.add(candidate);
      if (attempt === 0 && shouldRetryCopy(error)) continue;
      throw error;
    }
  }

  throw new Error(`Failed to copy ${rule.name}`);
}

function enqueueRuleSave<T>(ruleName: string, run: () => Promise<T>): Promise<T> {
  const previous = ruleSaveQueues.get(ruleName) ?? Promise.resolve();
  const next = previous.catch(() => undefined).then(run);
  const queued = next.finally(() => {
    if (ruleSaveQueues.get(ruleName) === queued) {
      ruleSaveQueues.delete(ruleName);
    }
  });
  ruleSaveQueues.set(ruleName, queued);
  return next;
}

export function saveRuleDraft(
  originalName: string,
  draft: RuleDraft,
  api: SaveRuleApi,
): Promise<string> {
  return enqueueRuleSave(originalName, async () => {
    const fresh = await api.fetchRuleDetail(originalName);
    if (!fresh) throw new Error(`Failed to reload ${originalName}`);

    const freshDraft = detailToDraft(fresh);
    const merged: RuleDraft = {
      ...freshDraft,
      name: draft.name.trim(),
      description: draft.description,
      event: draft.event,
      group: draft.group,
      priority: draft.priority,
      tags: draft.tags,
      audience: draft.audience,
      agent_scope: draft.agent_scope,
      enabled: draft.enabled,
    };

    if (!merged.name) throw new Error("Rule name is required");

    await api.updateRule(originalName, draftToDefinition(merged), {
      newName: merged.name,
      throwOnError: true,
    });
    await api.fetchRules();
    return merged.name;
  });
}
