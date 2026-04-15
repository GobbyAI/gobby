import claudeLogo from "../../assets/provider-logos/claude-symbol.svg";
import geminiLogo from "../../assets/provider-logos/gemini-icon-2025.svg";
import codexLogo from "../../assets/provider-logos/openai-symbol-2025.svg";
import qwenLogo from "../../assets/provider-logos/qwen-logo.svg";
import { SOURCE_COLORS } from "./sourceTheme";
import type { SourceType } from "./sourceIconUtils";

interface SourceIconProps {
  source: SourceType;
  size?: number;
}

export function SourceIcon({ source, size = 14 }: SourceIconProps) {
  const providerLogo =
    {
      claude: claudeLogo,
      gemini: geminiLogo,
      qwen: qwenLogo,
      codex: codexLogo,
    }[source as "claude" | "gemini" | "qwen" | "codex"] || null;

  if (providerLogo) {
    return (
      <img
        src={providerLogo}
        width={size}
        height={size}
        className={`source-icon source-icon-${source}`}
        alt=""
        aria-hidden="true"
        draggable={false}
      />
    );
  }

  const color = SOURCE_COLORS[source] || SOURCE_COLORS.default;

  switch (source) {
    default:
      return (
        <svg
          width={size}
          height={size}
          viewBox="0 0 24 24"
          fill="none"
          className="source-icon"
          stroke={color}
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="10" />
        </svg>
      );
  }
}
