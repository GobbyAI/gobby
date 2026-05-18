/* eslint-disable react-hooks/exhaustive-deps -- Extracted useChat transport intentionally closes over parent refs and stable setters to preserve the original hook behavior. */
import { useCallback, useEffect, useRef } from "react";
import { connectChatTransport } from "./transportLifecycle";
import type {
  TransportConnectRef,
  UseChatTransportParams,
} from "./transportTypes";

export function useChatTransport(params: UseChatTransportParams) {
  const {
    applyMainSessionMeta,
    clearContinuationRollback,
    clearContinuingSession,
    markSessionUsageFresh,
    resolveAgentName,
    restoreContinuationState,
    setSelectedProvider,
  } = params;

  const connectRef = useRef<(() => void) | null>(null);

  // Connect to WebSocket
  const connect = useCallback(() => {
    connectChatTransport(params, connectRef as TransportConnectRef);
  }, [
    applyMainSessionMeta,
    clearContinuingSession,
    clearContinuationRollback,
    markSessionUsageFresh,
    resolveAgentName,
    restoreContinuationState,
    setSelectedProvider,
  ]);

  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  return connect;
}
