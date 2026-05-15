import { useState, useMemo, useRef, useEffect, useCallback, useId } from "react";
import { createPortal } from "react-dom";
import type { ProjectOption } from "../types/chat";
import { cn } from "../lib/utils";
import { SegmentedControl } from "./ui/SegmentedControl";
import { inputFocusCls } from "./shared/focusStyles";

type ProjectMode = "personal" | "project";
type PickerMode = "search" | "compact";

/** Width of the project picker in pixels (matches Tailwind w-48). */
const PICKER_WIDTH = 192;

interface ProjectSelectorProps {
  projects: ProjectOption[];
  selectedProjectId: string | null;
  onProjectChange: (projectId: string) => void;
  disabled?: boolean;
  dropDirection?: "up" | "down";
}

export function ProjectSelector({
  projects,
  selectedProjectId,
  onProjectChange,
  disabled = false,
  dropDirection = "down",
}: ProjectSelectorProps) {
  const personalProject = projects.find((p) => p.name === "Personal");
  const isPersonal =
    !selectedProjectId || selectedProjectId === personalProject?.id;
  const selectedName = !isPersonal
    ? projects.find((p) => p.id === selectedProjectId)?.name
    : null;
  const selectedLabel = isPersonal ? "Personal" : selectedName ?? "Project";
  const nonPersonalProjects = useMemo(
    () => projects.filter((p) => p.name !== "Personal"),
    [projects],
  );
  const compactOptions = useMemo(
    () => [
      ...(personalProject ? [personalProject] : []),
      ...nonPersonalProjects,
    ],
    [nonPersonalProjects, personalProject],
  );
  const [pickerMode, setPickerMode] = useState<PickerMode | null>(null);
  const [projectSearch, setProjectSearch] = useState("");
  const [activeOptionIndex, setActiveOptionIndex] = useState(0);
  const filtered = useMemo(
    () =>
      projectSearch
        ? nonPersonalProjects.filter((p) =>
            p.name.toLowerCase().includes(projectSearch.toLowerCase()),
          )
        : nonPersonalProjects,
    [nonPersonalProjects, projectSearch],
  );
  const triggerRef = useRef<HTMLDivElement>(null);
  const compactTriggerRef = useRef<HTMLButtonElement>(null);
  const pickerRef = useRef<HTMLDivElement>(null);
  const [pickerPos, setPickerPos] = useState<{ top: number; left: number } | null>(null);
  const showProjectSearch = pickerMode === "search";
  const showCompactMenu = pickerMode === "compact";
  const pickerOptions = showCompactMenu ? compactOptions : filtered;
  const boundedActiveOptionIndex =
    pickerOptions.length === 0
      ? 0
      : Math.min(activeOptionIndex, pickerOptions.length - 1);
  const pickerIdBase = useId();
  const listboxId = `${pickerIdBase}-project-options`;
  const activeOptionId =
    showProjectSearch && pickerOptions[boundedActiveOptionIndex]
      ? `${listboxId}-option-${boundedActiveOptionIndex}`
      : undefined;

  const closePicker = useCallback((restoreCompactFocus = false) => {
    setPickerMode(null);
    setProjectSearch("");
    setActiveOptionIndex(0);
    if (restoreCompactFocus) {
      requestAnimationFrame(() => compactTriggerRef.current?.focus());
    }
  }, []);

  const updatePosition = useCallback(() => {
    if (!triggerRef.current) return;
    const rect = triggerRef.current.getBoundingClientRect();
    const margin = 8;
    const desiredLeft = rect.right - PICKER_WIDTH;
    // Clamp horizontally so the picker can't escape the viewport when the
    // trigger sits near the right edge or on a narrow screen.
    const left = Math.max(
      margin,
      Math.min(desiredLeft, window.innerWidth - PICKER_WIDTH - margin),
    );
    const desiredTop = dropDirection === "up" ? rect.top : rect.bottom + 4;
    // Vertical clamp uses an estimated max height — the dropdown content is
    // capped via max-h-32 inside the search results plus the input row, so
    // 200px is a safe upper bound.
    const estimatedHeight = 200;
    const top = Math.max(
      margin,
      Math.min(desiredTop, window.innerHeight - estimatedHeight - margin),
    );
    setPickerPos({ top, left });
  }, [dropDirection]);

  useEffect(() => {
    if (!pickerMode) return;
    updatePosition();
    const handleClick = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        triggerRef.current?.contains(target) ||
        pickerRef.current?.contains(target)
      )
        return;
      closePicker();
    };
    const handleScrollOrResize = () => updatePosition();
    document.addEventListener("mousedown", handleClick);
    window.addEventListener("scroll", handleScrollOrResize, true);
    window.addEventListener("resize", handleScrollOrResize);
    return () => {
      document.removeEventListener("mousedown", handleClick);
      window.removeEventListener("scroll", handleScrollOrResize, true);
      window.removeEventListener("resize", handleScrollOrResize);
    };
  }, [closePicker, pickerMode, updatePosition]);

  const handleModeChange = (next: ProjectMode) => {
    if (next === "personal") {
      if (personalProject) onProjectChange(personalProject.id);
      closePicker();
      return;
    }
    if (nonPersonalProjects.length === 1) {
      onProjectChange(nonPersonalProjects[0].id);
    } else {
      setActiveOptionIndex(0);
      setPickerMode((prev) => (prev === "search" ? null : "search"));
    }
  };

  const handleProjectSelect = (projectId: string) => {
    onProjectChange(projectId);
    closePicker();
  };

  const toggleCompactMenu = () => {
    setActiveOptionIndex(0);
    setPickerMode((prev) => (prev === "compact" ? null : "compact"));
  };

  const handleProjectSearchChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setProjectSearch(e.target.value);
    setActiveOptionIndex(0);
  };

  const handleSearchKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      closePicker();
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveOptionIndex((prev) =>
        pickerOptions.length === 0 ? 0 : Math.min(prev + 1, pickerOptions.length - 1),
      );
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveOptionIndex((prev) => Math.max(prev - 1, 0));
      return;
    }
    if (e.key === "Enter" && pickerOptions[boundedActiveOptionIndex]) {
      handleProjectSelect(pickerOptions[boundedActiveOptionIndex].id);
    }
  };

  const handleCompactTriggerKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggleCompactMenu();
      return;
    }
    if (e.key === "Escape" && showCompactMenu) {
      e.preventDefault();
      closePicker(true);
    }
  };

  const handlePickerKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      closePicker(showCompactMenu);
    }
  };

  const isOptionSelected = (project: ProjectOption) =>
    project.name === "Personal" ? isPersonal : project.id === selectedProjectId;

  return (
    <div className="project-selector" ref={triggerRef}>
      <div className="project-selector-segmented-wrap">
        <SegmentedControl<ProjectMode>
          value={isPersonal ? "personal" : "project"}
          onChange={handleModeChange}
          options={[
            { value: "personal", label: "Personal" },
            { value: "project", label: selectedName ?? "Project" },
          ]}
          ariaLabel="Project scope"
          size="md"
          disabled={disabled}
          className="project-selector-segmented"
        />
      </div>
      <div className="project-selector-compact-wrap">
        <button
          ref={compactTriggerRef}
          type="button"
          className="project-selector-compact-trigger"
          onClick={toggleCompactMenu}
          onKeyDown={handleCompactTriggerKeyDown}
          disabled={disabled}
          aria-label={`Project scope: ${selectedLabel}`}
          aria-haspopup="listbox"
          aria-expanded={showCompactMenu}
          aria-controls={showCompactMenu ? listboxId : undefined}
        >
          <span className="project-selector-compact-label">{selectedLabel}</span>
        </button>
      </div>
      {pickerMode &&
        pickerPos &&
        createPortal(
          <div
            ref={pickerRef}
            className="fixed w-48 rounded-md border border-border bg-background shadow-lg z-[1000]"
            style={{
              top: pickerPos.top,
              left: pickerPos.left,
              ...(dropDirection === "up" ? { transform: "translateY(-100%) translateY(-4px)" } : {}),
            }}
            onKeyDown={handlePickerKeyDown}
          >
            {showProjectSearch && (
              <input
                className={`w-full px-2 py-1.5 text-xs bg-transparent border-b border-border text-foreground placeholder:text-muted-foreground ${inputFocusCls}`}
                placeholder="Search"
                value={projectSearch}
                onChange={handleProjectSearchChange}
                onKeyDown={handleSearchKeyDown}
                role="combobox"
                aria-expanded={showProjectSearch}
                aria-controls={listboxId}
                aria-owns={listboxId}
                aria-activedescendant={activeOptionId}
                aria-autocomplete="list"
                autoFocus
              />
            )}
            <div
              id={listboxId}
              className="max-h-32 overflow-y-auto"
              role="listbox"
              aria-label={showCompactMenu ? "Project scope options" : "Project search results"}
            >
              {pickerOptions.map((p, index) => (
                <button
                  key={p.id}
                  id={`${listboxId}-option-${index}`}
                  role="option"
                  aria-selected={isOptionSelected(p)}
                  className={cn(
                    "w-full text-left px-2 py-1 text-xs hover:bg-muted",
                    isOptionSelected(p) && "bg-accent/20 text-accent",
                    showProjectSearch && index === boundedActiveOptionIndex && "bg-muted",
                  )}
                  onClick={() => handleProjectSelect(p.id)}
                >
                  {p.name}
                </button>
              ))}
              {pickerOptions.length === 0 && (
                <div className="px-2 py-1 text-xs text-muted-foreground">
                  No projects found
                </div>
              )}
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}
