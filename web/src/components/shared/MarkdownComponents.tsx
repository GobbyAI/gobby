import type { Components } from "react-markdown";
import {
  MarkdownAnchor,
  MarkdownCodeBlock,
  MarkdownTableWrapper,
} from "./MarkdownRenderers";

// Export components for ReactMarkdown
export const markdownComponents: Partial<Components> = {
  code: MarkdownCodeBlock as Components["code"],
  table: MarkdownTableWrapper as Components["table"],
  a: MarkdownAnchor as Components["a"],
};
