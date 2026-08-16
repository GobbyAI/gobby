import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { useWebSocketEvent } from "./useWebSocketEvent";

export interface PipelineDefDetail {
  id: string;
  name: string;
  kind?: string;
  description: string | null;
  version: string;
  enabled: boolean;
  source: string;
  tags: string[] | null;
  project_id: string | null;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
  definition_json: string;
  canvas_json: string | null;
  has_template_update?: boolean;
}

interface PipelineFilters {
  enabled?: boolean;
  project_id?: string;
  include_deleted?: boolean;
}

function getBaseUrl(): string {
  return "";
}

export function usePipelineDefs() {
  const [pipelines, setPipelines] = useState<PipelineDefDetail[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedPipeline, setSelectedPipeline] =
    useState<PipelineDefDetail | null>(null);
  const listRequestGenerationRef = useRef(0);
  const selectionRequestGenerationRef = useRef(0);
  const filtersRef = useRef<PipelineFilters | undefined>(undefined);

  const fetchPipelines = useCallback(
    async (params?: PipelineFilters): Promise<boolean> => {
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
        const url = `${getBaseUrl()}/api/pipelines/definitions${
          query ? `?${query}` : ""
        }`;

        const response = await fetch(url);
        if (response.ok) {
          const data = await response.json();
          if (requestGeneration === listRequestGenerationRef.current) {
            setPipelines(data.definitions || []);
          }
          return true;
        }
      } catch (error) {
        if (requestGeneration === listRequestGenerationRef.current) {
          console.error("Failed to fetch pipeline definitions:", error);
        }
      }
      return false;
    },
    [],
  );

  const refetchPipelines = useCallback(
    () => fetchPipelines(filtersRef.current),
    [fetchPipelines],
  );

  const fetchPipeline = useCallback(
    async (id: string): Promise<PipelineDefDetail | null> => {
      try {
        const response = await fetch(
          `${getBaseUrl()}/api/pipelines/definitions/${encodeURIComponent(id)}`,
        );
        if (response.ok) {
          const data = await response.json();
          return data.definition || null;
        }
      } catch (error) {
        console.error("Failed to fetch pipeline definition:", error);
      }
      return null;
    },
    [],
  );

  const createPipeline = useCallback(
    async (params: {
      name: string;
      definition_json: string;
      description?: string;
      enabled?: boolean;
      tags?: string[];
    }): Promise<PipelineDefDetail | null> => {
      try {
        const response = await fetch(
          `${getBaseUrl()}/api/pipelines/definitions`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(params),
          },
        );
        if (response.ok) {
          const data = await response.json();
          if (data.status === "success") {
            await refetchPipelines();
            return data.definition;
          }
        }
      } catch (error) {
        console.error("Failed to create pipeline definition:", error);
      }
      return null;
    },
    [refetchPipelines],
  );

  const updatePipeline = useCallback(
    async (
      id: string,
      params: {
        name?: string;
        definition_json?: string;
        description?: string;
        enabled?: boolean;
        tags?: string[];
        canvas_json?: string;
      },
    ): Promise<PipelineDefDetail | null> => {
      try {
        const response = await fetch(
          `${getBaseUrl()}/api/pipelines/definitions/${encodeURIComponent(id)}`,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(params),
          },
        );
        if (response.ok) {
          const data = await response.json();
          if (data.status === "success") {
            await refetchPipelines();
            return data.definition;
          }
        }
      } catch (error) {
        console.error("Failed to update pipeline definition:", error);
      }
      return null;
    },
    [refetchPipelines],
  );

  const deletePipeline = useCallback(
    async (id: string): Promise<boolean> => {
      try {
        const response = await fetch(
          `${getBaseUrl()}/api/pipelines/definitions/${encodeURIComponent(id)}`,
          {
            method: "DELETE",
          },
        );
        if (response.ok) {
          const data = await response.json();
          if (data.deleted) {
            if (selectedId === id) {
              setSelectedId(null);
              setSelectedPipeline(null);
            }
            await refetchPipelines();
            return true;
          }
        }
      } catch (error) {
        console.error("Failed to delete pipeline definition:", error);
      }
      return false;
    },
    [refetchPipelines, selectedId],
  );

  const duplicatePipeline = useCallback(
    async (id: string, newName: string): Promise<PipelineDefDetail | null> => {
      try {
        const response = await fetch(
          `${getBaseUrl()}/api/pipelines/definitions/${encodeURIComponent(id)}/duplicate`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ new_name: newName }),
          },
        );
        if (response.ok) {
          const data = await response.json();
          if (data.status === "success") {
            await refetchPipelines();
            return data.definition;
          }
        }
      } catch (error) {
        console.error("Failed to duplicate pipeline definition:", error);
      }
      return null;
    },
    [refetchPipelines],
  );

  const toggleEnabled = useCallback(
    async (id: string): Promise<PipelineDefDetail | null> => {
      try {
        const response = await fetch(
          `${getBaseUrl()}/api/pipelines/definitions/${encodeURIComponent(id)}/toggle`,
          {
            method: "PUT",
          },
        );
        if (response.ok) {
          const data = await response.json();
          if (data.status === "success") {
            await refetchPipelines();
            return data.definition;
          }
        }
      } catch (error) {
        console.error("Failed to toggle pipeline definition:", error);
      }
      return null;
    },
    [refetchPipelines],
  );

  const importYaml = useCallback(
    async (
      yamlContent: string,
      projectId?: string,
    ): Promise<PipelineDefDetail | null> => {
      try {
        const response = await fetch(
          `${getBaseUrl()}/api/pipelines/definitions/import`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              yaml_content: yamlContent,
              project_id: projectId,
            }),
          },
        );
        if (response.ok) {
          const data = await response.json();
          if (data.status === "success") {
            await refetchPipelines();
            return data.definition;
          }
        }
      } catch (error) {
        console.error("Failed to import pipeline YAML:", error);
      }
      return null;
    },
    [refetchPipelines],
  );

  const exportYaml = useCallback(async (id: string): Promise<string | null> => {
    try {
      const response = await fetch(
        `${getBaseUrl()}/api/pipelines/definitions/${encodeURIComponent(id)}/export`,
      );
      if (response.ok) {
        return await response.text();
      }
    } catch (error) {
      console.error("Failed to export pipeline YAML:", error);
    }
    return null;
  }, []);

  const restorePipeline = useCallback(
    async (id: string): Promise<boolean> => {
      try {
        const response = await fetch(
          `${getBaseUrl()}/api/pipelines/definitions/${encodeURIComponent(id)}/restore`,
          {
            method: "POST",
          },
        );
        if (response.ok) {
          const data = await response.json();
          if (data.status === "success") {
            await refetchPipelines();
            return true;
          }
        }
      } catch (error) {
        console.error("Failed to restore pipeline definition:", error);
      }
      return false;
    },
    [refetchPipelines],
  );

  const selectPipeline = useCallback(
    async (id: string | null) => {
      const requestGeneration = ++selectionRequestGenerationRef.current;
      setSelectedId(id);
      if (id) {
        const detail = await fetchPipeline(id);
        if (requestGeneration === selectionRequestGenerationRef.current) {
          setSelectedPipeline(detail);
        }
      } else {
        setSelectedPipeline(null);
      }
    },
    [fetchPipeline],
  );

  const activeCount = useMemo(() => {
    return pipelines.filter((pipeline) => pipeline.enabled).length;
  }, [pipelines]);

  useEffect(() => {
    setIsLoading(true);
    fetchPipelines().finally(() => setIsLoading(false));
  }, [fetchPipelines]);

  const debounceRef = useRef<number | null>(null);
  useEffect(() => {
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current);
      listRequestGenerationRef.current += 1;
      selectionRequestGenerationRef.current += 1;
    };
  }, []);
  useWebSocketEvent(
    "workflow_event",
    useCallback(() => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current);
      debounceRef.current = window.setTimeout(() => refetchPipelines(), 500);
    }, [refetchPipelines]),
  );

  return {
    pipelines,
    isLoading,
    selectedId,
    selectedPipeline,
    activeCount,
    fetchPipelines,
    fetchPipeline,
    createPipeline,
    updatePipeline,
    deletePipeline,
    duplicatePipeline,
    toggleEnabled,
    importYaml,
    exportYaml,
    restorePipeline,
    selectPipeline,
  };
}
