import { useState } from "react";
import { SettingsSection, type SettingsSectionFields } from "./SettingsSection";
import {
  useSettingsSectionContext,
  type ProjectSelectionContextValue,
} from "./SettingsSectionContext";
import { ProjectSelectField } from "../../activity/fields";
import { ValidationDetectionEditor } from "../../ValidationDetectionEditor";
import {
  MapConfigField,
  NumberConfigField,
  SchemaSelectField,
  StringListConfigField,
  Subsection,
  SwitchConfigField,
  TextAreaConfigField,
  TextConfigField,
} from "./configFields";

/**
 * Projects & Sessions settings (audit IA section 4). Owns the active-project
 * client preference plus the session-lifecycle, summary, handoff, history,
 * tracking, verification-default, and validation-detection config
 * groups. Built on the #17108 foundation: the active project is a live
 * client preference threaded through `projectSelection` (App owns and persists
 * it to `ui_settings.selectedProjectId`); everything else flows through the
 * per-section draft via the shared config-field wrappers.
 */

const SESSION_SUMMARY_PATHS = [
  "session_summary.profile",
  "session_summary.candidates",
  "session_summary.enabled",
  "session_summary.prompt",
  "session_summary.summary_file_path",
];

const SESSION_LIFECYCLE_PATHS = [
  "session_lifecycle.active_session_pause_minutes",
  "session_lifecycle.stale_session_timeout_hours",
  "session_lifecycle.expire_check_interval_minutes",
  "session_lifecycle.transcript_processing_interval_minutes",
  "session_lifecycle.transcript_processing_batch_size",
  "session_lifecycle.transcript_archive_dir",
];

const CHAT_HISTORY_PATHS = [
  "chat_history.max_message_chars",
  "chat_history.max_total_chars",
];

const MESSAGE_TRACKING_PATHS = [
  "message_tracking.enabled",
  "message_tracking.poll_interval",
  "message_tracking.debounce_delay",
  "message_tracking.max_message_length",
  "message_tracking.broadcast_enabled",
];

const VERIFICATION_DEFAULTS_PATHS = [
  "verification_defaults.unit_tests",
  "verification_defaults.type_check",
  "verification_defaults.lint",
  "verification_defaults.format",
  "verification_defaults.build",
  "verification_defaults.doc_tests",
  "verification_defaults.integration",
  "verification_defaults.security",
  "verification_defaults.code_review",
  "verification_defaults.custom",
];

// `validation_detection` is owned as a single object subtree, edited as a whole
// through ValidationDetectionEditor. This one path covers its sub-fields:
// enabled, builtin_matchers_enabled, disabled_builtin_matcher_ids,
// recognized_wrappers, wrapper_rules, and custom_matchers.
const VALIDATION_DETECTION_PATH = "validation_detection";

const OWNED_PATHS: readonly string[] = [
  ...SESSION_LIFECYCLE_PATHS,
  ...SESSION_SUMMARY_PATHS,
  ...CHAT_HISTORY_PATHS,
  ...MESSAGE_TRACKING_PATHS,
  ...VERIFICATION_DEFAULTS_PATHS,
  VALIDATION_DETECTION_PATH,
];

// Verification commands are all optional `str | None` daemon fields; an empty
// input clears back to the default (null). Placeholders show the canonical
// command shape without forcing it.
const VERIFICATION_COMMAND_FIELDS: ReadonlyArray<[string, string, string]> = [
  ["unit_tests", "Unit tests", "uv run pytest tests/ -v"],
  ["type_check", "Type check", "uv run mypy src/"],
  ["lint", "Lint", "uv run ruff check src/"],
  ["format", "Format check", "uv run ruff format --check src/"],
  ["build", "Build", ""],
  ["doc_tests", "Doc tests", ""],
  ["integration", "Integration tests", ""],
  ["security", "Security scan", "bandit -r src/"],
  ["code_review", "Code review", "coderabbit review --ci"],
];

function ProjectSelectionGroup({
  projectSelection,
}: {
  projectSelection?: ProjectSelectionContextValue;
}) {
  if (!projectSelection) {
    return (
      <Subsection
        title="Project selection"
        hint="The project new sessions and tasks attach to."
      >
        <p className="rounded-lg border border-border bg-muted px-5 py-4 text-sm leading-[1.5] text-foreground-muted">
          Project selection is unavailable.
        </p>
      </Subsection>
    );
  }
  return (
    <Subsection
      title="Project selection"
      hint="The active project new sessions and tasks attach to. Stored as ui_settings.selectedProjectId and shared with the top-bar selector."
    >
      <ProjectSelectField
        label="Active project"
        ariaLabel="Active project"
        value={projectSelection.selectedProjectId ?? ""}
        onChange={projectSelection.onSelectProject}
      />
    </Subsection>
  );
}

function SessionLifecycleGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Session lifecycle"
      hint="When sessions pause and expire, and how transcripts are processed."
    >
      <NumberConfigField
        fields={fields}
        path="session_lifecycle.active_session_pause_minutes"
        label="Pause active sessions after (minutes)"
        ariaLabel="Pause active sessions after (minutes)"
      />
      <NumberConfigField
        fields={fields}
        path="session_lifecycle.stale_session_timeout_hours"
        label="Expire stale sessions after (hours)"
        ariaLabel="Expire stale sessions after (hours)"
      />
      <NumberConfigField
        fields={fields}
        path="session_lifecycle.expire_check_interval_minutes"
        label="Expiry check interval (minutes)"
        ariaLabel="Expiry check interval (minutes)"
      />
      <NumberConfigField
        fields={fields}
        path="session_lifecycle.transcript_processing_interval_minutes"
        label="Transcript processing interval (minutes)"
        ariaLabel="Transcript processing interval (minutes)"
      />
      <NumberConfigField
        fields={fields}
        path="session_lifecycle.transcript_processing_batch_size"
        label="Transcript processing batch size"
        ariaLabel="Transcript processing batch size"
      />
      <TextConfigField
        fields={fields}
        path="session_lifecycle.transcript_archive_dir"
        label="Transcript archive directory"
        ariaLabel="Transcript archive directory"
        placeholder="~/.gobby/session_transcripts"
      />
    </Subsection>
  );
}

function SessionSummaryGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Session summaries"
      hint="LLM-generated handoff summaries written when a session ends."
    >
      <SwitchConfigField
        fields={fields}
        path="session_summary.enabled"
        label="Generate session summaries"
        ariaLabel="Generate session summaries"
      />
      <SchemaSelectField
        fields={fields}
        path="session_summary.profile"
        label="Summary capability profile"
        ariaLabel="Summary capability profile"
      />
      <StringListConfigField
        fields={fields}
        path="session_summary.candidates"
        label="Summary model candidates"
        ariaLabel="Summary model candidate"
        addLabel="Add candidate"
        placeholder="provider/model"
      />
      <TextConfigField
        fields={fields}
        path="session_summary.summary_file_path"
        label="Summary directory"
        ariaLabel="Summary directory"
        placeholder=".gobby/session_summaries"
      />
      <TextAreaConfigField
        fields={fields}
        path="session_summary.prompt"
        label="Summary prompt template"
        ariaLabel="Session summary prompt template"
        rows={8}
      />
    </Subsection>
  );
}

function ChatHistoryGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Chat history"
      hint="Limits on the history context injected into chats."
    >
      <NumberConfigField
        fields={fields}
        path="chat_history.max_message_chars"
        label="Max characters per message"
        ariaLabel="Max characters per message"
      />
      <NumberConfigField
        fields={fields}
        path="chat_history.max_total_chars"
        label="Max total characters"
        ariaLabel="Max total characters"
      />
    </Subsection>
  );
}

function MessageTrackingGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Message tracking"
      hint="How transcript messages are polled and broadcast."
    >
      <SwitchConfigField
        fields={fields}
        path="message_tracking.enabled"
        label="Enable message tracking"
        ariaLabel="Enable message tracking"
      />
      <NumberConfigField
        fields={fields}
        path="message_tracking.poll_interval"
        label="Poll interval (seconds)"
        ariaLabel="Poll interval (seconds)"
        step={0.5}
      />
      <NumberConfigField
        fields={fields}
        path="message_tracking.debounce_delay"
        label="Debounce delay (seconds)"
        ariaLabel="Debounce delay (seconds)"
        step={0.5}
      />
      <NumberConfigField
        fields={fields}
        path="message_tracking.max_message_length"
        label="Max message length"
        ariaLabel="Max message length"
      />
      <SwitchConfigField
        fields={fields}
        path="message_tracking.broadcast_enabled"
        label="Broadcast message events"
        ariaLabel="Broadcast message events"
      />
    </Subsection>
  );
}

function VerificationDefaultsGroup({
  fields,
}: {
  fields: SettingsSectionFields;
}) {
  return (
    <Subsection
      title="Verification defaults"
      hint="Default validation commands applied to new projects."
    >
      {VERIFICATION_COMMAND_FIELDS.map(([key, label, placeholder]) => (
        <TextConfigField
          key={key}
          fields={fields}
          path={`verification_defaults.${key}`}
          label={label}
          ariaLabel={`${label} command`}
          placeholder={placeholder || undefined}
          nullable
        />
      ))}
      <MapConfigField
        fields={fields}
        path="verification_defaults.custom"
        label="Custom commands"
        ariaLabel="Custom verification command"
        addLabel="Add command"
        keyPlaceholder="name"
      />
    </Subsection>
  );
}

function ValidationDetectionGroup({
  fields,
  onValidityChange,
}: {
  fields: SettingsSectionFields;
  onValidityChange: (isValid: boolean) => void;
}) {
  return (
    <Subsection
      title="Validation detection"
      hint="Recognize validation commands in agent shell calls. Edit the matcher config as JSON, then preview a command to test it."
    >
      <ValidationDetectionEditor
        value={fields.getValue(VALIDATION_DETECTION_PATH)}
        onChange={(next) => fields.setValue(VALIDATION_DETECTION_PATH, next)}
        onValidityChange={onValidityChange}
      />
    </Subsection>
  );
}

export function ProjectsSessionsSection() {
  const { projectSelection } = useSettingsSectionContext();
  const [validationDetectionValid, setValidationDetectionValid] =
    useState(true);
  return (
    <SettingsSection
      sectionId="projects-sessions"
      ownedPaths={OWNED_PATHS}
      saveDisabled={!validationDetectionValid}
    >
      {(fields) => (
        <>
          <ProjectSelectionGroup projectSelection={projectSelection} />
          <SessionLifecycleGroup fields={fields} />
          <SessionSummaryGroup fields={fields} />
          <ChatHistoryGroup fields={fields} />
          <MessageTrackingGroup fields={fields} />
          <VerificationDefaultsGroup fields={fields} />
          <ValidationDetectionGroup
            fields={fields}
            onValidityChange={setValidationDetectionValid}
          />
        </>
      )}
    </SettingsSection>
  );
}
