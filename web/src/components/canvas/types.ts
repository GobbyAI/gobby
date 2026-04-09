export interface BoundValue {
  literalString?: string;
  path?: string; // JSON path/pointer into dataModel
}

export interface Action {
  name: string;
  context?: Record<string, BoundValue>;
}

export interface ChildrenSpec {
  explicitList?: string[];
}

export interface A2UIComponentDef {
  type: string;
  text?: BoundValue;
  label?: BoundValue;
  actions?: Action[];
  children?: ChildrenSpec;
  // Other component-specific properties
  [key: string]: any;
}

export interface A2UISurfaceState {
  canvasId: string;
  conversationId: string;
  mode: string;
  surface: Record<string, A2UIComponentDef>;
  dataModel: Record<string, any>;
  rootComponentId: string | null;
  completed: boolean;
}

export interface UserAction {
  name: string;
  sourceComponentId: string;
  timestamp: string;
  context: Record<string, any>;
}

export interface CanvasEvent {
  type: string;
  event: string;
  canvas_id: string;
  [key: string]: any;
}

export interface A2UIComponentProps {
  componentId: string;
  def: A2UIComponentDef;
  surface: Record<string, A2UIComponentDef>;
  dataModel: Record<string, any>;
  onAction: (action: UserAction) => void;
  updateField: (path: string, value: any) => void;
  completed: boolean;
  depth?: number;
}

// Utility functions

const resolvePath = (obj: any, path: string): any => {
  if (!path || !obj) return undefined;

  // Handle simple dot notation (e.g. "user.name")
  // Note: JSON pointer uses "/" but dot notation is common. Let's support dot notation for simplicity
  // or real JSON pointer if requested. The doc says "path is JSON pointer" so let's allow basic dot notation.
  const parts = path.replace(/^\//, "").split(/[/.]/);

  let current = obj;
  for (const part of parts) {
    if (current === undefined || current === null) return undefined;
    current = current[part];
  }
  return current;
};

export const resolveBoundValue = (bv?: BoundValue, dataModel?: Record<string, any>): any => {
  if (!bv) return undefined;
  if (bv.literalString !== undefined) return bv.literalString;
  if (bv.path && dataModel) return resolvePath(dataModel, bv.path) ?? "";
  return "";
};

export const resolveActionContext = (
  context?: Record<string, BoundValue>,
  dataModel?: Record<string, any>
): Record<string, any> => {
  if (!context) return {};
  const resolved: Record<string, any> = {};
  for (const [key, bv] of Object.entries(context)) {
    resolved[key] = resolveBoundValue(bv, dataModel);
  }
  return resolved;
};
