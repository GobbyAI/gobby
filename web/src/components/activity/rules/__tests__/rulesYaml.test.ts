import { describe, expect, it } from "vitest";

import type { RuleDetail } from "../../../../hooks/useRules";
import {
  detailToDraft,
  draftToDefinition,
  draftToYaml,
  type RuleDraft,
  yamlToDraft,
} from "../RulesTabData";

const baseDraft: RuleDraft = {
  name: "alpha-rule",
  description: "Alpha description",
  event: "session.started",
  group: "sessions",
  priority: 20,
  tags: ["alpha", "guard"],
  audience: "agent",
  agent_scope: ["codex"],
  enabled: true,
  when: "session.state == 'ready'",
  match: { project: "gobby" },
  effects: [{ type: "inject_context", content: "Read AGENTS.md" }],
  extra: {
    throttle: { limit: 2, window: "1m" },
    custom_flag: true,
  },
};

describe("rules YAML conversion", () => {
  it("round-trips the complete fetched rule definition", () => {
    const detail = {
      id: "rule-id",
      name: "alpha-rule",
      description: "Alpha description",
      event: "session.started",
      group: "sessions",
      when: "session.state == 'ready'",
      enabled: true,
      priority: 20,
      source: "project",
      tags: ["alpha", "guard"],
      audience: "agent",
      agent_scope: ["codex"],
      match: { project: "gobby" },
      tools: ["gobby-tasks"],
      effects: [
        { type: "inject_context", content: "Read AGENTS.md" },
        { type: "set_variable", name: "rules_checked", value: true },
      ],
      custom_flag: true,
    } as RuleDetail & Record<string, unknown>;

    const draft = detailToDraft(detail);

    expect(draft.extra).toEqual({ tools: ["gobby-tasks"], custom_flag: true });
    expect(draftToDefinition(draft)).toMatchObject({
      match: detail.match,
      tools: detail.tools,
      effects: detail.effects,
      custom_flag: true,
    });
    expect(draftToDefinition(draft)).not.toHaveProperty("id");
    expect(draftToDefinition(draft)).not.toHaveProperty("source");
  });

  it("serializes full draft definitions and preserves unknown fields", () => {
    const yamlText = draftToYaml(baseDraft);

    expect(yamlText).toContain("name: alpha-rule");
    expect(yamlText).toContain("match:");
    expect(yamlText).toContain("effects:");
    expect(yamlText).toContain("throttle:");

    const parsedDraft = yamlToDraft(yamlText, { ...baseDraft, name: "fallback-rule" });
    const definition = draftToDefinition(parsedDraft);

    expect(parsedDraft).toEqual(baseDraft);
    expect(definition).toMatchObject({
      event: "session.started",
      throttle: { limit: 2, window: "1m" },
      custom_flag: true,
    });
    expect(definition).not.toHaveProperty("name");
  });

  it("parses edited YAML into the save draft shape", () => {
    const parsedDraft = yamlToDraft(
      `
name: alpha-yaml
description: Edited through YAML
event: session.closed
group: lifecycle
priority: 42
tags:
  - yaml
  - edited
audience: session
agent_scope:
  - codex
  - claude
enabled: false
when: workspace == clean
match:
  branch: main
effects:
  - type: set_variable
    name: rules_yaml_enabled
    value: true
custom_nested:
  allowed: true
`,
      baseDraft,
    );

    expect(parsedDraft).toEqual({
      name: "alpha-yaml",
      description: "Edited through YAML",
      event: "session.closed",
      group: "lifecycle",
      priority: 42,
      tags: ["yaml", "edited"],
      audience: "session",
      agent_scope: ["codex", "claude"],
      enabled: false,
      when: "workspace == clean",
      match: { branch: "main" },
      effects: [{ type: "set_variable", name: "rules_yaml_enabled", value: true }],
      extra: { custom_nested: { allowed: true } },
    });
  });

  it("rejects YAML that cannot represent a rule definition", () => {
    expect(() => yamlToDraft("[]", baseDraft)).toThrow("Invalid YAML: expected an object");
    expect(() => yamlToDraft("effects: block", baseDraft)).toThrow(
      '"effects" must be an array of objects',
    );
  });
});
