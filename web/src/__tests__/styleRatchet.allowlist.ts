// Style-debt ratchet allowlist — the recorded ceiling for legacy styling
// idioms (see docs/guides/frontend-style-guide.md).
//
// Attrition contract: entries may only be DELETED or DECREASED. Never add an
// entry, never increase a count, never raise the line ceiling. When you migrate
// a file (onto components/ui primitives + Tailwind utilities), the stale-count
// check in styleRatchet.test.ts forces you to shrink its entry here so the
// ratchet can never loosen.

// `btn`/`btn-*` class tokens per file (string literals in ts/tsx). Empty:
// the .btn system is retired — this is a pure ban.
export const BTN_CLASS_ALLOWLIST: Record<string, number> = {}

export type RawElement = 'button' | 'input' | 'select' | 'textarea'

// Raw interactive JSX elements per file (components/ui itself is exempt —
// primitives have to render the real element).
export const RAW_ELEMENT_ALLOWLIST: Record<RawElement, Record<string, number>> = {
  button: {
    'src/App.tsx': 1,
    'src/components/ProjectSelector.tsx': 2,
    'src/components/ValidationDetectionEditor.tsx': 1,
    'src/components/activity/wiki/WikiAskMode.tsx': 7, // 4.11 deferral: Ask surface replacement
    'src/components/app/AppErrorBoundary.tsx': 2,
    'src/components/auth/LoginPage.tsx': 1,
    'src/components/chat/ActiveAgentIndicator.tsx': 1,
    'src/components/chat/AgentPickerDropdown.tsx': 4,
    'src/components/chat/BranchIndicator.tsx': 3,
    'src/components/chat/ChatCommandPalette.tsx': 1,
    'src/components/chat/ChatInput.tsx': 1,
    'src/components/chat/ChatInputModelControls.tsx': 1,
    'src/components/chat/ChatInputPrimaryButton.tsx': 1,
    'src/components/chat/ChatInputQueuedFiles.tsx': 2,
    'src/components/chat/CodeBlockRenderers.tsx': 1,
    'src/components/chat/CommandBar.tsx': 1,
    'src/components/chat/ProviderPicker.tsx': 3,
    'src/components/chat/ResumeSessionModal.tsx': 1,
    'src/components/chat/ToolCallCard.tsx': 2,
    'src/components/chat/ToolResultImage.tsx': 1,
    'src/components/command-browser/SkillBrowserModal.tsx': 3,
    'src/components/command-browser/ToolBrowserModal.tsx': 4,
    'src/components/settings/SettingsOverlay.tsx': 2,
    'src/components/settings/sections/PromptsTemplatesSection.tsx': 1,
    'src/components/shared/DiffBlock.tsx': 1,
    'src/components/shared/MermaidBlock.tsx': 1,
  },
  input: {
    'src/components/ProjectSelector.tsx': 1,
    'src/components/ValidationDetectionEditor.tsx': 1,
    'src/components/auth/LoginPage.tsx': 3,
    'src/components/chat/ChatInputToolbar.tsx': 1,
    'src/components/chat/CommandPalette.tsx': 1,
    'src/components/chat/ResumeSessionModal.tsx': 2,
    'src/components/chat/ToolCallCard.tsx': 1,
    'src/components/command-browser/ToolArgumentForm.tsx': 1,
    'src/components/settings/sections/AppearanceSection.tsx': 1,
    'src/components/settings/sections/McpToolsSection.tsx': 1,
    'src/components/settings/sections/PromptsTemplatesSection.tsx': 1,
    'src/components/settings/sections/ToolApprovalsSection.tsx': 1,
  },
  select: {
    'src/components/command-browser/ToolArgumentForm.tsx': 1,
  },
  textarea: {
    'src/components/ValidationDetectionEditor.tsx': 1,
    'src/components/activity/wiki/WikiAskMode.tsx': 1, // 4.11 deferral: Ask surface replacement
    'src/components/chat/ChatInput.tsx': 1,
    'src/components/chat/PlanApprovalActions.tsx': 1,
    'src/components/command-browser/ToolArgumentForm.tsx': 1,
  },
}

// `const *_CLS =` style-constant declarations per file.
export const CLS_CONSTANT_ALLOWLIST: Record<string, number> = {
  'src/components/ValidationDetectionEditor.tsx': 9,
  'src/components/chat/AgentPickerDropdown.tsx': 11,
}

// The complete recorded stylesheet set. New .css files are banned outright.
export const CSS_FILE_ALLOWLIST: readonly string[] = [
  'src/components/activity/skills/SkillsTab.css',
  'src/components/activity/taskdetail/task-detail.css',
  'src/components/chat/styles.css',
  'src/components/chat/styles/activity-panel.css',
  'src/components/chat/styles/cron-tab.css',
  'src/components/chat/styles/empty-state.css',
  'src/components/chat/styles/files-tab.css',
  'src/components/chat/styles/input-base.css',
  'src/components/chat/styles/input-composer.css',
  'src/components/chat/styles/input-responsive.css',
  'src/components/chat/styles/input-status.css',
  'src/components/chat/styles/input-voice.css',
  'src/components/chat/styles/input.css',
  'src/components/chat/styles/layout.css',
  'src/components/chat/styles/mcp-tab.css',
  'src/components/chat/styles/message.css',
  'src/components/chat/styles/pipelines-tab.css',
  'src/components/chat/styles/rules-tab.css',
  'src/components/chat/styles/sessions-tab.css',
  'src/components/chat/styles/traces-tab.css',
  'src/components/chat/styles/variables.css',
  'src/components/tasks/task-execution.css',
  'src/styles/accessibility.css',
  'src/styles/app-shell.css',
  'src/styles/base.css',
  'src/styles/dropdown-caret.css',
  'src/styles/index.css',
  'src/styles/markdown.css',
  'src/styles/segmented-control.css',
  'src/styles/settings-overlay.css',
  'src/styles/tailwind-theme.css',
  'src/styles/tokens.css',
]

// `!important` occurrences per file.
export const IMPORTANT_ALLOWLIST: Record<string, number> = {
  'src/components/chat/styles.css': 1,
  'src/components/chat/styles/input-voice.css': 1,
  'src/styles/base.css': 4,
}

// Total lines across all recorded stylesheets. The ceiling only moves down;
// once actual drops more than the slack below it, the test demands a tighten.
export const CSS_TOTAL_LINE_CEILING = 6600
export const CSS_LINE_TIGHTEN_SLACK = 200
