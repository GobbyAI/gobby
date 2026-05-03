import React from 'react';
import { A2UISurfaceState, A2UIComponentDef, UserAction, ChildrenSpec } from './types';
import { useA2UIDataModel } from './hooks/useA2UIDataModel';

import { A2UIText } from './components/A2UIText';
import { A2UIButton } from './components/A2UIButton';
import { A2UITextField } from './components/A2UITextField';
import { A2UICheckBox } from './components/A2UICheckBox';
import { A2UIRow } from './components/A2UIRow';
import { A2UIColumn } from './components/A2UIColumn';
import { A2UICard } from './components/A2UICard';
import { A2UIList } from './components/A2UIList';
import { A2UIImage } from './components/A2UIImage';
import { A2UIIcon } from './components/A2UIIcon';
import { A2UIBadge } from './components/A2UIBadge';

const MAX_RENDER_DEPTH = 20;

const COMPONENT_MAP: Record<string, React.FC<any>> = {
  Text: A2UIText,
  Button: A2UIButton,
  TextField: A2UITextField,
  CheckBox: A2UICheckBox,
  Row: A2UIRow,
  Column: A2UIColumn,
  Card: A2UICard,
  List: A2UIList,
  Image: A2UIImage,
  Icon: A2UIIcon,
  Badge: A2UIBadge,
};

class CanvasErrorBoundary extends React.Component<{ children: React.ReactNode }, { hasError: boolean }> {
  constructor(props: any) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  render() {
    if (this.state.hasError) {
      return <div className="p-2 border border-destructive text-destructive text-sm rounded bg-destructive/10">Failed to render component</div>;
    }
    return this.props.children;
  }
}

export const RenderComponent: React.FC<{
  componentId: string;
  surface: Record<string, A2UIComponentDef>;
  dataModel: Record<string, any>;
  onAction: (action: UserAction) => void;
  updateField: (path: string, value: any) => void;
  completed: boolean;
  depth?: number;
}> = ({ componentId, surface, dataModel, onAction, updateField, completed, depth = 0 }) => {
  if (depth > MAX_RENDER_DEPTH) {
    return <div className="p-2 bg-destructive/10 text-destructive text-xs rounded">Max render depth exceeded</div>;
  }

  const def = surface[componentId];
  if (!def) return null;

  const Component = COMPONENT_MAP[def.type];
  if (!Component) {
    return <div className="p-2 bg-destructive/10 text-destructive text-xs rounded">Unknown type: {def.type}</div>;
  }

  return (
    <CanvasErrorBoundary>
      <Component
        componentId={componentId}
        def={def}
        surface={surface}
        dataModel={dataModel}
        onAction={onAction}
        updateField={updateField}
        completed={completed}
        depth={depth}
      />
    </CanvasErrorBoundary>
  );
};

export const RenderChildren: React.FC<{
  childrenSpec?: ChildrenSpec;
  surface: Record<string, A2UIComponentDef>;
  dataModel: Record<string, any>;
  onAction: (action: UserAction) => void;
  updateField?: (path: string, value: any) => void;
  completed: boolean;
  depth?: number;
}> = ({ childrenSpec, surface, dataModel, onAction, updateField = () => {}, completed, depth = 0 }) => {
  if (!childrenSpec?.explicitList) return null;

  return (
    <>
      {childrenSpec.explicitList.map(childId => (
        <RenderComponent
          key={childId}
          componentId={childId}
          surface={surface}
          dataModel={dataModel}
          onAction={onAction}
          updateField={updateField}
          completed={completed}
          depth={depth + 1}
        />
      ))}
    </>
  );
};

export interface A2UIRendererProps {
  surfaceState: A2UISurfaceState;
  onAction: (canvasId: string, action: UserAction) => void;
}

export const A2UIRenderer: React.FC<A2UIRendererProps> = ({ surfaceState, onAction }) => {
  const { dataModel, updateField } = useA2UIDataModel(surfaceState.dataModel);

  const rootId = surfaceState.rootComponentId;
  const completed = surfaceState.completed;

  if (!rootId) return null;

  const handleAction = (action: UserAction) => {
    if (!completed) {
      onAction(surfaceState.canvasId, action);
    }
  };

  return (
    <div className={`rounded-lg border border-accent/30 bg-accent/5 p-3 flex flex-col gap-2 relative ${completed ? 'opacity-60 pointer-events-none' : ''}`}>
      {completed && <div className="absolute top-2 right-2 text-[10px] font-bold text-muted-foreground uppercase tracking-widest bg-background border border-border px-2 py-0.5 rounded">Completed</div>}
      <RenderComponent
        componentId={rootId}
        surface={surfaceState.surface}
        dataModel={dataModel}
        onAction={handleAction}
        updateField={updateField}
        completed={completed}
      />
    </div>
  );
};
