import { useState, useMemo, useRef, useEffect, useCallback } from "react";
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
  const pickerRef = useRef<HTMLDivElement>(null);
  const [pickerPos, setPickerPos] = useState<{ top: number; left: number } | null>(null);
  const showProjectSearch = pickerMode === "search";
  const showCompactMenu = pickerMode === "compact";
  const pickerOptions = showCompactMenu ? compactOptions : filtered;

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
      setPickerMode(null);
      setProjectSearch("");
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
  }, [pickerMode, updatePosition]);

  const handleModeChange = (next: ProjectMode) => {
    if (next === "personal") {
      if (personalProject) onProjectChange(personalProject.id);
      setPickerMode(null);
      return;
    }
    if (nonPersonalProjects.length === 1) {
      onProjectChange(nonPersonalProjects[0].id);
    } else {
      setPickerMode((prev) => (prev === "search" ? null : "search"));
    }
  };

  const handleProjectSelect = (projectId: string) => {
    onProjectChange(projectId);
    setPickerMode(null);
    setProjectSearch("");
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
          type="button"
          className="project-selector-compact-trigger"
          onClick={() =>
            setPickerMode((prev) => (prev === "compact" ? null : "compact"))
          }
          disabled={disabled}
          aria-label={`Project scope: ${selectedLabel}`}
          aria-haspopup="listbox"
          aria-expanded={showCompactMenu}
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
            role="listbox"
            aria-label={showCompactMenu ? "Project scope options" : "Project search results"}
          >
            {showProjectSearch && (
              <input
                className={`w-full px-2 py-1.5 text-xs bg-transparent border-b border-border text-foreground placeholder:text-muted-foreground ${inputFocusCls}`}
                placeholder="Search"
                value={projectSearch}
                onChange={(e) => setProjectSearch(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") {
                    setPickerMode(null);
                    setProjectSearch("");
                  }
                  if (e.key === "Enter" && filtered.length > 0) {
                    handleProjectSelect(filtered[0].id);
                  }
                }}
                role="combobox"
                aria-expanded={true}
                aria-controls="project-search-results"
                aria-autocomplete="list"
                autoFocus
              />
            )}
            <div
              id="project-search-results"
              className="max-h-32 overflow-y-auto"
            >
              {pickerOptions.map((p) => (
                <button
                  key={p.id}
                  role="option"
                  aria-selected={isOptionSelected(p)}
                  className={cn(
                    "w-full text-left px-2 py-1 text-xs hover:bg-muted",
                    isOptionSelected(p) && "bg-accent/20 text-accent",
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
