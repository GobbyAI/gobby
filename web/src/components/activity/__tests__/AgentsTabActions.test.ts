import { describe, expect, it } from "vitest";

import {
  buildAgentDefinitionBody,
  buildDuplicateAgentBody,
} from "../agents/AgentsTabActions";
import {
  agentToDraft,
  createAgentDraft,
  type AgentDefInfo,
} from "../agents/AgentsTabData";

function agentDefinition(): AgentDefInfo {
  return {
    definition: {
      name: "reviewer",
      description: "Reviews changes",
      surfaces: ["spawn"],
      prompts: { agent: "Review the assigned implementation." },
      provider: "claude",
      model: "opus",
      reasoning_effort: null,
      reasoning_required: false,
      fallback_agent: null,
      is_local: false,
      sources: ["https://example.com/reviewer.yaml"],
      version: "1.2.0",
      isolation: "worktree",
      base_branch: "0.5.0",
      timeout: 120,
      workflows: {
        rules: ["review-rules"],
        rule_selectors: { include: ["tag:review"], exclude: [] },
        skill_selectors: {
          include: ["*", "development-discipline"],
          exclude: ["browser-testing"],
        },
        skill_format: "full",
        custom_workflow_key: { enabled: true },
      },
      step_workflow: {
        steps: [{ name: "review", description: "Review the change" }],
        variables: { current_step: "review" },
        exit_condition: "review_complete == true",
      },
      blocked_tools: ["Write"],
      blocked_mcp_tools: ["dangerous-tool"],
    },
    source: "installed",
    source_path: null,
    db_id: "agent-id",
    enabled: true,
    overridden_by: null,
    deleted_at: null,
    tags: ["review"],
  };
}

describe("AgentsTabActions", () => {
  it("preserves wildcard, exclusions, and custom workflow keys across edit-save", () => {
    const body = buildAgentDefinitionBody(agentToDraft(agentDefinition()));

    expect(body.prompts).toEqual({
      persona: null,
      agent: "Review the assigned implementation.",
    });
    expect(body.workflows).toEqual({
      rules: ["review-rules"],
      rule_selectors: { include: ["tag:review"], exclude: [] },
      skill_selectors: {
        include: ["*", "development-discipline"],
        exclude: ["browser-testing"],
      },
      skill_format: "full",
      custom_workflow_key: { enabled: true },
    });
  });

  it("defaults new drafts to the tag:default rule selector", () => {
    expect(buildAgentDefinitionBody(createAgentDraft()).workflows).toEqual({
      rule_selectors: { include: ["tag:default"], exclude: [] },
    });
  });

  it("keeps rule selectors when the other editable workflow values are cleared", () => {
    const draft = createAgentDraft();
    draft.form.pipeline = "review";
    draft.rules = ["review-rules"];
    draft.ruleSelectors = { include: ["review"], exclude: [] };
    draft.variables = { review_depth: "full" };
    draft.skills = ["development-discipline"];
    draft.workflows = {
      pipeline: "review",
      rules: ["review-rules"],
      rule_selectors: draft.ruleSelectors,
      variables: draft.variables,
      skill_selectors: { include: draft.skills },
    };

    draft.form.pipeline = "";
    draft.rules = [];
    draft.ruleSelectors = { include: [], exclude: [] };
    draft.variables = {};
    draft.skills = [];

    expect(buildAgentDefinitionBody(draft).workflows).toEqual({
      rule_selectors: { include: [], exclude: [] },
    });
  });

  it("builds duplicate payloads without dropping definition fields", () => {
    const body = buildDuplicateAgentBody(
      agentDefinition(),
      "reviewer-copy",
      "project-1",
    );

    expect(body).toMatchObject({
      name: "reviewer-copy",
      project_id: "project-1",
      sources: ["https://example.com/reviewer.yaml"],
      version: "1.2.0",
      step_workflow: {
        steps: [{ name: "review", description: "Review the change" }],
        variables: { current_step: "review" },
        exit_condition: "review_complete == true",
      },
      blocked_tools: ["Write"],
      blocked_mcp_tools: ["dangerous-tool"],
      workflows: expect.objectContaining({
        custom_workflow_key: { enabled: true },
      }),
    });
    // The create API forbids unknown keys, so response-only fields must not leak.
    expect(body).not.toHaveProperty("is_local");
    expect(body).not.toHaveProperty("mode");
  });

  it("round-trips nested step_workflow through draft and save body", () => {
    const agent = agentDefinition();
    const draft = agentToDraft(agent);
    const body = buildAgentDefinitionBody(draft);

    expect(draft.steps).toEqual([
      { name: "review", description: "Review the change" },
    ]);
    expect(draft.stepVariables).toEqual({ current_step: "review" });
    expect(draft.exitCondition).toBe("review_complete == true");
    expect(body.step_workflow).toEqual({
      steps: [{ name: "review", description: "Review the change" }],
      variables: { current_step: "review" },
      exit_condition: "review_complete == true",
    });
    expect(body).not.toHaveProperty("steps");
    expect(body).not.toHaveProperty("step_variables");
    expect(body).not.toHaveProperty("exit_condition");
  });
});
