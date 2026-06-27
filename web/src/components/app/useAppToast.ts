import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";

type AppToastState = {
  toastMessage: string | null;
  setToastMessage: Dispatch<SetStateAction<string | null>>;
  showToast: (msg: string, durationMs?: number) => void;
};

export function useAppToast(): AppToastState {
  const [toastMessage, setToastMessage] = useState<string | null>(null);
  const toastTimerRef = useRef<number | null>(null);

  const showToast = useCallback((msg: string, durationMs = 3000) => {
    if (toastTimerRef.current !== null) clearTimeout(toastTimerRef.current);
    setToastMessage(msg);
    toastTimerRef.current = window.setTimeout(
      () => setToastMessage(null),
      durationMs,
    );
  }, []);

  useEffect(() => {
    return () => {
      if (toastTimerRef.current !== null) {
        clearTimeout(toastTimerRef.current);
      }
    };
  }, []);

  return { toastMessage, setToastMessage, showToast };
}
