import {
  useState,
  useMemo,
  useRef,
  useEffect,
  useCallback,
  useId,
} from "react";
import { createPortal } from "react-dom";
import type { ProjectOption } from "../types/chat";
import { cn } from "../lib/utils";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { SegmentedControl } from "./ui/SegmentedControl";
import { coarseHitAreaCls } from "./ui/controlStyles";

type ProjectMode = "personal" | "project";
type PickerMode = "search" | "compact";
type PickerRestoreFocus = false | "compact" | "search";

/** Width of the project picker in pixels (matches Tailwind w-48). */
const PICKER_WIDTH = 192;

/** Sub-label for a project this machine has no checkout of. */
const NOT_CHECKED_OUT_LABEL = "not checked out on this machine";

const isOptionAvailable = (project: ProjectOption) =>
  project.hasCheckout !== false;

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
  const selectedLabel = isPersonal ? "Personal" : (selectedName ?? "Project");
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
  const searchInputRef = useRef<HTMLInputElement>(null);
  const pickerRef = useRef<HTMLDivElement>(null);
  const compactListboxRef = useRef<HTMLDivElement>(null);
  const [pickerPos, setPickerPos] = useState<{
    top: number;
    left: number;
  } | null>(null);
  const showProjectSearch = pickerMode === "search";
  const showCompactMenu = pickerMode === "compact";
  const pickerOptions = showCompactMenu ? compactOptions : filtered;
  const boundedActiveOptionIndex =
    pickerOptions.length === 0
      ? 0
      : Math.min(activeOptionIndex, pickerOptions.length - 1);
  const pickerIdBase = useId();
  const listboxId = `${pickerIdBase}-project-options`;
  const activeOptionId = pickerOptions[boundedActiveOptionIndex]
    ? `${listboxId}-option-${boundedActiveOptionIndex}`
    : undefined;

  const closePicker = useCallback(
    (restoreFocus: PickerRestoreFocus = false) => {
      const restoreCompactFocus = () => compactTriggerRef.current?.focus();
      const restoreSearchFocus = () => {
        triggerRef.current
          ?.querySelector<HTMLElement>('[role="radio"][aria-checked="true"]')
          ?.focus();
      };
      setPickerMode(null);
      setProjectSearch("");
      setActiveOptionIndex(0);
      if (restoreFocus === "compact") {
        requestAnimationFrame(restoreCompactFocus);
      }
      if (restoreFocus === "search") {
        requestAnimationFrame(restoreSearchFocus);
      }
    },
    [],
  );

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

  useEffect(() => {
    if (!showCompactMenu) return;
    requestAnimationFrame(() => compactListboxRef.current?.focus());
  }, [showCompactMenu]);

  const handleModeChange = (next: ProjectMode) => {
    if (next === "personal") {
      if (personalProject) onProjectChange(personalProject.id);
      closePicker();
      return;
    }
    if (
      nonPersonalProjects.length === 1 &&
      isOptionAvailable(nonPersonalProjects[0])
    ) {
      onProjectChange(nonPersonalProjects[0].id);
    } else {
      setActiveOptionIndex(0);
      setPickerMode((prev) => (prev === "search" ? null : "search"));
    }
  };

  const handleProjectSelect = (
    projectId: string,
    restoreFocus: PickerRestoreFocus = showCompactMenu ? "compact" : false,
  ) => {
    onProjectChange(projectId);
    closePicker(restoreFocus);
  };

  const toggleCompactMenu = () => {
    setActiveOptionIndex(0);
    setPickerMode((prev) => (prev === "compact" ? null : "compact"));
  };

  const handleArrowNavigation = (
    e: React.KeyboardEvent<HTMLInputElement | HTMLDivElement>,
    onSelect: () => void,
    options: { preventEnterDefault?: boolean } = {},
  ) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveOptionIndex((prev) =>
        pickerOptions.length === 0
          ? 0
          : Math.min(prev + 1, pickerOptions.length - 1),
      );
      return true;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveOptionIndex((prev) => Math.max(prev - 1, 0));
      return true;
    }
    if (e.key === "Enter" && pickerOptions[boundedActiveOptionIndex]) {
      if (options.preventEnterDefault) {
        e.preventDefault();
      }
      if (isOptionAvailable(pickerOptions[boundedActiveOptionIndex])) {
        onSelect();
      }
      return true;
    }
    return false;
  };

  const handleProjectSearchChange = (
    e: React.ChangeEvent<HTMLInputElement>,
  ) => {
    setProjectSearch(e.target.value);
    setActiveOptionIndex(0);
  };

  const handleSearchKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      closePicker("search");
      return;
    }
    handleArrowNavigation(e, () =>
      handleProjectSelect(pickerOptions[boundedActiveOptionIndex].id, "search"),
    );
  };

  const handleCompactTriggerKeyDown = (
    e: React.KeyboardEvent<HTMLButtonElement>,
  ) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggleCompactMenu();
      return;
    }
    if (e.key === "Escape" && showCompactMenu) {
      e.preventDefault();
      closePicker("compact");
    }
  };

  const handlePickerKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      closePicker(showCompactMenu ? "compact" : false);
      return;
    }
    if (!showCompactMenu) {
      return;
    }
    handleArrowNavigation(
      e,
      () => handleProjectSelect(pickerOptions[boundedActiveOptionIndex].id),
      {
        preventEnterDefault: true,
      },
    );
  };

  const isOptionSelected = (project: ProjectOption) =>
    project.name === "Personal" ? isPersonal : project.id === selectedProjectId;

  return (
    <div
      className="relative min-w-0 mobile:w-25"
      ref={triggerRef}
      role="group"
      aria-label="Project selector"
    >
      <div className="mobile:hidden">
        {/* No overflow-hidden on the segmented control: it would clip the
            options' coarse-pointer ::before hit-area expansion out of
            hit-testing. Edge options round themselves to match the track. */}
        <SegmentedControl<ProjectMode>
          value={isPersonal ? "personal" : "project"}
          onChange={handleModeChange}
          options={[
            { value: "personal", label: "Personal" },
            { value: "project", label: selectedName ?? "Project" },
          ]}
          ariaLabel="Project scope"
          disabled={disabled}
        />
      </div>
      {/* No overflow-hidden here: it would clip the coarse-pointer ::before
          hit-area expansion out of hit-testing, capping the tap target at the
          28px row. The trigger rounds itself to match the border instead. */}
      <div className="hidden h-[var(--control-row-height)] min-h-[var(--control-row-height)] w-full items-stretch rounded-md border border-border bg-background mobile:inline-flex">
        <Button
          ref={compactTriggerRef}
          type="button"
          variant="ghost"
          size="sm"
          dense
          className={cn(
            "w-full rounded-[inherit] py-0 [font-family:inherit]",
            coarseHitAreaCls,
          )}
          onClick={toggleCompactMenu}
          onKeyDown={handleCompactTriggerKeyDown}
          disabled={disabled}
          aria-label={`Project scope: ${selectedLabel}`}
          aria-haspopup="listbox"
          aria-expanded={showCompactMenu}
          aria-controls={showCompactMenu ? listboxId : undefined}
        >
          <span className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap">
            {selectedLabel}
          </span>
        </Button>
      </div>
      {pickerMode &&
        pickerPos &&
        createPortal(
          <div
            ref={pickerRef}
            className="fixed z-[1000] w-48 rounded-md border border-border bg-background shadow-lg"
            style={{
              top: pickerPos.top,
              left: pickerPos.left,
              ...(dropDirection === "up"
                ? { transform: "translateY(-100%) translateY(-4px)" }
                : {}),
            }}
            onKeyDown={handlePickerKeyDown}
          >
            {showProjectSearch && (
              <Input
                ref={searchInputRef}
                className="h-auto rounded-none border-0 border-b border-border bg-transparent px-2 py-1.5 text-xs text-foreground placeholder:text-muted-foreground"
                placeholder="Search"
                value={projectSearch}
                onChange={handleProjectSearchChange}
                onKeyDown={handleSearchKeyDown}
                role="combobox"
                aria-expanded={showProjectSearch}
                aria-controls={listboxId}
                aria-activedescendant={activeOptionId}
                aria-autocomplete="list"
                autoFocus
              />
            )}
            <div
              ref={showCompactMenu ? compactListboxRef : undefined}
              id={listboxId}
              className="max-h-32 overflow-y-auto"
              role="listbox"
              aria-label={
                showCompactMenu
                  ? "Project scope options"
                  : "Project search results"
              }
              aria-activedescendant={
                showCompactMenu ? activeOptionId : undefined
              }
              tabIndex={showCompactMenu ? -1 : undefined}
            >
              {pickerOptions.map((p, index) => {
                const available = isOptionAvailable(p);
                return (
                  <Button
                    key={p.id}
                    id={`${listboxId}-option-${index}`}
                    type="button"
                    variant="ghost"
                    size="sm"
                    dense
                    role="option"
                    aria-selected={isOptionSelected(p)}
                    aria-disabled={available ? undefined : true}
                    tabIndex={-1}
                    className={cn(
                      "min-h-0 w-full justify-start rounded-none border-0 px-2 py-1 text-left text-xs font-normal",
                      coarseHitAreaCls,
                      isOptionSelected(p) && "bg-accent/20 text-accent",
                      !available && "cursor-not-allowed text-muted-foreground",
                      (showProjectSearch || showCompactMenu) &&
                        index === boundedActiveOptionIndex &&
                        "bg-muted",
                    )}
                    onClick={() => {
                      if (available) handleProjectSelect(p.id);
                    }}
                  >
                    {available ? (
                      p.name
                    ) : (
                      <span className="flex min-w-0 flex-col items-start">
                        <span className="max-w-full truncate">{p.name}</span>
                        <span className="text-[length:var(--text-2xs)] leading-tight text-muted-foreground/80">
                          {NOT_CHECKED_OUT_LABEL}
                        </span>
                      </span>
                    )}
                  </Button>
                );
              })}
            </div>
            {/* Outside the listbox so it only ever contains options. */}
            {pickerOptions.length === 0 && (
              <div className="px-2 py-1 text-xs text-muted-foreground">
                No projects found
              </div>
            )}
          </div>,
          document.body,
        )}
    </div>
  );
}
