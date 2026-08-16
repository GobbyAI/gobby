import { useState, useEffect, useCallback, useRef } from "react";
import { useWebSocketEvent } from "./useWebSocketEvent";

export interface VariableDef {
  id: string;
  name: string;
  description: string | null;
  enabled: boolean;
  source: string;
  tags: string[] | null;
  project_id: string | null;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
  default_value: unknown;
  has_template_update?: boolean;
}

interface VariableFilters {
  enabled?: boolean;
  project_id?: string;
  include_deleted?: boolean;
}

interface VariableRow {
  id?: unknown;
  name?: unknown;
  description?: unknown;
  enabled?: unknown;
  source?: unknown;
  tags?: unknown;
  project_id?: unknown;
  created_at?: unknown;
  updated_at?: unknown;
  deleted_at?: unknown;
  default_value?: unknown;
  value?: unknown;
  has_template_update?: unknown;
}

function getBaseUrl(): string {
  return "";
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asNullableString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function toVariableDef(row: VariableRow): VariableDef {
  return {
    id: asString(row.id),
    name: asString(row.name),
    description: asNullableString(row.description),
    enabled: Boolean(row.enabled),
    source: asString(row.source),
    tags: Array.isArray(row.tags)
      ? row.tags.filter((tag): tag is string => typeof tag === "string")
      : null,
    project_id: asNullableString(row.project_id),
    created_at: asString(row.created_at),
    updated_at: asString(row.updated_at),
    deleted_at: asNullableString(row.deleted_at),
    default_value:
      row.default_value !== undefined ? row.default_value : row.value,
    has_template_update:
      typeof row.has_template_update === "boolean"
        ? row.has_template_update
        : undefined,
  };
}

export function useVariableDefs() {
  const [variables, setVariables] = useState<VariableDef[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const listRequestGenerationRef = useRef(0);
  const filtersRef = useRef<VariableFilters | undefined>(undefined);

  const fetchVariables = useCallback(
    async (params?: VariableFilters): Promise<boolean> => {
      filtersRef.current = params;
      const requestGeneration = ++listRequestGenerationRef.current;
      try {
        const searchParams = new URLSearchParams();
        if (params?.enabled !== undefined)
          searchParams.set("enabled", String(params.enabled));
        if (params?.project_id)
          searchParams.set("project_id", params.project_id);
        if (params?.include_deleted)
          searchParams.set("include_deleted", "true");
        const query = searchParams.toString();
        const url = `${getBaseUrl()}/api/variables${query ? `?${query}` : ""}`;

        const response = await fetch(url);
        if (response.ok) {
          const data = await response.json();
          if (requestGeneration === listRequestGenerationRef.current) {
            const rows = Array.isArray(data.variables) ? data.variables : [];
            setVariables(rows.map((row: VariableRow) => toVariableDef(row)));
          }
          return true;
        }
      } catch (error) {
        if (requestGeneration === listRequestGenerationRef.current) {
          console.error("Failed to fetch variable defaults:", error);
        }
      }
      return false;
    },
    [],
  );

  const refetchVariables = useCallback(
    () => fetchVariables(filtersRef.current),
    [fetchVariables],
  );

  const createVariable = useCallback(
    async (params: {
      name: string;
      value?: unknown;
      description?: string;
      enabled?: boolean;
      project_id?: string;
      tags?: string[];
    }): Promise<VariableDef | null> => {
      try {
        const response = await fetch(`${getBaseUrl()}/api/variables`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(params),
        });
        if (response.ok) {
          const data = await response.json();
          if (data.status === "success") {
            await refetchVariables();
            return data.variable ? toVariableDef(data.variable) : null;
          }
        }
      } catch (error) {
        console.error("Failed to create variable default:", error);
      }
      return null;
    },
    [refetchVariables],
  );

  const toggleEnabled = useCallback(
    async (id: string): Promise<VariableDef | null> => {
      try {
        const response = await fetch(
          `${getBaseUrl()}/api/variables/${encodeURIComponent(id)}/toggle`,
          { method: "PUT" },
        );
        if (response.ok) {
          const data = await response.json();
          if (data.status === "success") {
            await refetchVariables();
            return data.variable ? toVariableDef(data.variable) : null;
          }
        }
      } catch (error) {
        console.error("Failed to toggle variable default:", error);
      }
      return null;
    },
    [refetchVariables],
  );

  const deleteVariable = useCallback(
    async (id: string): Promise<boolean> => {
      try {
        const response = await fetch(
          `${getBaseUrl()}/api/variables/${encodeURIComponent(id)}`,
          { method: "DELETE" },
        );
        if (response.ok) {
          const data = await response.json();
          if (data.deleted) {
            await refetchVariables();
            return true;
          }
        }
      } catch (error) {
        console.error("Failed to delete variable default:", error);
      }
      return false;
    },
    [refetchVariables],
  );

  useEffect(() => {
    setIsLoading(true);
    fetchVariables().finally(() => setIsLoading(false));
  }, [fetchVariables]);

  const debounceRef = useRef<number | null>(null);
  useEffect(() => {
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current);
      listRequestGenerationRef.current += 1;
    };
  }, []);
  useWebSocketEvent(
    "workflow_event",
    useCallback(() => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current);
      debounceRef.current = window.setTimeout(() => refetchVariables(), 500);
    }, [refetchVariables]),
  );

  return {
    variables,
    isLoading,
    fetchVariables,
    createVariable,
    toggleEnabled,
    deleteVariable,
  };
}
