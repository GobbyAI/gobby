import { Button } from "../../../../web/src/components/ui/Button";
import {
  MousePointer2,
  Scan,
  List,
  Menu,
  Minimize2,
  MessageSquare,
  Move,
} from "lucide-react";
import type { PointerEvent } from "react";
import type { Mode } from "./selection";
export function Toolbar({
  count,
  collapsed,
  mode,
  onMode,
  onList,
  onMenu,
  onCollapse,
  onDrag,
}: {
  count: number;
  collapsed: boolean;
  mode: Mode;
  onMode: (mode: Mode) => void;
  onList: () => void;
  onMenu: () => void;
  onCollapse: () => void;
  onDrag: (event: PointerEvent<HTMLButtonElement>) => void;
}) {
  if (collapsed)
    return (
      <Button
        aria-label="Expand Gobby Annotate"
        title="Expand Gobby Annotate"
        className="annotate-collapsed"
        size="icon"
        onClick={onCollapse}
        onPointerDown={onDrag}
      >
        <MessageSquare size={18} />
      </Button>
    );
  return (
    <div
      role="toolbar"
      aria-label="Gobby Annotate"
      className="annotate-toolbar"
    >
      <Button
        size="icon"
        aria-label="Move toolbar"
        title="Drag to move; use menu to dock with keyboard"
        onPointerDown={onDrag}
      >
        <Move size={18} />
      </Button>
      {mode === "browse" ? (
        <>
          <Button
            size="icon"
            aria-label="Select element"
            title="Select element"
            onClick={() => onMode("element")}
          >
            <MousePointer2 size={18} />
          </Button>
          <Button
            size="icon"
            aria-label="Select rectangle"
            title="Select rectangle"
            onClick={() => onMode("rectangle")}
          >
            <Scan size={18} />
          </Button>
        </>
      ) : (
        <Button onClick={() => onMode("browse")}>Cancel</Button>
      )}
      <Button
        aria-label={`Annotations (${count})`}
        title="Annotations"
        onClick={onList}
      >
        <List size={18} />
        <span>{count}</span>
      </Button>
      <Button
        size="icon"
        aria-label="Batch and export menu"
        title="Batch and export"
        onClick={onMenu}
      >
        <Menu size={18} />
      </Button>
      <Button
        size="icon"
        aria-label="Collapse toolbar"
        title="Collapse"
        onClick={onCollapse}
      >
        <Minimize2 size={18} />
      </Button>
    </div>
  );
}
