import { useState } from "react";
import type { AgentRunRecord } from "../../hooks/useAgentRuns";
import {
  ChevronIcon,
  PipelineStatusDot as StatusDot,
} from "./execution-utils";
import { cn } from "../../lib/utils";
import { normalizeStatus } from "./ReportsPage.helpers";
import { CloseIcon } from "./ReportsPage.icons";
import {
  BTN_BASE_CLS,
  BTN_REJECT_CLS,
  DETAIL_BODY_CLS,
  DETAIL_CLOSE_CLS,
  DETAIL_CODE_CLS,
  DETAIL_ERROR_CLS,
  DETAIL_HEADER_CLS,
  DETAIL_HEADER_TOP_CLS,
  DETAIL_ID_CLS,
  DETAIL_LABEL_CLS,
  DETAIL_MONO_CLS,
  DETAIL_SECTION_CLS,
  DETAIL_STAT_CLS,
  DETAIL_STAT_LABEL_CLS,
  DETAIL_STAT_VALUE_CLS,
  DETAIL_STATS_CLS,
  DETAIL_STATUS_CLS,
  DETAIL_TAG_CLS,
  DETAIL_TAGS_CLS,
  DETAIL_TITLE_CLS,
  DETAIL_TOGGLE_CLS,
  DETAIL_VALUE_CLS,
  STATUS_TEXT_CLS,
} from "./ReportsPage.styles";

interface AgentDetailProps {
  run: AgentRunRecord;
  actionLoading: string | null;
  onCancel: (runId: string) => Promise<void>;
  onClose: () => void;
}

export function AgentDetail({
  run,
  actionLoading,
  onCancel,
  onClose,
}: AgentDetailProps) {
  const [showPrompt, setShowPrompt] = useState(false);
  const [showResult, setShowResult] = useState(false);

  const totalTokens =
    (run.usage_input_tokens || 0) + (run.usage_output_tokens || 0);

  return (
    <>
      <div className={DETAIL_HEADER_CLS}>
        <div className={DETAIL_HEADER_TOP_CLS}>
          <span className={DETAIL_ID_CLS}>{run.id}</span>
          <button className={DETAIL_CLOSE_CLS} onClick={onClose}>
            <CloseIcon />
          </button>
        </div>
        <div className={DETAIL_TITLE_CLS}>
          {run.workflow_name || run.prompt?.slice(0, 80) || "Agent Run"}
        </div>
        <div className={DETAIL_STATUS_CLS}>
          <StatusDot status={run.status} />
          <span className={STATUS_TEXT_CLS}>
            {normalizeStatus(run.status)}
          </span>
        </div>
        <div className={DETAIL_TAGS_CLS}>
          <span className={DETAIL_TAG_CLS}>{run.provider}</span>
          {run.model && <span className={DETAIL_TAG_CLS}>{run.model}</span>}
          <span className={DETAIL_TAG_CLS}>{run.mode}</span>
        </div>
      </div>

      <div className={DETAIL_BODY_CLS}>
        {run.status === "running" && (
          <div className={DETAIL_SECTION_CLS}>
            <button
              type="button"
              className={cn(BTN_BASE_CLS, BTN_REJECT_CLS)}
              onClick={() => onCancel(run.id)}
              disabled={actionLoading === run.id}
            >
              {actionLoading === run.id ? "Cancelling..." : "Cancel Agent"}
            </button>
          </div>
        )}

        {run.summary_markdown && (
          <div className={DETAIL_SECTION_CLS}>
            <span className={DETAIL_LABEL_CLS}>Summary</span>
            <div className={DETAIL_CODE_CLS}>{run.summary_markdown}</div>
          </div>
        )}

        {(run.status === "error" || run.status === "timeout") && run.error && (
          <div className={DETAIL_ERROR_CLS}>Error: {run.error}</div>
        )}

        {run.status === "success" && run.result && (
          <div className={DETAIL_SECTION_CLS}>
            <button
              type="button"
              className={DETAIL_TOGGLE_CLS}
              onClick={() => setShowResult(!showResult)}
            >
              <ChevronIcon expanded={showResult} /> Result
            </button>
            {showResult && (
              <div className={DETAIL_CODE_CLS}>{run.result}</div>
            )}
          </div>
        )}

        {totalTokens > 0 && (
          <div className={DETAIL_SECTION_CLS}>
            <span className={DETAIL_LABEL_CLS}>Usage</span>
            <div className={DETAIL_STATS_CLS}>
              <div className={DETAIL_STAT_CLS}>
                <span className={DETAIL_STAT_LABEL_CLS}>Input</span>
                <span className={DETAIL_STAT_VALUE_CLS}>
                  {(run.usage_input_tokens || 0).toLocaleString()}
                </span>
              </div>
              <div className={DETAIL_STAT_CLS}>
                <span className={DETAIL_STAT_LABEL_CLS}>Output</span>
                <span className={DETAIL_STAT_VALUE_CLS}>
                  {(run.usage_output_tokens || 0).toLocaleString()}
                </span>
              </div>
              {(run.usage_cache_read_tokens || 0) > 0 && (
                <div className={DETAIL_STAT_CLS}>
                  <span className={DETAIL_STAT_LABEL_CLS}>Cache</span>
                  <span className={DETAIL_STAT_VALUE_CLS}>
                    {(run.usage_cache_read_tokens || 0).toLocaleString()}
                  </span>
                </div>
              )}
              <div className={DETAIL_STAT_CLS}>
                <span className={DETAIL_STAT_LABEL_CLS}>Tools</span>
                <span className={DETAIL_STAT_VALUE_CLS}>
                  {run.tool_calls_count ?? 0}
                </span>
              </div>
            </div>
          </div>
        )}

        {run.prompt && (
          <div className={DETAIL_SECTION_CLS}>
            <button
              type="button"
              className={DETAIL_TOGGLE_CLS}
              onClick={() => setShowPrompt(!showPrompt)}
            >
              <ChevronIcon expanded={showPrompt} /> Prompt
            </button>
            {showPrompt && (
              <div className={DETAIL_CODE_CLS}>{run.prompt}</div>
            )}
          </div>
        )}

        {(run.task_id || run.worktree_id || run.clone_id || run.git_branch) && (
          <div className={DETAIL_SECTION_CLS}>
            <span className={DETAIL_LABEL_CLS}>Context</span>
            {run.task_id && (
              <span className={cn(DETAIL_VALUE_CLS, DETAIL_MONO_CLS)}>
                Task: {run.task_id}
              </span>
            )}
            {run.git_branch && (
              <span className={cn(DETAIL_VALUE_CLS, DETAIL_MONO_CLS)}>
                Branch: {run.git_branch}
              </span>
            )}
            {(run.worktree_id || run.clone_id) && (
              <span className={DETAIL_VALUE_CLS}>
                {run.worktree_id
                  ? `Worktree: ${run.worktree_id}`
                  : `Clone: ${run.clone_id}`}
              </span>
            )}
          </div>
        )}
      </div>
    </>
  );
}
