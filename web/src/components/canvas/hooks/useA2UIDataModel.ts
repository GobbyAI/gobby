import { useState, useCallback, useEffect } from 'react';

export const useA2UIDataModel = (initialDataModel: Record<string, any>) => {
  const [dataModel, setDataModel] = useState<Record<string, any>>(initialDataModel || {});

  // Sync from props
  useEffect(() => {
    setDataModel(initialDataModel || {});
  }, [initialDataModel]);

  const updateField = useCallback((path: string, value: any) => {
    setDataModel(prev => {
      // Create a deep copy to allow mutation without altering previous state references directly
      const next = JSON.parse(JSON.stringify(prev));

      const parts = path.replace(/^\//, "").split(/[/.]/);
      let current = next;

      for (let i = 0; i < parts.length - 1; i++) {
        const part = parts[i];
        if (!(part in current)) {
          current[part] = {};
        }
        current = current[part];
      }

      const lastPart = parts[parts.length - 1];
      current[lastPart] = value;

      return next;
    });
  }, []);

  const mergeDataModel = useCallback((updates: Record<string, any>) => {
    setDataModel(prev => ({
      ...prev,
      ...updates
    }));
  }, []);

  const resetDataModel = useCallback((newModel: Record<string, any>) => {
    setDataModel(newModel || {});
  }, []);

  return {
    dataModel,
    updateField,
    mergeDataModel,
    resetDataModel
  };
};
