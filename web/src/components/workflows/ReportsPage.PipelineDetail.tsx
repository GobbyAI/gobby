import { useState } from "react";
import type {
  PipelineExecutionRecord,
  PipelineStepExecution,
} from "../../hooks/usePipelineExecutions";
import {
  AlertIcon,
  ChevronIcon,
  PipelineStatusDot as StatusDot,
  StepDisplay,
} from "./execution-utils";
import { formatJson } from "./executionFormatters";
import { cn } from "../../lib/utils";
import { normalizeStatus } from "./ReportsPage.helpers";
import { CloseIcon, CronIcon, TraceIcon } from "./ReportsPage.icons";
import {
  APPROVAL_ACTIONS_CLS,
  APPROVAL_CLS,
  APPROVAL_MESSAGE_CLS,
  BTN_APPROVE_CLS,
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
  DETAIL_STATUS_CLS,
  DETAIL_STEPS_CLS,
  DETAIL_TITLE_CLS,
  DETAIL_TOGGLE_CLS,
  DETAIL_TRIGGER_CLS,
  DETAIL_VALUE_CLS,
  STATUS_TEXT_CLS,
} from "./ReportsPage.styles";

interface PipelineDetailProps {
  execution: PipelineExecutionRecord;
  actionLoading: string | null;
  onApprove: (token: string) => Promise<void>;
  onReject: (token: string) => Promise<void>;
  onNavigateToTrace?: (traceId: string) => void;
  onClose: () => void;
}

interface ApprovalPanelProps {
  execution: PipelineExecutionRecord;
  actionLoading: string | null;
  onApprove: (token: string) => Promise<void>;
  onReject: (token: string) => Promise<void>;
}

function ApprovalPanel({
  execution,
  actionLoading,
  onApprove,
  onReject,
}: ApprovalPanelProps) {
  const waitingStep: PipelineStepExecution | undefined = execution.steps.find(
    (s) => s.status === "waiting_approval" && s.approval_token,
  );
  if (!waitingStep?.approval_token) return null;

  return (
    <div className={APPROVAL_CLS}>
      <div className={APPROVAL_MESSAGE_CLS}>
        <AlertIcon />
        <span>Step &ldquo;{waitingStep.step_id}&rdquo; requires approval</span>
      </div>
      <div className={APPROVAL_ACTIONS_CLS}>
        <button
          type="button"
          className={cn(BTN_BASE_CLS, BTN_APPROVE_CLS)}
          onClick={() => onApprove(waitingStep.approval_token!)}
          disabled={actionLoading === waitingStep.approval_token}
        >
          {actionLoading === waitingStep.approval_token ? "Approving..." : "Approve"}
        </button>
        <button
          type="button"
          className={cn(BTN_BASE_CLS, BTN_REJECT_CLS)}
          onClick={() => onReject(waitingStep.approval_token!)}
          disabled={actionLoading === waitingStep.approval_token}
        >
          {actionLoading === waitingStep.approval_token ? "Rejecting..." : "Reject"}
        </button>
      </div>
    </div>
  );
}

export function PipelineDetail({
  execution,
  actionLoading,
  onApprove,
  onReject,
  onNavigateToTrace,
  onClose,
}: PipelineDetailProps) {
  const [showConfig, setShowConfig] = useState(false);
  const [showInputs, setShowInputs] = useState(false);
  const [showOutputs, setShowOutputs] = useState(false);
  return (
    <>
      <div className={DETAIL_HEADER_CLS}>
        <div className={DETAIL_HEADER_TOP_CLS}>
          <span className={DETAIL_ID_CLS}>{execution.id}</span>
          <button className={DETAIL_CLOSE_CLS} onClick={onClose}>
            <CloseIcon />
          </button>
        </div>
        <div className={DETAIL_TITLE_CLS}>{execution.pipeline_name}</div>
        <div className={DETAIL_STATUS_CLS}>
          <StatusDot status={execution.status} />
          <span className={STATUS_TEXT_CLS}>
            {normalizeStatus(execution.status)}
          </span>
          {execution.cron_job_name && (
            <span className={DETAIL_TRIGGER_CLS}>
              <CronIcon /> {execution.cron_job_name}
            </span>
          )}
        </div>
      </div>

      <div className={DETAIL_BODY_CLS}>
        {execution.trace_id && onNavigateToTrace && (
          <div className={DETAIL_SECTION_CLS}>
            <button
              type="button"
              className={BTN_BASE_CLS}
              onClick={() => onNavigateToTrace(execution.trace_id!)}
              title="View telemetry trace for this execution"
            >
              <TraceIcon />
              View Trace
            </button>
          </div>
        )}

        {execution.status === "waiting_approval" && (
          <ApprovalPanel
            execution={execution}
            actionLoading={actionLoading}
            onApprove={onApprove}
            onReject={onReject}
          />
        )}

        {execution.steps.length > 0 && (
          <div className={DETAIL_SECTION_CLS}>
            <span className={DETAIL_LABEL_CLS}>Execution Report</span>
            <div className={DETAIL_STEPS_CLS}>
              {execution.steps.map((step, index) => (
                <StepDisplay key={step.id} step={step} index={index} />
              ))}
            </div>
          </div>
        )}

        {execution.outputs_json &&
          (() => {
            try {
              const outputs = JSON.parse(execution.outputs_json);
              if (outputs.error) {
                return (
                  <div className={DETAIL_ERROR_CLS}>
                    Error: {outputs.error}
                  </div>
                );
              }
            } catch (err) {
              if (import.meta.env.DEV) console.debug("Failed to parse pipeline outputs JSON", err);
            }
            return null;
          })()}

        {execution.inputs_json && (
          <div className={DETAIL_SECTION_CLS}>
            <button
              type="button"
              className={DETAIL_TOGGLE_CLS}
              onClick={() => setShowInputs(!showInputs)}
            >
              <ChevronIcon expanded={showInputs} /> Inputs
            </button>
            {showInputs && (
              <div className={DETAIL_CODE_CLS}>
                {formatJson(execution.inputs_json)}
              </div>
            )}
          </div>
        )}

        {execution.status === "completed" && execution.outputs_json && (
          <div className={DETAIL_SECTION_CLS}>
            <button
              type="button"
              className={DETAIL_TOGGLE_CLS}
              onClick={() => setShowOutputs(!showOutputs)}
            >
              <ChevronIcon expanded={showOutputs} /> Outputs
            </button>
            {showOutputs && (
              <div className={DETAIL_CODE_CLS}>
                {formatJson(execution.outputs_json)}
              </div>
            )}
          </div>
        )}

        {execution.definition_json && (
          <div className={DETAIL_SECTION_CLS}>
            <button
              type="button"
              className={DETAIL_TOGGLE_CLS}
              onClick={() => setShowConfig(!showConfig)}
            >
              <ChevronIcon expanded={showConfig} /> Pipeline Config
            </button>
            {showConfig && (
              <div className={DETAIL_CODE_CLS}>
                {formatJson(execution.definition_json)}
              </div>
            )}
          </div>
        )}

        {execution.parent_execution_id && (
          <div className={DETAIL_SECTION_CLS}>
            <span className={DETAIL_LABEL_CLS}>Parent</span>
            <span className={cn(DETAIL_VALUE_CLS, DETAIL_MONO_CLS)}>
              {execution.parent_execution_id}
            </span>
          </div>
        )}
      </div>
    </>
  );
}
