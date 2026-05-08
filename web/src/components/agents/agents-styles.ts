// Shared Tailwind class constants for the agents/ surface.
//
// Migrated from agents.css and AgentStepsEditor.css under #13674. Consumers
// (AgentEditForm, AgentPortfolioPage, AgentRulesEditor, AgentSkillsEditor,
// AgentStepsEditor, AgentToolBlocksEditor, AgentVariablesEditor,
// IsolationTargetSelector, plus workflows/AgentsTab) import these names
// instead of relying on a side-effect CSS import.

// ── Shared agent buttons ──

export const AGENT_BTN_CLS =
  "px-3 py-1 border border-border rounded-md bg-[var(--bg-tertiary)] text-[var(--text-primary)] text-[length:calc(var(--font-size-base)*0.75)] font-medium cursor-pointer transition-colors hover:bg-[var(--bg-secondary)] hover:border-[var(--text-muted)] disabled:opacity-50 disabled:cursor-not-allowed pointer-coarse:min-h-11";
export const AGENT_BTN_PRIMARY_CLS =
  "bg-[var(--accent)] border-[var(--accent)] text-[var(--accent-foreground)] hover:bg-[var(--accent-hover)] hover:border-[var(--accent-hover)]";
export const AGENT_BTN_DANGER_CLS =
  "bg-transparent border-[var(--color-error)] text-[var(--color-error)] hover:bg-[color-mix(in_srgb,var(--color-error)_10%,transparent)]";

// ── Tab container ──

export const AGENT_DEFS_TAB_CLS = "flex flex-col flex-1 overflow-hidden";

// ── Edit form chrome ──

export const AGENT_EDIT_YAML_VIEW_CLS =
  "h-full [&_.codemirror-container]:h-full";

export const AGENT_EDIT_META_CLS =
  "px-5 py-3 border-b border-border";
export const AGENT_EDIT_META_ROW_CLS =
  "flex items-center justify-between py-1 text-sm";
export const AGENT_EDIT_META_LABEL_CLS =
  "text-[var(--text-muted)] shrink-0 mr-3";
export const AGENT_EDIT_META_VALUE_CLS =
  "flex-1 max-w-[220px] text-right [&_select]:w-full [&_input[type=number]]:w-full";

export const AGENT_EDIT_FIELD_CLS = "flex flex-col gap-1";
export const AGENT_EDIT_FIELD_DISABLED_CLS = "opacity-50 pointer-events-none";
export const AGENT_EDIT_LABEL_CLS =
  "text-[length:calc(var(--font-size-base)*0.6875)] text-[var(--text-muted)] uppercase tracking-[0.3px]";
export const AGENT_EDIT_HINT_CLS =
  "font-normal italic ml-1 opacity-60 normal-case tracking-normal";

export const AGENT_EDIT_INPUT_CLS =
  "bg-[var(--bg-primary)] border border-border rounded-md px-2.5 py-1.5 text-[length:calc(var(--font-size-base)*0.8125)] text-[var(--text-primary)] font-[inherit] outline-none transition-colors focus:border-[var(--accent)] focus:shadow-[0_0_0_2px_color-mix(in_srgb,var(--accent)_20%,transparent)]";
export const AGENT_EDIT_TEXTAREA_CLS =
  "font-[inherit] leading-[1.4] min-h-[3.6em]";

export const AGENT_EDIT_MODEL_FIELD_CLS = "flex items-center gap-1";
export const AGENT_EDIT_MODEL_TOGGLE_CLS =
  "bg-transparent border border-border rounded text-[var(--text-muted)] cursor-pointer px-1.5 py-1 text-[length:calc(var(--font-size-base)*0.875)] leading-none transition-colors hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11";

export const AGENT_EDIT_CODEMIRROR_CLS =
  "min-h-[200px] max-h-[400px] overflow-hidden border border-border rounded-md [&_.codemirror-container]:h-[200px]";

export const AGENT_EDIT_SECTION_CLS =
  "px-5 py-3 border-b border-border flex flex-col gap-1.5";
export const AGENT_EDIT_SECTION_TITLE_CLS =
  "text-sm font-semibold text-[var(--text-muted)] uppercase tracking-wider mb-1 mt-0";

export const AGENT_EDIT_LINK_BTN_CLS =
  "bg-transparent border-0 text-[var(--accent)] underline cursor-pointer text-[length:calc(var(--font-size-base)*0.75)] p-0 hover:text-[var(--accent-hover)]";

// Checkbox affordances — these had no CSS rule before (orphaned during a
// prior partial migration); give them a sensible default inline so the
// label sits next to the checkbox with consistent spacing.
export const AGENT_EDIT_CHECKBOX_CLS =
  "flex items-center gap-1.5 cursor-pointer text-[length:calc(var(--font-size-base)*0.8125)] text-[var(--text-primary)] select-none";
export const AGENT_EDIT_CHECKBOX_GROUP_CLS = "flex flex-col gap-1.5";

// ── Rules editor ──

export const AGENT_RULES_EDITOR_CLS = "flex flex-col gap-2";
export const AGENT_RULES_CHIPS_CLS = "flex flex-wrap gap-1.5 items-center";
export const AGENT_RULES_CHIP_CLS =
  "inline-flex items-center gap-1 bg-[var(--bg-tertiary)] border border-border rounded-xl pl-2.5 pr-2 py-0.5 text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-primary)]";
export const AGENT_RULES_CHIP_REMOVE_CLS =
  "bg-none border-0 text-[var(--text-muted)] cursor-pointer text-[length:calc(var(--font-size-base)*0.875)] leading-none px-0.5 transition-colors hover:text-[var(--color-error)]";
export const AGENT_RULES_CHIP_SELECTOR_CLS = "border-dashed";
export const AGENT_RULES_CHIP_INCLUDE_CLS =
  "border-[var(--color-info)] text-[var(--color-info)]";
export const AGENT_RULES_CHIP_EXCLUDE_CLS =
  "border-[var(--color-error)] text-[var(--color-error)]";
export const AGENT_RULES_EMPTY_CLS =
  "text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)] italic";
export const AGENT_RULES_ADD_SELECT_CLS =
  "max-w-[200px] text-[length:calc(var(--font-size-base)*0.75)]";
export const AGENT_RULES_ADD_BTN_CLS =
  "text-[length:calc(var(--font-size-base)*0.6875)]! px-2 py-0.5! self-start";

export const AGENT_RULE_SELECTORS_CLS =
  "mt-2.5 pt-2.5 border-t border-border flex flex-col gap-2";
export const AGENT_RULE_SELECTORS_LABEL_CLS =
  "text-sm font-semibold text-[var(--text-muted)] uppercase tracking-wider";
export const AGENT_RULE_SELECTOR_GROUP_CLS = "flex flex-col gap-1";
export const AGENT_RULE_SELECTOR_HEADING_CLS =
  "text-[length:calc(var(--font-size-base)*0.6875)] text-[var(--text-muted)] uppercase tracking-wider";
export const AGENT_RULE_SELECTOR_INPUT_ROW_CLS = "flex gap-1 items-center";
export const AGENT_RULE_SELECTOR_PREFIX_CLS =
  "w-20 shrink-0 text-[length:calc(var(--font-size-base)*0.75)]!";
export const AGENT_RULE_SELECTOR_VALUE_WRAP_CLS = "flex-1 min-w-0";

// ── Variables editor ──

export const AGENT_VARS_EDITOR_CLS = "flex flex-col gap-2";
export const AGENT_VARS_LIST_CLS = "flex flex-col gap-1";
export const AGENT_VARS_ROW_CLS =
  "flex items-center gap-2 text-[length:calc(var(--font-size-base)*0.75)]";
export const AGENT_VARS_KEY_CLS =
  "font-semibold text-[var(--text-primary)] min-w-[80px]";
export const AGENT_VARS_VALUE_CLS =
  "text-[var(--text-muted)] flex-1 overflow-hidden text-ellipsis whitespace-nowrap";
export const AGENT_VARS_ADD_ROW_CLS = "flex gap-1.5 items-center";
export const AGENT_VARS_ADD_INPUT_CLS = `${AGENT_EDIT_INPUT_CLS} flex-1 min-w-0 text-[length:calc(var(--font-size-base)*0.75)] px-2 py-1`;

// ── Card grid / cards ──

export const AGENT_DEFS_EMPTY_CLS =
  "text-center p-10 text-[var(--text-muted)] text-[length:calc(var(--font-size-base)*0.75)]";
export const AGENT_DEFS_GRID_CLS =
  "grid grid-cols-[repeat(auto-fill,minmax(340px,1fr))] gap-2.5";

export const AGENT_DEF_CARD_CLS =
  "bg-[var(--bg-secondary)] border border-border rounded-lg overflow-hidden transition-colors hover:border-[var(--text-muted)]";
export const AGENT_DEF_CARD_EXPANDED_CLS =
  "border-[var(--accent)] col-span-full";
export const AGENT_DEF_CARD_DELETED_CLS =
  "opacity-50 border-dashed hover:opacity-70";
export const AGENT_DEF_NAME_DELETED_CLS = "line-through";

// Agent-card overrides for the workflows-card-footer rendered inside
// agent-def-card (the agent surface uses a tighter padding than the
// generic workflow card footer).
export const AGENT_DEF_CARD_FOOTER_PAD_CLS = "px-4 pt-2.5 pb-4";

export const AGENT_DEF_HEADER_CLS =
  "flex flex-col gap-1.5 p-4 w-full bg-none border-0 text-[var(--text-primary)] cursor-pointer text-left font-sans hover:bg-[var(--bg-tertiary)]";
export const AGENT_DEF_HEADER_TOP_CLS =
  "flex items-center justify-between";
export const AGENT_DEF_NAME_CLS =
  "font-semibold text-[length:calc(var(--font-size-base)*0.875)] text-[var(--text-primary)] font-sans";
export const AGENT_DEF_CHEVRON_CLS =
  "text-[var(--text-muted)] text-[length:calc(var(--font-size-base)*0.625)]";
export const AGENT_DEF_DESC_CLS =
  "text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)] leading-tight overflow-hidden text-ellipsis whitespace-nowrap";

export const AGENT_DEF_BADGES_CLS = "flex flex-wrap gap-[5px] mt-0.5";

export const AGENT_DEF_BADGE_CLS =
  "text-[length:calc(var(--font-size-base)*0.625)] px-[7px] py-px rounded-[10px] border border-current font-medium whitespace-nowrap";
export const AGENT_DEF_BADGE_FILLED_CLS =
  "border-0 text-[oklch(15%_0_0)] font-semibold";
export const AGENT_DEF_BADGE_DIM_CLS =
  "border-border text-[var(--text-muted)]";
export const AGENT_DEF_BADGE_CHIP_CLS =
  "inline-flex items-center justify-center h-5 px-1.5 border-0 rounded-full text-2xs font-semibold leading-none tracking-normal whitespace-nowrap";
export const AGENT_DEF_BADGE_CHIP_LOCAL_CLS =
  "bg-[color-mix(in_srgb,var(--accent)_15%,transparent)] text-[var(--accent)]";

export const AGENT_DEF_PROPS_CLS =
  "grid grid-cols-2 gap-x-4 gap-y-1";
export const AGENT_DEF_PROP_ROW_CLS =
  "flex justify-between items-center text-[length:calc(var(--font-size-base)*0.75)] py-[3px]";
export const AGENT_DEF_PROP_LABEL_CLS = "text-[var(--text-muted)]";
export const AGENT_DEF_PROP_VALUE_CLS =
  "font-[inherit] font-medium text-[var(--text-primary)] text-[length:calc(var(--font-size-base)*0.75)]";

export const AGENT_DEF_SECTION_CLS = "flex flex-col gap-1.5";
export const AGENT_DEF_SECTION_TITLE_CLS =
  "text-xs font-semibold text-[var(--text-muted)] uppercase tracking-wider";
export const AGENT_DEF_DESCRIPTION_FULL_CLS =
  "text-sm text-[var(--text-secondary)] whitespace-pre-wrap m-0 leading-relaxed font-sans";
export const AGENT_DEF_JSON_CLS =
  "text-xs text-[var(--text-secondary)] bg-[var(--bg-primary)] border border-border rounded p-2 m-0 overflow-x-auto font-[inherit]";

export const AGENT_DEF_WORKFLOW_LIST_CLS = "flex flex-col gap-1.5";
export const AGENT_DEF_WORKFLOW_ITEM_CLS =
  "flex items-center flex-wrap gap-1.5 text-sm";
export const AGENT_DEF_WORKFLOW_NAME_CLS =
  "font-semibold font-[inherit] text-[var(--text-primary)]";
export const AGENT_DEF_WORKFLOW_DESC_CLS =
  "text-[var(--text-muted)] text-xs basis-full";

export const AGENT_DEF_SOURCE_INFO_CLS =
  "text-sm text-[var(--text-secondary)] [&_code]:font-[inherit] [&_code]:text-xs [&_code]:text-[var(--text-muted)] [&_code]:break-all";

export const AGENT_DEF_ACTIONS_CLS =
  "flex gap-2 items-center pt-1 border-t border-border";

export const AGENT_DEF_IMPORT_RESULT_CLS = "text-sm font-medium";
export const AGENT_DEF_IMPORT_RESULT_OK_CLS =
  "text-[var(--color-success-foreground)]";
export const AGENT_DEF_IMPORT_RESULT_ERR_CLS = "text-[var(--color-error)]";

// ── Steps editor (AgentStepsEditor.css) ──

export const STEP_EDITOR_CLS = "flex flex-col gap-1.5";
export const STEP_CARD_CLS =
  "border border-border rounded-md bg-[var(--bg-primary)] overflow-hidden";
export const STEP_CARD_EXPANDED_CLS = "border-[var(--accent)]";
export const STEP_CARD_HEADER_CLS =
  "flex items-center gap-2 px-3 py-2 cursor-pointer transition-colors hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11";
export const STEP_CARD_BODY_CLS =
  "px-3 pt-2 pb-3 border-t border-border flex flex-col gap-2.5";

export const STEP_NAME_BADGE_CLS =
  "font-[inherit] text-[length:calc(var(--font-size-base)*0.75)] font-semibold text-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_10%,transparent)] px-2 py-px rounded-[10px] whitespace-nowrap";
export const STEP_PREVIEW_CLS =
  "flex-1 text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)] overflow-hidden text-ellipsis whitespace-nowrap";
export const STEP_CHEVRON_CLS =
  "text-[var(--text-muted)] text-[length:calc(var(--font-size-base)*0.625)] shrink-0";

export const STEP_ACTIONS_CLS = "flex gap-1.5 items-center";

export const STEP_FIELD_CLS = "flex flex-col gap-[3px]";
export const STEP_FIELD_LABEL_CLS =
  "text-[length:calc(var(--font-size-base)*0.6875)] text-[var(--text-muted)] uppercase tracking-[0.3px] flex items-center gap-2";

export const STEP_SECTION_CLS =
  "flex flex-col gap-1.5 pt-1.5 border-t border-border";
export const STEP_SECTION_LABEL_CLS =
  "text-[length:calc(var(--font-size-base)*0.6875)] font-semibold text-[var(--text-muted)] uppercase tracking-wider m-0";

export const STEP_TOGGLE_SELECT_CLS =
  "text-[length:calc(var(--font-size-base)*0.6875)]! px-1 py-px! w-auto! max-w-[80px]";

export const STEP_CHIP_INPUT_CLS = "flex flex-col gap-1";
export const STEP_CHIPS_CLS = "flex flex-wrap gap-1";
export const STEP_CHIP_CLS =
  "inline-flex items-center gap-[3px] bg-[var(--bg-tertiary)] border border-border rounded-[10px] pl-2 pr-1.5 py-px text-[length:calc(var(--font-size-base)*0.6875)] font-[inherit] text-[var(--text-primary)]";
export const STEP_CHIP_REMOVE_CLS =
  "bg-none border-0 text-[var(--text-muted)] cursor-pointer text-[length:calc(var(--font-size-base)*0.8125)] leading-none px-px hover:text-[var(--color-error)]";
export const STEP_CHIP_ADD_ROW_CLS = "flex gap-1 items-center";
export const STEP_CHIP_FIELD_CLS =
  "flex-1 min-w-0 text-[length:calc(var(--font-size-base)*0.75)]! px-2 py-[3px]!";
export const STEP_CHIP_ADD_BTN_CLS =
  "text-[length:calc(var(--font-size-base)*0.75)]! px-2 py-[3px]!";

export const STEP_TRANSITION_ROW_CLS = "flex gap-1.5 items-center";
export const STEP_TRANSITION_TO_CLS =
  "w-[120px] shrink-0 text-[length:calc(var(--font-size-base)*0.75)]! px-1.5 py-1!";
export const STEP_TRANSITION_WHEN_CLS =
  "flex-1 min-w-0 text-[length:calc(var(--font-size-base)*0.75)]! px-1.5 py-1! font-[inherit]";

export const STEP_ADVANCED_TOGGLE_CLS =
  "bg-none border-0 text-[var(--text-muted)] text-[length:calc(var(--font-size-base)*0.75)] cursor-pointer flex items-center gap-1 p-0 uppercase tracking-wider font-medium hover:text-[var(--text-primary)]";
export const STEP_ADVANCED_FIELDS_CLS =
  "flex flex-col gap-2 mt-1.5";

export const STEP_JSON_EDITOR_CLS =
  "font-[inherit]! text-[length:calc(var(--font-size-base)*0.6875)]! min-h-[60px]";

export const STEP_READONLY_LIST_CLS = "flex flex-col gap-1";
export const STEP_READONLY_ITEM_CLS =
  "flex items-center gap-2 text-[length:calc(var(--font-size-base)*0.75)] py-1";
export const STEP_READONLY_SUMMARY_CLS =
  "text-[var(--text-muted)] text-[length:calc(var(--font-size-base)*0.6875)]";
