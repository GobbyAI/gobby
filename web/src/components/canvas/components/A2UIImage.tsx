import React from 'react';
import { A2UIComponentProps, resolveBoundValue } from '../types';

export const A2UIImage: React.FC<A2UIComponentProps> = ({ def, dataModel }) => {
  const src = resolveBoundValue(def.src, dataModel);
  const alt = resolveBoundValue(def.alt, dataModel) || "Image";

  if (!src) return null;

  // Security validation: only allow https:// or data:image/
  if (!src.startsWith('https://') && !src.startsWith('data:image/')) {
    return (
      <div className="p-2 border border-destructive text-destructive text-xs rounded bg-destructive/10">
        Invalid image source
      </div>
    );
  }

  return (
    <img
      src={src}
      alt={alt}
      loading="lazy"
      decoding="async"
      className="max-w-full rounded-md object-contain"
      style={{
        width: def.width,
        height: def.height
      }}
    />
  );
};
