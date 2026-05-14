import React from 'react';
import { A2UIComponentProps, resolveBoundValue } from '../types';
import { Heading } from '../../shared/Heading'

export const A2UIText: React.FC<A2UIComponentProps> = ({ def, dataModel }) => {
  const text = resolveBoundValue(def.text, dataModel);
  const style = def.style || 'body'; // e.g. 'title1', 'title2', 'body'

  if (!text) return null;

  switch (style) {
    case 'title1':
      return <Heading level={1} className="text-xl font-bold">{text}</Heading>;
    case 'title2':
      return <Heading level={2} className="text-lg font-semibold">{text}</Heading>;
    case 'title3':
      return <Heading level={3} className="font-medium text-foreground/90">{text}</Heading>;
    case 'body':
    default:
      return <p className="text-sm text-foreground">{text}</p>;
  }
};
