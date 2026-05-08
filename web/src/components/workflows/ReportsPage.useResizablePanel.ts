import { useCallback, useEffect, useRef, useState } from "react";
import type React from "react";

export function useResizablePanel(
  initialWidth: number,
  minWidth: number,
  maxWidth: number,
) {
  const [width, setWidth] = useState(initialWidth);
  const isDragging = useRef(false);
  const startX = useRef(0);
  const startWidth = useRef(0);
  const cleanupRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    return () => {
      cleanupRef.current?.();
    };
  }, []);

  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      isDragging.current = true;
      startX.current = e.clientX;
      startWidth.current = width;

      const onMove = (ev: MouseEvent) => {
        if (!isDragging.current) return;
        const delta = startX.current - ev.clientX;
        setWidth(
          Math.max(minWidth, Math.min(maxWidth, startWidth.current + delta)),
        );
      };
      const onUp = () => {
        isDragging.current = false;
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        cleanupRef.current = null;
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
      cleanupRef.current = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
      };
    },
    [width, minWidth, maxWidth],
  );

  const handleTouchStart = useCallback(
    (e: React.TouchEvent) => {
      e.preventDefault();
      isDragging.current = true;
      startX.current = e.touches[0].clientX;
      startWidth.current = width;

      const onMove = (ev: TouchEvent) => {
        ev.preventDefault();
        if (!isDragging.current) return;
        const delta = startX.current - ev.touches[0].clientX;
        setWidth(
          Math.max(minWidth, Math.min(maxWidth, startWidth.current + delta)),
        );
      };
      const onEnd = () => {
        isDragging.current = false;
        document.removeEventListener("touchmove", onMove);
        document.removeEventListener("touchend", onEnd);
        cleanupRef.current = null;
      };
      document.addEventListener("touchmove", onMove, { passive: false });
      document.addEventListener("touchend", onEnd);
      cleanupRef.current = () => {
        document.removeEventListener("touchmove", onMove);
        document.removeEventListener("touchend", onEnd);
      };
    },
    [width, minWidth, maxWidth],
  );

  return { width, handleMouseDown, handleTouchStart };
}
