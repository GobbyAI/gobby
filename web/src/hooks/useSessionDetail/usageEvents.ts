import { useCallback, type Dispatch, type SetStateAction } from "react";
import type { GobbySession } from "../../types/sessions";
import { useRafCoalescedHandler } from "../useRafCoalescedHandler";
import { useWebSocketEvent } from "../useWebSocketEvent";

interface Ref<T> {
  current: T;
}

interface UseSessionUsageEventsParams {
  sessionIdRef: Ref<string | null>;
  setSession: Dispatch<SetStateAction<GobbySession | null>>;
}

export function useSessionUsageEvents({
  sessionIdRef,
  setSession,
}: UseSessionUsageEventsParams): void {
  const applyUsageUpdate = useCallback(
    (data: Record<string, unknown>) => {
      const updatedSessionId =
        typeof data.session_id === "string" ? data.session_id : null;
      if (!updatedSessionId || updatedSessionId !== sessionIdRef.current)
        return;

      setSession((prev) =>
        prev
          ? {
              ...prev,
              usage_input_tokens:
                typeof data.usage_input_tokens === "number"
                  ? data.usage_input_tokens
                  : prev.usage_input_tokens,
              usage_output_tokens:
                typeof data.usage_output_tokens === "number"
                  ? data.usage_output_tokens
                  : prev.usage_output_tokens,
              usage_cache_creation_tokens:
                typeof data.usage_cache_creation_tokens === "number"
                  ? data.usage_cache_creation_tokens
                  : prev.usage_cache_creation_tokens,
              usage_cache_read_tokens:
                typeof data.usage_cache_read_tokens === "number"
                  ? data.usage_cache_read_tokens
                  : prev.usage_cache_read_tokens,
              context_window:
                typeof data.context_window === "number"
                  ? data.context_window
                  : prev.context_window,
              context_used_tokens:
                typeof data.context_used_tokens === "number"
                  ? data.context_used_tokens
                  : data.context_used_tokens === null
                    ? null
                    : prev.context_used_tokens,
              context_usage_ratio:
                typeof data.context_usage_ratio === "number"
                  ? data.context_usage_ratio
                  : data.context_usage_ratio === null
                    ? null
                    : prev.context_usage_ratio,
              context_usage_source:
                typeof data.context_usage_source === "string"
                  ? data.context_usage_source
                  : data.context_usage_source === null
                    ? null
                    : prev.context_usage_source,
              context_usage_confidence:
                typeof data.context_usage_confidence === "string"
                  ? data.context_usage_confidence
                  : data.context_usage_confidence === null
                    ? null
                    : prev.context_usage_confidence,
              last_prompt_input_tokens:
                typeof data.last_prompt_input_tokens === "number"
                  ? data.last_prompt_input_tokens
                  : data.last_prompt_input_tokens === null
                    ? null
                    : prev.last_prompt_input_tokens,
              last_prompt_uncached_input_tokens:
                typeof data.last_prompt_uncached_input_tokens === "number"
                  ? data.last_prompt_uncached_input_tokens
                  : data.last_prompt_uncached_input_tokens === null
                    ? null
                    : prev.last_prompt_uncached_input_tokens,
              last_prompt_cache_read_tokens:
                typeof data.last_prompt_cache_read_tokens === "number"
                  ? data.last_prompt_cache_read_tokens
                  : data.last_prompt_cache_read_tokens === null
                    ? null
                    : prev.last_prompt_cache_read_tokens,
              last_prompt_cache_creation_tokens:
                typeof data.last_prompt_cache_creation_tokens === "number"
                  ? data.last_prompt_cache_creation_tokens
                  : data.last_prompt_cache_creation_tokens === null
                    ? null
                    : prev.last_prompt_cache_creation_tokens,
              last_completion_output_tokens:
                typeof data.last_completion_output_tokens === "number"
                  ? data.last_completion_output_tokens
                  : data.last_completion_output_tokens === null
                    ? null
                    : prev.last_completion_output_tokens,
              model: typeof data.model === "string" ? data.model : prev.model,
            }
          : prev,
      );
    },
    [sessionIdRef, setSession],
  );

  const enqueueUsageUpdate = useRafCoalescedHandler(applyUsageUpdate);
  useWebSocketEvent(
    "session_usage_updated",
    useCallback(
      (data: Record<string, unknown>) => {
        const updatedSessionId =
          typeof data.session_id === "string" ? data.session_id : null;
        if (!updatedSessionId || updatedSessionId !== sessionIdRef.current)
          return;
        enqueueUsageUpdate(data);
      },
      [enqueueUsageUpdate, sessionIdRef],
    ),
  );
}
