import { useMemo } from "react";

import { useProjects, type ProjectWithStats } from "../../../hooks/useProjects";
import { SelectField } from "./FieldPrimitives";
import type { DraftFieldBaseProps, FieldOption } from "./types";

interface ProjectSelectFieldProps extends DraftFieldBaseProps {
  value: string;
  onChange: (value: string) => void;
}

type ProjectRegistryState = ReturnType<typeof useProjects> & {
  error?: unknown;
  isError?: boolean;
};

function projectLabel(project: ProjectWithStats): string {
  return project.display_name.trim() || project.name || project.id;
}

function unknownProjectLabel(projectId: string): string {
  return `Unknown project (${projectId.slice(0, 8)})`;
}

function projectOptions(
  projects: ProjectWithStats[],
  value: string,
  placeholder: string | undefined,
): FieldOption[] {
  const options: FieldOption[] = [
    {
      value: "",
      label: placeholder ?? "No project",
    },
    ...projects.map((project) => ({
      value: project.id,
      label: projectLabel(project),
    })),
  ];

  if (value && !projects.some((project) => project.id === value)) {
    options.splice(1, 0, {
      value,
      label: unknownProjectLabel(value),
    });
  }

  return options;
}

function hintOptions(value: string, label: string): FieldOption[] {
  return [{ value, label }];
}

export function ProjectSelectField({
  label,
  value,
  onChange,
  disabled,
  placeholder,
  ariaLabel,
}: ProjectSelectFieldProps) {
  const registry = useProjects() as ProjectRegistryState;
  const projects =
    registry.allProjects.length > 0 ? registry.allProjects : registry.projects;
  const hasError = Boolean(registry.error || registry.isError);

  const options = useMemo(() => {
    if (registry.isLoading) {
      return hintOptions(value, "Loading projects");
    }

    if (hasError) {
      return hintOptions(value, "Projects unavailable");
    }

    return projectOptions(projects, value, placeholder);
  }, [hasError, placeholder, projects, registry.isLoading, value]);

  return (
    <SelectField
      label={label}
      ariaLabel={ariaLabel}
      value={value}
      onChange={onChange}
      disabled={disabled || registry.isLoading || hasError}
      options={options}
    />
  );
}
