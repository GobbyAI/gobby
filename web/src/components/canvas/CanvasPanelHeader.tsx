import React from 'react';
import { Button } from '../chat/ui/Button';
import { Badge } from '../chat/ui/Badge';

interface Props {
  title?: string;
  onClose: () => void;
  isMobile?: boolean;
}

export const CanvasPanelHeader: React.FC<Props> = ({ title, onClose, isMobile = false }) => {
  return (
    <div className="flex items-center justify-between border-b border-border px-4 py-2 shrink-0 bg-muted/50">
      <div className="flex items-center gap-2 overflow-hidden">
        <Badge variant="info" className="shrink-0 uppercase text-[10px] tracking-wider font-semibold">Canvas</Badge>
        <span className="text-sm font-medium truncate" title={title || 'Interactive Canvas'}>
          {title || 'Interactive Canvas'}
        </span>
      </div>
      <Button
        variant={isMobile ? "outline" : "ghost"}
        size="icon"
        className={isMobile ? "h-10 w-10 shrink-0" : "h-6 w-6 shrink-0"}
        onClick={onClose}
        aria-label="Close Canvas"
      >
        <svg aria-hidden="true" width={isMobile ? 20 : 16} height={isMobile ? 20 : 16} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M18 6L6 18M6 6l12 12" />
        </svg>
      </Button>
    </div>
  );
};
