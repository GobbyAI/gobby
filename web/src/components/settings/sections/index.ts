import type { ComponentType } from "react";
import { SETTINGS_SECTIONS } from "../sections";
import type { SettingsSectionId } from "../sections";
import { AppearanceSection } from "./AppearanceSection";
import { ProvidersModelsSection } from "./ProvidersModelsSection";
import { ChatVoiceSection } from "./ChatVoiceSection";
import { ProjectsSessionsSection } from "./ProjectsSessionsSection";
import { ToolApprovalsSection } from "./ToolApprovalsSection";
import { SecretsAuthSection } from "./SecretsAuthSection";
import { PromptsTemplatesSection } from "./PromptsTemplatesSection";
import { AutomationWorkflowsSection } from "./AutomationWorkflowsSection";
import { McpToolsSection } from "./McpToolsSection";
import { MemoryKnowledgeSection } from "./MemoryKnowledgeSection";
import { ObservabilitySection } from "./ObservabilitySection";
import { IntegrationsHooksSection } from "./IntegrationsHooksSection";
import { RuntimeInfrastructureSection } from "./RuntimeInfrastructureSection";

export interface SettingsSectionEntry {
  id: SettingsSectionId;
  label: string;
  component: ComponentType;
}

// Keyed by SettingsSectionId so the compiler rejects this map if a section id
// is added to sections.ts without a component (or vice versa).
const SECTION_COMPONENTS: Record<SettingsSectionId, ComponentType> = {
  appearance: AppearanceSection,
  "providers-models": ProvidersModelsSection,
  "chat-voice": ChatVoiceSection,
  "projects-sessions": ProjectsSessionsSection,
  "tool-approvals": ToolApprovalsSection,
  "secrets-auth": SecretsAuthSection,
  "prompts-templates": PromptsTemplatesSection,
  "automation-workflows": AutomationWorkflowsSection,
  "mcp-tools": McpToolsSection,
  "memory-knowledge": MemoryKnowledgeSection,
  observability: ObservabilitySection,
  "integrations-hooks": IntegrationsHooksSection,
  "runtime-infrastructure": RuntimeInfrastructureSection,
};

/**
 * The ordered section registry. Order and labels are derived from
 * SETTINGS_SECTIONS (the audit IA source of truth), so the nav and overlay
 * render sections in IA order with a single source for both.
 */
export const SETTINGS_SECTION_REGISTRY: SettingsSectionEntry[] =
  SETTINGS_SECTIONS.map((section) => ({
    id: section.id,
    label: section.label,
    component: SECTION_COMPONENTS[section.id],
  }));

export function getSettingsSectionComponent(
  id: SettingsSectionId,
): ComponentType {
  return SECTION_COMPONENTS[id];
}
