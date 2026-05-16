import js from "@eslint/js";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import { defineConfig, globalIgnores } from "eslint/config";
import globals from "globals";
import tseslint from "typescript-eslint";

const SET_STATE_IN_EFFECT_EXEMPTIONS = [
  "src/components/ConfigurationPage.tsx",
  "src/components/activity/FilesTab.tsx",
  "src/components/activity/PipelinesTab.tsx",
  "src/components/activity/SessionInteractionModal.tsx",
  "src/components/activity/TasksTab.tsx",
  "src/components/agents/AgentPortfolioPage.tsx",
  "src/components/canvas/hooks/useA2UIDataModel.ts",
  "src/components/canvas/hooks/useCanvasPanel.ts",
  "src/components/chat/ChatInput.tsx",
  "src/components/chat/CommandPalette.tsx",
  "src/components/chat/ResumeSessionModal.tsx",
  "src/components/code-graph/CodeGraphExplorer.tsx",
  "src/components/command-browser/ToolBrowserModal.tsx",
  "src/components/integrations/ChannelDetail.tsx",
  "src/components/mcp/McpToolDetail.tsx",
  "src/components/memory/KnowledgeGraph.tsx",
  "src/components/memory/MemoryGraph.tsx",
  "src/components/memory/MemoryPage.tsx",
  "src/components/rules/ExpressionBuilder.tsx",
  "src/components/rules/RuleEditForm.tsx",
  "src/components/sessions/SessionDetail.tsx",
  "src/components/source-control/BranchDetail.tsx",
  "src/components/source-control/PullRequestDetail.tsx",
  "src/components/tasks/ActionFeed.tsx",
  "src/components/tasks/ActivityPulse.tsx",
  "src/components/tasks/AssigneePicker.tsx",
  "src/components/tasks/LaunchAgentDialog.tsx",
  "src/components/tasks/PermissionOverrides.tsx",
  "src/components/tasks/QuickCaptureTask.tsx",
  "src/components/tasks/RawTraceView.tsx",
  "src/components/tasks/SessionViewer.tsx",
  "src/components/tasks/TaskComments.tsx",
  "src/components/tasks/TaskCreateForm.tsx",
  "src/components/tasks/TaskDetail.tsx",
  "src/components/tasks/TaskMemories.tsx",
  "src/components/tasks/TaskStatusStrip.tsx",
  "src/components/terminals/TerminalsPage.tsx",
  "src/components/workflows/AgentsTab.tsx",
  "src/components/workflows/ReportsPage.tsx",
  "src/components/workflows/RulesTab.tsx",
  "src/hooks/useAgentDefinitions.ts",
  "src/hooks/useAgentRuns.ts",
  "src/hooks/useAuth.ts",
  "src/hooks/useCronJobs.ts",
  "src/hooks/useDashboard.ts",
  "src/hooks/useIsMobile.ts",
  "src/hooks/useMcp.ts",
  "src/hooks/useMemory.ts",
  "src/hooks/useMetrics.ts",
  "src/hooks/usePipelineExecutions.ts",
  "src/hooks/useProjects.ts",
  "src/hooks/useRules.ts",
  "src/hooks/useSavings.ts",
  "src/hooks/useSessionDetail.ts",
  "src/hooks/useSessions.ts",
  "src/hooks/useSkills.ts",
  "src/hooks/useSourceControl.ts",
  "src/hooks/useTasks.ts",
  "src/hooks/useTimeStats.ts",
  "src/hooks/useTokenTimeSeries.ts",
  "src/hooks/useTraces.ts",
  "src/hooks/useUsage.ts",
  "src/hooks/useVoice.ts",
  "src/hooks/useWorkflows.ts",
  "src/setup/steps/Bootstrap.tsx",
  "src/setup/steps/CliHooks.tsx",
  "src/setup/steps/PersonalWorkspace.tsx",
  "src/setup/steps/ProjectDiscovery.tsx",
  "src/setup/steps/SystemCheck.tsx",
];

const IMMUTABILITY_EXEMPTIONS = [
  "src/App.tsx",
  "src/hooks/useChat.ts",
  "src/hooks/useTasks.ts",
];

export default defineConfig([
  globalIgnores(["dist/", "dist-setup/", "coverage/", ".vite/"]),
  {
    files: ["src/**/*.{ts,tsx}"],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    plugins: {
      "jsx-a11y": jsxA11y,
    },
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      // Canary-only rules
      "react-hooks/set-state-in-effect": "error",
      "react-hooks/refs": "error",
      "react-hooks/immutability": "error",
      "react-hooks/purity": "error",
      "react-hooks/preserve-manual-memoization": "error",

      "jsx-a11y/control-has-associated-label": [
        "error",
        {
          labelAttributes: [],
          controlComponents: [],
          ignoreElements: ["input", "select", "textarea"],
          ignoreRoles: [
            "grid",
            "listbox",
            "menu",
            "menubar",
            "radiogroup",
            "row",
            "tablist",
            "toolbar",
            "tree",
            "treegrid",
          ],
          depth: 5,
        },
      ],

      "jsx-a11y/label-has-associated-control": [
        "error",
        {
          assert: "either",
          depth: 5,
        },
      ],

      // TypeScript already handles unused vars via noUnusedLocals
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-empty-object-type": "off",

      // ES2020 target doesn't support ErrorOptions ({ cause })
      "preserve-caught-error": "off",

      "react-refresh/only-export-components": [
        "warn",
        { allowConstantExport: true },
      ],
    },
  },
  {
    files: SET_STATE_IN_EFFECT_EXEMPTIONS,
    rules: {
      // Canary rule still over-flags legacy async fetch/reset patterns.
      "react-hooks/set-state-in-effect": "off",
    },
  },
  {
    files: IMMUTABILITY_EXEMPTIONS,
    rules: {
      // These files intentionally use refs for lifecycle coordination.
      "react-hooks/immutability": "off",
    },
  },
]);
