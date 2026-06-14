import type { ChannelType } from "../../../hooks/useIntegrations";

interface IntegrationPlatformIconProps {
  type: ChannelType;
  size?: number;
}

export function IntegrationPlatformIcon({ type, size = 16 }: IntegrationPlatformIconProps) {
  const props = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 2,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };

  switch (type) {
    case "slack":
      return (
        <svg {...props}>
          <line x1="12" y1="2" x2="12" y2="22" />
          <line x1="2" y1="12" x2="22" y2="12" />
        </svg>
      );
    case "telegram":
      return (
        <svg {...props}>
          <line x1="22" y1="2" x2="11" y2="13" />
          <polygon points="22 2 15 22 11 13 2 9 22 2" fill="none" />
        </svg>
      );
    case "discord":
      return (
        <svg {...props}>
          <path d="M6 11a1 1 0 1 1 0 2 1 1 0 0 1 0-2" />
          <path d="M18 11a1 1 0 1 1 0 2 1 1 0 0 1 0-2" />
          <path d="M8 4c-2 0-4 1-5 3 4 8 6 13 9 13s5-5 9-13c-1-2-3-3-5-3" />
        </svg>
      );
    case "teams":
      return (
        <svg {...props}>
          <rect x="3" y="3" width="8" height="8" rx="1" />
          <rect x="13" y="3" width="8" height="8" rx="1" />
          <rect x="3" y="13" width="8" height="8" rx="1" />
          <rect x="13" y="13" width="8" height="8" rx="1" />
        </svg>
      );
    case "email":
      return (
        <svg {...props}>
          <rect x="2" y="4" width="20" height="16" rx="2" />
          <path d="M22 7l-10 7L2 7" />
        </svg>
      );
    case "sms":
      return (
        <svg {...props}>
          <rect x="5" y="2" width="14" height="20" rx="2" />
          <line x1="12" y1="18" x2="12.01" y2="18" />
        </svg>
      );
    case "gobby_chat":
      return (
        <svg {...props}>
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
        </svg>
      );
  }
}
