import { useState, useMemo, useRef, useEffect } from "react";
import type { ProjectOption } from "../types/chat";
import { cn } from "../lib/utils";

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
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!showProjectSearch) return;
    const handleClick = (e: MouseEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setShowProjectSearch(false);
        setProjectSearch("");
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [showProjectSearch]);

  return (
    <div className="relative" ref={containerRef}>
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
      {showProjectSearch && (
        <div
          className={cn(
            "absolute right-0 w-48 rounded-md border border-border bg-background shadow-lg z-50",
            dropDirection === "up" ? "bottom-full mb-1" : "top-full mt-1",
          )}
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
          <div id="project-search-results" className="max-h-32 overflow-y-auto">
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
        </div>
      )}
    </div>
  );
}
