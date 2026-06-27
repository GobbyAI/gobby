import { normalizeChatMode } from "../../types/chat";
import type { AcpConfigOption, ApprovalOption } from "../../types/chat";
import {
  saveConversationId,
  saveDbSessionId,
  uuid,
} from "./conversationPersistence";
import {
  isChatProvider,
  normalizeSessionType,
  toSessionObservationMeta,
} from "./sessionRecords";
import { MESSAGE_FEEDBACK_DELAY_MS } from "./constants";
import type { UseChatTransportParams } from "./transportTypes";

function isApprovalOption(value: unknown): value is ApprovalOption {
  if (!value || typeof value !== "object") return false;
  const option = value as Record<string, unknown>;
  const decision = option.decision;
  const emphasis = option.emphasis;
  return (
    typeof option.id === "string" &&
    typeof option.label === "string" &&
    (option.description === undefined || typeof option.description === "string") &&
    (decision === undefined || decision === "approve") &&
    (emphasis === undefined || emphasis === "primary" || emphasis === "accent")
  );
}

function isAcpConfigOptionValue(
  value: unknown,
): value is AcpConfigOption["options"][number] {
  if (!value || typeof value !== "object") return false;
  const optionValue = value as Record<string, unknown>;
  return (
    typeof optionValue.value === "string" &&
    typeof optionValue.name === "string" &&
    (optionValue.description === undefined ||
      typeof optionValue.description === "string")
  );
}

function isAcpConfigOption(value: unknown): value is AcpConfigOption {
  if (!value || typeof value !== "object") return false;
  const option = value as Record<string, unknown>;
  return (
    typeof option.id === "string" &&
    typeof option.name === "string" &&
    typeof option.type === "string" &&
    typeof option.currentValue === "string" &&
    (option.description === undefined || typeof option.description === "string") &&
    (option.category === undefined || typeof option.category === "string") &&
    Array.isArray(option.options) &&
    option.options.every(isAcpConfigOptionValue)
  );
}

function readAcpConfigOptions(data: Record<string, unknown>): AcpConfigOption[] {
  const raw = data.config_options;
  if (!Array.isArray(raw)) return [];
  return raw.filter(isAcpConfigOption);
}

export function handlePlanPendingApproval(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  const msgConvId = data.conversation_id as string | undefined;
  if (msgConvId === ctx.conversationIdRef.current) {
    const planContent = data.plan_content as string | undefined;
    if (planContent) {
      const previousPlanContent = ctx.planContentRef.current;
      // Per-CLI plan-accept options (backend registry source of truth).
      // Absent/empty -> the UI degrades to a single generic Approve.
      const options = Array.isArray(data.options)
        ? data.options.filter(isApprovalOption)
        : [];
      ctx.setPlanApprovalOptions(options);
      ctx.setPlanPendingApproval(true);
      // A fresh plan re-arms the cycle: clear any prior approval so a
      // revised plan does not inherit a stale "approved" state (#15681).
      ctx.setPlanApproved(false);
      ctx.planContentRef.current = planContent;
      if (planContent !== previousPlanContent) {
        ctx.onPlanReadyRef.current?.(planContent);
      }
    }
  }
}

export function handleModeChanged(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  const msgConvId = data.conversation_id as string | undefined;
  if (msgConvId === ctx.conversationIdRef.current) {
    const rawMode = data.mode as string | undefined;
    const newMode = rawMode ? normalizeChatMode(rawMode) : undefined;
    const reason = data.reason as string | undefined;
    if (newMode) {
      ctx.lastServerModeTimestampRef.current = Date.now();
      // Clear plan state on approval — for rejection, the eager
      // clear in requestPlanChanges() already handled it, and
      // clearing here would race with a new plan_pending_approval
      // that may have arrived before this mode_changed.
      if (reason === "plan_approved") {
        ctx.setPlanPendingApproval(false);
        // Authoritative approval signal for the Plans panel (#15681).
        ctx.setPlanApproved(true);
        ctx.planContentRef.current = null;
      }
      if (
        reason === "plan_changes_requested" &&
        ctx.pendingPlanFeedbackRef.current
      ) {
        const feedback = ctx.pendingPlanFeedbackRef.current;
        ctx.pendingPlanFeedbackRef.current = null;
        setTimeout(() => {
          ctx.sendMessageRef.current?.(feedback);
        }, MESSAGE_FEEDBACK_DELAY_MS);
      }
      // Only update mode and notify if it actually changed —
      // prevents set_mode → mode_changed → setState → set_mode loop
      if (newMode !== ctx.currentModeRef.current) {
        ctx.currentModeRef.current = newMode;
        ctx.onModeChangedRef.current?.(newMode);
      }
    }
  }
}

export function handleSessionInfo(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  const ref = data.session_ref as string | undefined;
  if (ref) ctx.setSessionRef(ref);
  const dbSid = data.db_session_id as string | undefined;
  const infoConvId = data.conversation_id as string | undefined;
  if (dbSid && (!infoConvId || infoConvId === ctx.conversationIdRef.current)) {
    ctx.setDbSessionId(dbSid);
  }
  const branch = data.current_branch as string | undefined;
  if (branch !== undefined) ctx.setCurrentBranch(branch);
  const wtPath = data.worktree_path as string | undefined;
  if (wtPath !== undefined) ctx.setWorktreePath(wtPath);
  const agentName = data.agent_name as string | undefined;
  if (agentName) ctx.setActiveAgent(agentName);
  // Reconcile the mode radio to the backend session's authoritative chat_mode.
  // A session can restore a persisted mode (e.g. bypass) that differs from the
  // UI's optimistic default (Plan); without this the UI showed read-only Plan
  // while the agent ran permissively and the plan-approval surface was
  // suppressed (#15709). Update local state only (no set_mode echo) to avoid a
  // set_mode -> mode_changed loop, mirroring handleSessionContinued.
  const infoMode = data.chat_mode as string | undefined;
  if (infoMode && (!infoConvId || infoConvId === ctx.conversationIdRef.current)) {
    const restored = normalizeChatMode(infoMode);
    if (restored !== ctx.currentModeRef.current) {
      ctx.currentModeRef.current = restored;
      ctx.onModeChangedRef.current?.(restored);
    }
  }
  if (!infoConvId || infoConvId === ctx.conversationIdRef.current) {
    ctx.setAcpConfigOptions(readAcpConfigOptions(data));
  }
}

export function handleSessionConfigOptions(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  const msgConvId = data.conversation_id as string | undefined;
  if (msgConvId && msgConvId !== ctx.conversationIdRef.current) return;
  ctx.setAcpConfigOptions(readAcpConfigOptions(data));
}

export function handleWorktreeSwitched(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  ctx.setCurrentBranch((data.new_branch as string) ?? null);
  ctx.setWorktreePath((data.worktree_path as string) ?? null);
}

export function handleAgentChanged(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  const agentName = data.agent_name as string | undefined;
  if (agentName) ctx.setActiveAgent(agentName);
}

export function handleSessionContinued(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  ctx.clearContinuingSession();
  const nextConversationId = (data.conversation_id as string | undefined) ?? null;
  const nextDbSessionId = (data.db_session_id as string) ?? null;
  if (
    nextConversationId &&
    nextConversationId !== ctx.conversationIdRef.current
  ) {
    ctx.conversationIdRef.current = nextConversationId;
    ctx.setConversationId(nextConversationId);
    saveConversationId(nextConversationId);
  }
  ctx.setDbSessionId(nextDbSessionId);
  ctx.dbSessionIdRef.current = nextDbSessionId;
  saveDbSessionId(nextDbSessionId);
  const continuedMeta = toSessionObservationMeta(data, {
    ref: (data.ref as string | undefined) ?? ctx.sessionRefRef.current,
    status: (data.status as string | undefined) ?? "active",
    sessionType: normalizeSessionType(data.session_type) ?? "web_chat",
  });
  if (continuedMeta) {
    ctx.setMainSessionMeta(continuedMeta);
    ctx.setSessionTitle(continuedMeta.title ?? null);
    if (continuedMeta.ref) {
      ctx.setSessionRef(continuedMeta.ref);
    }
    ctx.setCurrentBranch(continuedMeta.gitBranch ?? null);
    if (continuedMeta.source && isChatProvider(continuedMeta.source)) {
      ctx.setSelectedProvider(continuedMeta.source);
    }
    if (continuedMeta.chatMode) {
      const restored = normalizeChatMode(continuedMeta.chatMode);
      ctx.currentModeRef.current = restored;
      ctx.onModeChangedRef.current?.(restored);
    }
  }
  if (nextDbSessionId) {
    const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
    fetch(`${baseUrl}/api/sessions/${nextDbSessionId}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((payload) => {
        const session = payload?.session;
        if (!session || ctx.dbSessionIdRef.current !== nextDbSessionId) return;
        ctx.applyMainSessionMeta(session);
      })
      .catch((error) => {
        console.warn("Failed to fetch continued session metadata", {
          sessionId: nextDbSessionId,
          error,
        });
      });
  }
  ctx.clearContinuationRollback();
  const resumeNotice =
    typeof data.resume_notice === "string" ? data.resume_notice : null;
  if (resumeNotice) {
    ctx.setMessages((prev) => [
      ...prev,
      {
        id: `system-resume-notice-${uuid()}`,
        role: "system" as const,
        content: resumeNotice,
        timestamp: new Date(),
      },
    ]);
  }
  if (import.meta.env.DEV) {
    console.debug("Session continued:", data);
  }
}

export function handleTransportError(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  if (ctx.continuingSessionIdRef.current) {
    const activeContinuationId = ctx.continuingSessionIdRef.current;
    ctx.clearContinuingSession();
    const rollback = ctx.continuationRollbackRef.current;
    if (rollback && rollback.sourceSessionId === activeContinuationId) {
      ctx.clearContinuationRollback();
      ctx.restoreContinuationState(rollback);
    }
  }
  const errorMessage =
    typeof data.message === "string" ? data.message : "Unknown error";
  ctx.setMessages((prev) => [
    ...prev,
    {
      id: `system-error-${uuid()}`,
      role: "system" as const,
      content: errorMessage,
      timestamp: new Date(),
    },
  ]);
}

export function handleConnectionEstablished(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  const serverConversations = (data.conversation_ids as string[]) || [];
  if (serverConversations.includes(ctx.conversationIdRef.current)) {
    if (import.meta.env.DEV) {
      console.debug(
        "Reconnected to existing conversation:",
        ctx.conversationIdRef.current,
      );
    }
  }
  if (import.meta.env.DEV) {
    console.debug("Connection established:", data);
  }
}

export function handleSubscribeSuccess(data: Record<string, unknown>) {
  if (import.meta.env.DEV) {
    console.debug("Subscribed to events:", data);
  }
}

export function handleChatDeleted(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  const cid = data.conversation_id as string;
  if (import.meta.env.DEV) {
    console.debug("Chat deleted confirmed:", cid);
  }
  ctx.onChatDeletedRef.current?.(cid);
}

export function handleChatCleared(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  const cid = data.conversation_id as string;
  if (import.meta.env.DEV) {
    console.debug("Chat cleared confirmed:", cid);
  }
  ctx.onChatClearedRef.current?.(cid);
}
