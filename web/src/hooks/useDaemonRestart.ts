import { useCallback, useState } from "react";
import {
  readDaemonRestartFailure,
  requestDaemonRestart,
  type ProtectedCronRun,
} from "../lib/api";

function getRestartErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  return "Failed to restart daemon";
}

export function useDaemonRestart() {
  const [showRestart, setShowRestart] = useState(false);
  const [restartError, setRestartError] = useState<string | null>(null);
  const [restartProtectedRuns, setRestartProtectedRuns] = useState<
    ProtectedCronRun[]
  >([]);

  const markRestartRequired = useCallback(() => {
    setRestartError(null);
    setRestartProtectedRuns([]);
    setShowRestart(true);
  }, []);

  const restartDaemon = useCallback(async (force = false) => {
    setRestartError(null);
    setRestartProtectedRuns([]);
    try {
      const res = await requestDaemonRestart(force);
      const failure = await readDaemonRestartFailure(res);
      if (failure) {
        setRestartError(failure.message);
        setRestartProtectedRuns(failure.protectedRuns);
        return false;
      }
      setShowRestart(false);
      return true;
    } catch (err) {
      console.error("Failed to restart daemon:", err);
      setRestartError(getRestartErrorMessage(err));
      return false;
    }
  }, []);

  return {
    showRestart,
    restartError,
    restartProtectedRuns,
    markRestartRequired,
    restartDaemon,
  };
}
