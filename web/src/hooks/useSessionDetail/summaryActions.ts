import {
  useCallback,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import type { GobbySession } from "../../types/sessions";
import { generateSessionSummary } from "./api";

export function useSessionSummaryActions(
  sessionId: string | null,
  setSession: Dispatch<SetStateAction<GobbySession | null>>,
) {
  const [isGeneratingSummary, setIsGeneratingSummary] = useState(false);

  const generateSummary = useCallback(async () => {
    if (!sessionId || isGeneratingSummary) return;

    setIsGeneratingSummary(true);
    try {
      const summaryMarkdown = await generateSessionSummary(sessionId);
      if (summaryMarkdown) {
        setSession((prev) =>
          prev ? { ...prev, summary_markdown: summaryMarkdown } : prev,
        );
      }
    } catch (e) {
      console.error("Failed to generate summary:", e);
    } finally {
      setIsGeneratingSummary(false);
    }
  }, [isGeneratingSummary, sessionId, setSession]);

  return { generateSummary, isGeneratingSummary };
}
