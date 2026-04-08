import { useState, useMemo, useRef, useEffect, useCallback } from "react";
import { createPortal } from "react-dom";
import type { ProjectOption } from "../types/chat";
import { cn } from "../lib/utils";

/** Width of the project dropdown in pixels (matches Tailwind w-48). */
const DROPDOWN_WIDTH = 192;

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
  const nonPersonalProjects = useMemo(
    () => projects.filter((p) => p.name !== "Personal"),
    [projects],
  );
  const [showProjectSearch, setShowProjectSearch] = useState(false);
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
  const dropdownRef = useRef<HTMLDivElement>(null);
  const [dropdownPos, setDropdownPos] = useState<{ top: number; left: number; width: number } | null>(null);

  const updatePosition = useCallback(() => {
    if (!triggerRef.current) return;
    const rect = triggerRef.current.getBoundingClientRect();
    setDropdownPos({
      top: dropDirection === "up" ? rect.top : rect.bottom + 4,
      left: rect.right - DROPDOWN_WIDTH,
      width: DROPDOWN_WIDTH,
    });
  }, [dropDirection]);

  useEffect(() => {
    if (!showProjectSearch) return;
    updatePosition();
    const handleClick = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        triggerRef.current?.contains(target) ||
        dropdownRef.current?.contains(target)
      )
        return;
      setShowProjectSearch(false);
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
  }, [showProjectSearch, updatePosition]);

  return (
    <div className="relative" ref={triggerRef}>
      <div className="flex rounded-md border border-border text-xs">
        <button
          className={cn(
            "px-2 py-1 rounded-l-md transition-colors",
            isPersonal
              ? "bg-accent text-accent-foreground"
              : "text-muted-foreground hover:bg-muted",
          )}
          onClick={() => {
            if (personalProject) onProjectChange(personalProject.id);
            setShowProjectSearch(false);
          }}
          disabled={disabled}
        >
          Personal
        </button>
        <button
          className={cn(
            "px-2 py-1 rounded-r-md transition-colors",
            !isPersonal
              ? "bg-accent text-accent-foreground"
              : "text-muted-foreground hover:bg-muted",
          )}
          onClick={() => {
            if (nonPersonalProjects.length === 1)
              onProjectChange(nonPersonalProjects[0].id);
            else setShowProjectSearch(!showProjectSearch);
          }}
          disabled={disabled}
          aria-haspopup="listbox"
          aria-expanded={showProjectSearch}
        >
          {selectedName ?? "Project"}
        </button>
      </div>
      {showProjectSearch &&
        dropdownPos &&
        createPortal(
          <div
            ref={dropdownRef}
            className="fixed w-48 rounded-md border border-border bg-background shadow-lg z-[250]"
            style={{
              top: dropdownPos.top,
              left: dropdownPos.left,
              ...(dropDirection === "up" ? { transform: "translateY(-100%) translateY(-4px)" } : {}),
            }}
            role="listbox"
            aria-label="Project search results"
          >
            <input
              className="w-full px-2 py-1.5 text-xs bg-transparent border-b border-border text-foreground placeholder:text-muted-foreground focus:outline-none"
              placeholder="Search projects..."
              value={projectSearch}
              onChange={(e) => setProjectSearch(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  setShowProjectSearch(false);
                  setProjectSearch("");
                }
                if (e.key === "Enter" && filtered.length > 0) {
                  onProjectChange(filtered[0].id);
                  setShowProjectSearch(false);
                  setProjectSearch("");
                }
              }}
              role="combobox"
              aria-expanded={true}
              aria-controls="project-search-results"
              aria-autocomplete="list"
              autoFocus
            />
            <div
              id="project-search-results"
              className="max-h-32 overflow-y-auto"
            >
              {filtered.map((p) => (
                <button
                  key={p.id}
                  role="option"
                  aria-selected={p.id === selectedProjectId}
                  className={cn(
                    "w-full text-left px-2 py-1 text-xs hover:bg-muted",
                    p.id === selectedProjectId && "bg-accent/20 text-accent",
                  )}
                  onClick={() => {
                    onProjectChange(p.id);
                    setShowProjectSearch(false);
                    setProjectSearch("");
                  }}
                >
                  {p.name}
                </button>
              ))}
              {filtered.length === 0 && (
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
