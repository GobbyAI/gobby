import { createElement, type ReactNode } from "react";
import type { Components } from "react-markdown";
import {
  Anchor,
  CodeBlockInner,
  ImageBlock,
  TableWrapper,
} from "./CodeBlockRenderers";

// Headings render with browser defaults under Tailwind's preflight (margins
// reset to 0), so they jam against the following text. Give them an explicit
// top-margin / weight / size ladder so plans and chat markdown read with
// hierarchy; first-child clears the leading gap.
const heading =
  (level: 1 | 2 | 3 | 4, cls: string): Components["h1"] =>
  ({ children }: { children?: ReactNode }) =>
    createElement(`h${level}`, { className: cls }, children);

export const codeBlockComponents: Partial<Components> = {
  code: CodeBlockInner as Components["code"],
  table: TableWrapper as Components["table"],
  a: Anchor as Components["a"],
  img: ImageBlock as Components["img"],
  h1: heading(1, "mt-4 mb-2 text-2xl font-semibold [&:first-child]:mt-0"),
  h2: heading(2, "mt-4 mb-2 text-xl font-semibold [&:first-child]:mt-0"),
  h3: heading(3, "mt-3 mb-1.5 text-lg font-semibold [&:first-child]:mt-0"),
  h4: heading(4, "mt-3 mb-1 text-base font-semibold [&:first-child]:mt-0"),
};
