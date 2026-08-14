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
      role: "reviewer",
      goal: null,
      personality: null,
      instructions: null,
      provider: "claude",
      model: "opus",
      reasoning_effort: null,
      reasoning_required: false,
      fallback_agent: null,
      mode: "autonomous",
      isolation: "worktree",
      base_branch: "0.5.0",
      timeout: 120,
      default_workflow: "review",
      sandbox: { filesystem: "workspace-write" },
      workflows: {
        rules: ["review-rules"],
        skill_selectors: {
          include: ["*", "development-discipline"],
          exclude: ["browser-testing"],
        },
        skill_format: "full",
        custom_workflow_key: { enabled: true },
      },
      lifecycle_variables: { task_claimed: false },
      default_variables: { review_depth: "full" },
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

    expect(body.workflows).toEqual({
      rules: ["review-rules"],
      skill_selectors: {
        include: ["*", "development-discipline"],
        exclude: ["browser-testing"],
      },
      skill_format: "full",
      custom_workflow_key: { enabled: true },
    });
  });

  it("sends null when the final editable workflow values are cleared", () => {
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
    draft.ruleSelectors = null;
    draft.variables = {};
    draft.skills = [];

    expect(buildAgentDefinitionBody(draft).workflows).toBeNull();
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
      sandbox_config: { filesystem: "workspace-write" },
      lifecycle_variables: { task_claimed: false },
      default_variables: { review_depth: "full" },
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
    expect(body).not.toHaveProperty("sandbox");
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
