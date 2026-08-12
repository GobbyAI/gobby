import { JsonBlock } from "./JsonBlock";

interface UnknownBlockCardProps {
  blockType: string;
  raw: Record<string, unknown>;
}

export function UnknownBlockCard({ blockType, raw }: UnknownBlockCardProps) {
  return (
    <div className="my-1.5 rounded border border-warning-foreground/30 bg-warning-foreground/5 text-xs">
      <details>
        <summary className="cursor-pointer px-3 py-1.5 font-medium text-warning-foreground/80 select-none hover:text-warning-foreground">
          Unknown block: <code className="ml-1 font-mono">{blockType}</code>
        </summary>
        <JsonBlock
          value={raw}
          className="border-t border-warning-foreground/20"
        />
      </details>
    </div>
  );
}
