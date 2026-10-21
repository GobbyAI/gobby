import { Button } from "../../../../web/src/components/ui/Button";
import { MousePointer2, Scan, List, Menu, Minimize2, Move } from "lucide-react";
import type { PointerEvent } from "react";
import type { Mode } from "./selection";
import icon from "../../../safari/Assets.xcassets/AppIcon.appiconset/icon-128.png";
export function Toolbar({
  count,
  collapsed,
  mode,
  panel,
  onMode,
  onList,
  onMenu,
  onCollapse,
  onDrag,
}: {
  count: number;
  collapsed: boolean;
  mode: Mode;
  panel: "none" | "list" | "menu";
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
        <img src={icon} alt="" width={28} height={28} draggable={false} />
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
      <Button
        size="icon"
        aria-label="Select element"
        aria-pressed={mode === "element"}
        title={
          mode === "element"
            ? "Stop selecting element (press again)"
            : "Select element"
        }
        onClick={() => onMode(mode === "element" ? "browse" : "element")}
      >
        <MousePointer2 size={18} />
      </Button>
      <Button
        size="icon"
        aria-label="Select rectangle"
        aria-pressed={mode === "rectangle"}
        title={
          mode === "rectangle"
            ? "Stop selecting rectangle (press again)"
            : "Select rectangle"
        }
        onClick={() => onMode(mode === "rectangle" ? "browse" : "rectangle")}
      >
        <Scan size={18} />
      </Button>
      <Button
        aria-label={`Annotations (${count})`}
        aria-expanded={panel === "list"}
        title="Annotations"
        onClick={onList}
      >
        <List size={18} />
        <span>{count}</span>
      </Button>
      <Button
        size="icon"
        aria-label="Batch and export menu"
        aria-expanded={panel === "menu"}
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
