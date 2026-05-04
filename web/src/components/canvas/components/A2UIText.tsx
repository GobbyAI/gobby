import React from 'react';
import { A2UIComponentProps, resolveBoundValue } from '../types';

export const A2UIText: React.FC<A2UIComponentProps> = ({ def, dataModel }) => {
  const text = resolveBoundValue(def.text, dataModel);
  const style = def.style || 'body'; // e.g. 'title1', 'title2', 'body'

  if (!text) return null;

  switch (style) {
    case 'title1':
      return <h1 className="text-xl font-bold">{text}</h1>;
    case 'title2':
      return <h2 className="text-lg font-semibold">{text}</h2>;
    case 'title3':
      return <h3 className="font-medium text-foreground/90">{text}</h3>;
    case 'body':
    default:
      return <p className="text-sm text-foreground">{text}</p>;
  }
};
