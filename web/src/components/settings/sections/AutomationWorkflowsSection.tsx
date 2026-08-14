import { SettingsSection, type SettingsSectionFields } from "./SettingsSection";
import { useSettingsSectionContext } from "./SettingsSectionContext";
import { SwitchField } from "../../activity/fields";
import { VariableDefaultsEditor } from "../VariableDefaultsEditor";
import {
  ListMapConfigField,
  NumberConfigField,
  NumberListConfigField,
  SchemaSelectField,
  StringListConfigField,
  Subsection,
  SwitchConfigField,
  TextAreaConfigField,
  TextConfigField,
} from "./configFields";

/**
 * Automation & Workflows settings (audit IA section 8). Owns the task system,
 * expansion, and validation config (under the hyphenated `gobby-tasks` key),
 * the workflow engine, tmux agent spawning, cron scheduler, automation loop,
 * and pipeline config groups — all draft-backed through the #17108 foundation.
 * Two surfaces are live rather than draft-backed: the rules-engine enforcement
 * toggle (`/api/rules`, threaded through context) and the variable-defaults
 * editor (`/api/variables`, self-contained in VariableDefaultsEditor).
 */

// All `gobby-tasks.*` draft paths live under the hyphenated key; getByPath
// splits on '.', so the hyphen stays intact as a single segment.
const TASKS_PATHS = [
  "gobby-tasks.enabled",
  "gobby-tasks.show_result_on_create",
];

const FILE_EXTRACTION_PATHS = [
  "gobby-tasks.file_extraction.file_extensions",
  "gobby-tasks.file_extraction.known_files",
  "gobby-tasks.file_extraction.path_prefixes",
];

const EXPANSION_PATHS = [
  "gobby-tasks.expansion.profile",
  "gobby-tasks.expansion.candidates",
  "gobby-tasks.expansion.enabled",
  "gobby-tasks.expansion.prompt_path",
  "gobby-tasks.expansion.system_prompt_path",
  "gobby-tasks.expansion.default_strategy",
  "gobby-tasks.expansion.timeout",
  "gobby-tasks.expansion.pattern_criteria.patterns",
  "gobby-tasks.expansion.pattern_criteria.detection_keywords",
];

const VALIDATION_PATHS = [
  "gobby-tasks.validation.profile",
  "gobby-tasks.validation.candidates",
  "gobby-tasks.validation.enabled",
  "gobby-tasks.validation.system_prompt",
  "gobby-tasks.validation.prompt_path",
  "gobby-tasks.validation.criteria_prompt_path",
  "gobby-tasks.validation.criteria_system_prompt",
  "gobby-tasks.validation.max_iterations",
  "gobby-tasks.validation.close_review_prompt_max_chars",
  "gobby-tasks.validation.escalation_enabled",
  "gobby-tasks.validation.escalation_notify",
  "gobby-tasks.validation.escalation_webhook_url",
  "gobby-tasks.validation.auto_generate_on_create",
  "gobby-tasks.validation.auto_generate_on_expand",
];

const WORKFLOW_PATHS = [
  "workflow.enabled",
  "workflow.timeout",
  "workflow.debug_echo_context",
];

const TMUX_PATHS = [
  "tmux.enabled",
  "tmux.command",
  "tmux.socket_name",
  "tmux.socket_path",
  "tmux.config_file",
  "tmux.session_prefix",
  "tmux.history_limit",
  "tmux.wsl_distribution",
  "tmux.idle_check_enabled",
  "tmux.idle_timeout_seconds",
  "tmux.idle_reprompt_delay_seconds",
  "tmux.max_reprompt_attempts",
  "tmux.reasoning_watchdog_interrupt_enabled",
  "tmux.reasoning_watchdog_settle_seconds",
  "tmux.init_timeout_seconds",
  "tmux.init_activity_grace_seconds",
  "tmux.registration_timeout_seconds",
  "tmux.auto_enter_approval_prompts",
  "tmux.auto_enter_agent_terminals",
  "tmux.auto_enter_agent_interval_seconds",
];

const CRON_PATHS = [
  "cron.enabled",
  "cron.check_interval_seconds",
  "cron.max_concurrent_jobs",
  "cron.running_timeout_seconds",
  "cron.cleanup_after_days",
  "cron.backoff_delays",
];

const SYSTEM_LOOPS_PATHS = [
  "system_loops.automation.enabled",
  "system_loops.automation.interval_seconds",
];

const PIPELINES_PATHS = [
  "pipelines.prompt_step.profile",
  "pipelines.prompt_step.candidates",
  "pipelines.nesting_depth_limit",
];

const OWNED_PATHS: readonly string[] = [
  ...TASKS_PATHS,
  ...FILE_EXTRACTION_PATHS,
  ...EXPANSION_PATHS,
  ...VALIDATION_PATHS,
  ...WORKFLOW_PATHS,
  ...TMUX_PATHS,
  ...CRON_PATHS,
  ...SYSTEM_LOOPS_PATHS,
  ...PIPELINES_PATHS,
];

function TasksGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Task system"
      hint="The gobby-tasks internal MCP server and how tasks extract affected files."
    >
      <SwitchConfigField
        fields={fields}
        path="gobby-tasks.enabled"
        label="Enable task system"
        ariaLabel="Enable task system"
      />
      <SwitchConfigField
        fields={fields}
        path="gobby-tasks.show_result_on_create"
        label="Show result on task create"
        ariaLabel="Show result on task create"
      />
      <StringListConfigField
        fields={fields}
        path="gobby-tasks.file_extraction.file_extensions"
        label="Task file extensions"
        ariaLabel="Task file extensions"
        addLabel="Add extension"
        placeholder=".py"
      />
      <StringListConfigField
        fields={fields}
        path="gobby-tasks.file_extraction.known_files"
        label="Known task files"
        ariaLabel="Known task files"
        addLabel="Add file"
        placeholder="README.md"
      />
      <StringListConfigField
        fields={fields}
        path="gobby-tasks.file_extraction.path_prefixes"
        label="Task path prefixes"
        ariaLabel="Task path prefixes"
        addLabel="Add prefix"
        placeholder="src/"
      />
    </Subsection>
  );
}

function ExpansionGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Task expansion"
      hint="LLM-driven decomposition of tasks into subtasks."
    >
      <SwitchConfigField
        fields={fields}
        path="gobby-tasks.expansion.enabled"
        label="Enable task expansion"
        ariaLabel="Enable task expansion"
      />
      <SchemaSelectField
        fields={fields}
        path="gobby-tasks.expansion.profile"
        label="Expansion capability profile"
        ariaLabel="Expansion capability profile"
      />
      <StringListConfigField
        fields={fields}
        path="gobby-tasks.expansion.candidates"
        label="Expansion model candidates"
        ariaLabel="Expansion model candidate"
        addLabel="Add candidate"
        placeholder="claude/sonnet"
      />
      <SchemaSelectField
        fields={fields}
        path="gobby-tasks.expansion.default_strategy"
        label="Default expansion strategy"
        ariaLabel="Default expansion strategy"
      />
      <TextConfigField
        fields={fields}
        path="gobby-tasks.expansion.prompt_path"
        label="Expansion prompt path"
        ariaLabel="Expansion prompt path"
        nullable
      />
      <TextConfigField
        fields={fields}
        path="gobby-tasks.expansion.system_prompt_path"
        label="Expansion system prompt path"
        ariaLabel="Expansion system prompt path"
        nullable
      />
      <NumberConfigField
        fields={fields}
        path="gobby-tasks.expansion.timeout"
        label="Expansion timeout (seconds)"
        ariaLabel="Expansion timeout (seconds)"
      />
      <ListMapConfigField
        fields={fields}
        path="gobby-tasks.expansion.pattern_criteria.patterns"
        label="Pattern criteria templates"
        ariaLabel="Pattern criteria templates"
        valueLabel="Criteria templates"
        addLabel="Add pattern"
        keyPlaceholder="pattern name"
        itemAddLabel="Add template"
      />
      <ListMapConfigField
        fields={fields}
        path="gobby-tasks.expansion.pattern_criteria.detection_keywords"
        label="Pattern detection keywords"
        ariaLabel="Pattern detection keywords"
        valueLabel="Keywords"
        addLabel="Add pattern"
        keyPlaceholder="pattern name"
        itemAddLabel="Add keyword"
      />
    </Subsection>
  );
}

function ValidationGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Task validation"
      hint="Automated validation of task completion against criteria."
    >
      <SwitchConfigField
        fields={fields}
        path="gobby-tasks.validation.enabled"
        label="Enable task validation"
        ariaLabel="Enable task validation"
      />
      <SchemaSelectField
        fields={fields}
        path="gobby-tasks.validation.profile"
        label="Validation capability profile"
        ariaLabel="Validation capability profile"
      />
      <StringListConfigField
        fields={fields}
        path="gobby-tasks.validation.candidates"
        label="Validation model candidates"
        ariaLabel="Validation model candidate"
        addLabel="Add candidate"
        placeholder="claude/sonnet"
      />
      <TextAreaConfigField
        fields={fields}
        path="gobby-tasks.validation.system_prompt"
        label="Validation system prompt"
        ariaLabel="Validation system prompt"
        rows={3}
      />
      <TextConfigField
        fields={fields}
        path="gobby-tasks.validation.prompt_path"
        label="Validation prompt path"
        ariaLabel="Validation prompt path"
        nullable
      />
      <TextConfigField
        fields={fields}
        path="gobby-tasks.validation.criteria_prompt_path"
        label="Criteria prompt path"
        ariaLabel="Criteria prompt path"
        nullable
      />
      <TextAreaConfigField
        fields={fields}
        path="gobby-tasks.validation.criteria_system_prompt"
        label="Criteria system prompt"
        ariaLabel="Criteria system prompt"
        rows={3}
      />
      <NumberConfigField
        fields={fields}
        path="gobby-tasks.validation.max_iterations"
        label="Max validation iterations"
        ariaLabel="Max validation iterations"
      />
      <NumberConfigField
        fields={fields}
        path="gobby-tasks.validation.close_review_prompt_max_chars"
        label="Close-review prompt limit (characters)"
        ariaLabel="Close-review prompt limit (characters)"
      />
      <SwitchConfigField
        fields={fields}
        path="gobby-tasks.validation.escalation_enabled"
        label="Enable escalation"
        ariaLabel="Enable escalation"
      />
      <SchemaSelectField
        fields={fields}
        path="gobby-tasks.validation.escalation_notify"
        label="Escalation notify method"
        ariaLabel="Escalation notify method"
      />
      <TextConfigField
        fields={fields}
        path="gobby-tasks.validation.escalation_webhook_url"
        label="Escalation webhook URL"
        ariaLabel="Escalation webhook URL"
        nullable
      />
      <SwitchConfigField
        fields={fields}
        path="gobby-tasks.validation.auto_generate_on_create"
        label="Auto-generate criteria on create"
        ariaLabel="Auto-generate criteria on create"
      />
      <SwitchConfigField
        fields={fields}
        path="gobby-tasks.validation.auto_generate_on_expand"
        label="Auto-generate criteria on expand"
        ariaLabel="Auto-generate criteria on expand"
      />
    </Subsection>
  );
}

function WorkflowEngineGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Workflow engine"
      hint="Rule-engine and workflow execution behavior."
    >
      <SwitchConfigField
        fields={fields}
        path="workflow.enabled"
        label="Enable workflow engine"
        ariaLabel="Enable workflow engine"
      />
      <NumberConfigField
        fields={fields}
        path="workflow.timeout"
        label="Workflow timeout (seconds)"
        ariaLabel="Workflow timeout (seconds)"
      />
      <SwitchConfigField
        fields={fields}
        path="workflow.debug_echo_context"
        label="Echo context for debugging"
        ariaLabel="Echo context for debugging"
      />
    </Subsection>
  );
}

function TmuxGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Tmux agents"
      hint="How spawned agents run inside tmux sessions."
    >
      <SwitchConfigField
        fields={fields}
        path="tmux.enabled"
        label="Enable tmux agents"
        ariaLabel="Enable tmux agents"
      />
      <TextConfigField
        fields={fields}
        path="tmux.command"
        label="tmux command"
        ariaLabel="tmux command"
        placeholder="tmux"
      />
      <TextConfigField
        fields={fields}
        path="tmux.socket_name"
        label="Socket name"
        ariaLabel="Socket name"
      />
      <TextConfigField
        fields={fields}
        path="tmux.socket_path"
        label="Socket path"
        ariaLabel="Socket path"
        nullable
      />
      <TextConfigField
        fields={fields}
        path="tmux.config_file"
        label="tmux config file"
        ariaLabel="tmux config file"
        nullable
      />
      <TextConfigField
        fields={fields}
        path="tmux.session_prefix"
        label="Session prefix"
        ariaLabel="Session prefix"
      />
      <NumberConfigField
        fields={fields}
        path="tmux.history_limit"
        label="Scrollback history limit"
        ariaLabel="Scrollback history limit"
      />
      <TextConfigField
        fields={fields}
        path="tmux.wsl_distribution"
        label="WSL distribution"
        ariaLabel="WSL distribution"
        nullable
      />
      <SwitchConfigField
        fields={fields}
        path="tmux.idle_check_enabled"
        label="Idle detection"
        ariaLabel="Idle detection"
      />
      <NumberConfigField
        fields={fields}
        path="tmux.idle_timeout_seconds"
        label="Idle timeout (seconds)"
        ariaLabel="Idle timeout (seconds)"
      />
      <NumberConfigField
        fields={fields}
        path="tmux.idle_reprompt_delay_seconds"
        label="Idle reprompt delay (seconds)"
        ariaLabel="Idle reprompt delay (seconds)"
      />
      <NumberConfigField
        fields={fields}
        path="tmux.max_reprompt_attempts"
        label="Max reprompt attempts"
        ariaLabel="Max reprompt attempts"
      />
      <SwitchConfigField
        fields={fields}
        path="tmux.reasoning_watchdog_interrupt_enabled"
        label="Reasoning watchdog interrupt"
        ariaLabel="Reasoning watchdog interrupt"
      />
      <NumberConfigField
        fields={fields}
        path="tmux.reasoning_watchdog_settle_seconds"
        label="Reasoning watchdog settle (seconds)"
        ariaLabel="Reasoning watchdog settle (seconds)"
        step={0.5}
      />
      <NumberConfigField
        fields={fields}
        path="tmux.init_timeout_seconds"
        label="Init timeout (seconds)"
        ariaLabel="Init timeout (seconds)"
      />
      <NumberConfigField
        fields={fields}
        path="tmux.init_activity_grace_seconds"
        label="Init activity grace (seconds)"
        ariaLabel="Init activity grace (seconds)"
        step={0.5}
      />
      <NumberConfigField
        fields={fields}
        path="tmux.registration_timeout_seconds"
        label="Registration timeout (seconds)"
        ariaLabel="Registration timeout (seconds)"
        step={0.5}
      />
      <SwitchConfigField
        fields={fields}
        path="tmux.auto_enter_approval_prompts"
        label="Auto-enter approval prompts"
        ariaLabel="Auto-enter approval prompts"
      />
      <SwitchConfigField
        fields={fields}
        path="tmux.auto_enter_agent_terminals"
        label="Auto-enter agent terminals"
        ariaLabel="Auto-enter agent terminals"
      />
      <NumberConfigField
        fields={fields}
        path="tmux.auto_enter_agent_interval_seconds"
        label="Auto-enter agent interval (seconds)"
        ariaLabel="Auto-enter agent interval (seconds)"
      />
    </Subsection>
  );
}

function CronGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Cron scheduler"
      hint="Scheduled job execution and retry backoff."
    >
      <SwitchConfigField
        fields={fields}
        path="cron.enabled"
        label="Enable cron scheduler"
        ariaLabel="Enable cron scheduler"
      />
      <NumberConfigField
        fields={fields}
        path="cron.check_interval_seconds"
        label="Check interval (seconds)"
        ariaLabel="Check interval (seconds)"
      />
      <NumberConfigField
        fields={fields}
        path="cron.max_concurrent_jobs"
        label="Max concurrent jobs"
        ariaLabel="Max concurrent jobs"
      />
      <NumberConfigField
        fields={fields}
        path="cron.running_timeout_seconds"
        label="Running timeout (seconds)"
        ariaLabel="Running timeout (seconds)"
      />
      <NumberConfigField
        fields={fields}
        path="cron.cleanup_after_days"
        label="Cleanup after (days)"
        ariaLabel="Cleanup after (days)"
      />
      <NumberListConfigField
        fields={fields}
        path="cron.backoff_delays"
        label="Retry backoff delays (seconds)"
        ariaLabel="Retry backoff delay"
        addLabel="Add delay"
      />
    </Subsection>
  );
}

function SystemLoopsGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Automation loop"
      hint="The daemon-owned loop that advances built tasks."
    >
      <SwitchConfigField
        fields={fields}
        path="system_loops.automation.enabled"
        label="Enable automation loop"
        ariaLabel="Enable automation loop"
      />
      <NumberConfigField
        fields={fields}
        path="system_loops.automation.interval_seconds"
        label="Automation loop interval (seconds)"
        ariaLabel="Automation loop interval (seconds)"
      />
    </Subsection>
  );
}

function PipelinesGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Pipelines"
      hint="Deterministic pipeline execution and prompt-step model selection."
    >
      <SchemaSelectField
        fields={fields}
        path="pipelines.prompt_step.profile"
        label="Pipeline prompt capability profile"
        ariaLabel="Pipeline prompt capability profile"
      />
      <StringListConfigField
        fields={fields}
        path="pipelines.prompt_step.candidates"
        label="Pipeline prompt model candidates"
        ariaLabel="Pipeline prompt model candidate"
        addLabel="Add candidate"
        placeholder="claude/sonnet"
      />
      <NumberConfigField
        fields={fields}
        path="pipelines.nesting_depth_limit"
        label="Pipeline nesting depth limit"
        ariaLabel="Pipeline nesting depth limit"
      />
    </Subsection>
  );
}

function RulesEnforcementGroup({
  rulesEnforcement,
  setRulesEnforcement,
}: {
  rulesEnforcement?: boolean;
  setRulesEnforcement?: (enabled: boolean) => Promise<boolean>;
}) {
  return (
    <Subsection
      title="Rules engine"
      hint="Whether declarative rules are enforced. Saved immediately, separate from the config draft below."
    >
      {setRulesEnforcement ? (
        <SwitchField
          label="Enforce rules engine"
          ariaLabel="Enforce rules engine"
          value={rulesEnforcement === true}
          onChange={(next) => {
            void setRulesEnforcement(next);
          }}
        />
      ) : (
        <p className="rounded-lg border border-border bg-muted px-5 py-4 text-sm leading-[1.5] text-foreground-muted">
          Rules enforcement is unavailable.
        </p>
      )}
    </Subsection>
  );
}

function WorkflowVariablesGroup() {
  return (
    <Subsection
      title="Workflow variables"
      hint="Session variable defaults. Edited live, separate from the config draft below."
    >
      <VariableDefaultsEditor />
    </Subsection>
  );
}

export function AutomationWorkflowsSection() {
  const { rulesEnforcement, setRulesEnforcement } = useSettingsSectionContext();
  return (
    <SettingsSection sectionId="automation-workflows" ownedPaths={OWNED_PATHS}>
      {(fields) => (
        <>
          <RulesEnforcementGroup
            rulesEnforcement={rulesEnforcement}
            setRulesEnforcement={setRulesEnforcement}
          />
          <WorkflowVariablesGroup />
          <TasksGroup fields={fields} />
          <ExpansionGroup fields={fields} />
          <ValidationGroup fields={fields} />
          <WorkflowEngineGroup fields={fields} />
          <TmuxGroup fields={fields} />
          <CronGroup fields={fields} />
          <SystemLoopsGroup fields={fields} />
          <PipelinesGroup fields={fields} />
        </>
      )}
    </SettingsSection>
  );
}
