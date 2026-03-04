import React from 'react';
import { CanvasPanelState } from './hooks/useCanvasPanel';
import { CanvasPanelHeader } from './CanvasPanelHeader';
import { ResizeHandle } from '../chat/artifacts/ResizeHandle';

interface Props {
  state: CanvasPanelState;
  panelWidth: number;
  onResize: (width: number) => void;
  onClose: () => void;
  isMobile?: boolean;
}

export const CanvasPanel: React.FC<Props> = ({ state, panelWidth, onResize, onClose, isMobile = false }) => {
  if (isMobile) {
    return (
      <div className="flex flex-col w-full h-full bg-background z-20">
        <CanvasPanelHeader title={state.title} onClose={onClose} isMobile />
        <div className="flex-1 overflow-hidden relative">
          <iframe
            src={state.url}
            sandbox="allow-scripts"
            className="absolute inset-0 w-full h-full border-0 bg-white"
            title={state.title || "Canvas"}
          />
        </div>
      </div>
    );
  }

  return (
    <>
      <ResizeHandle
        onResize={onResize}
        panelWidth={panelWidth}
        minWidth={400}
        maxWidth={1200}
      />
      <div
        className="flex flex-col border-l border-border bg-background shrink-0 z-10"
        style={{ width: `${panelWidth}px` }}
      >
        <CanvasPanelHeader title={state.title} onClose={onClose} />
        <div className="flex-1 overflow-hidden relative">
          <iframe
            src={state.url}
            sandbox="allow-scripts"
            className="absolute inset-0 w-full h-full border-0 bg-white"
            title={state.title || "Canvas"}
          />
        </div>
      </div>
    </>
  );
};
