import type { ChannelType } from "../../../hooks/useIntegrations";

// gobby_chat is deliberately absent: the internal chat channel is hidden from
// the Integrations surface entirely (#19152).
export const INTEGRATION_CHANNEL_TYPES: ChannelType[] = [
  "slack",
  "telegram",
  "discord",
  "teams",
  "email",
  "sms",
];

export const CHANNEL_DISPLAY_NAMES: Record<ChannelType, string> = {
  slack: "Slack",
  telegram: "Telegram",
  discord: "Discord",
  teams: "Teams",
  email: "Email",
  sms: "SMS",
  gobby_chat: "Gobby Chat",
};

export interface ChannelFieldDefinition {
  key: string;
  label: string;
  secret?: boolean;
  required?: boolean;
  placeholder?: string;
  type?: string;
}

export const CHANNEL_TYPE_FIELDS: Record<
  ChannelType,
  ChannelFieldDefinition[]
> = {
  slack: [
    { key: "bot_token", label: "Bot Token", secret: true, required: true },
    {
      key: "signing_secret",
      label: "Signing Secret",
      secret: true,
      required: true,
    },
    { key: "channel_id", label: "Channel ID", placeholder: "C0123456789" },
  ],
  telegram: [
    { key: "bot_token", label: "Bot Token", secret: true, required: true },
    { key: "chat_id", label: "Chat ID", placeholder: "-1001234567890" },
  ],
  discord: [
    { key: "bot_token", label: "Bot Token", secret: true, required: true },
    { key: "channel_id", label: "Channel ID", placeholder: "1234567890" },
  ],
  teams: [
    { key: "app_id", label: "App ID", secret: true, required: true },
    {
      key: "app_password",
      label: "App Password",
      secret: true,
      required: true,
    },
  ],
  email: [
    { key: "password", label: "Password", secret: true, required: true },
    {
      key: "smtp_host",
      label: "SMTP Host",
      required: true,
      placeholder: "smtp.gmail.com",
    },
    {
      key: "smtp_port",
      label: "SMTP Port",
      required: true,
      type: "number",
      placeholder: "587",
    },
    {
      key: "imap_host",
      label: "IMAP Host",
      required: true,
      placeholder: "imap.gmail.com",
    },
    {
      key: "imap_port",
      label: "IMAP Port",
      required: true,
      type: "number",
      placeholder: "993",
    },
    {
      key: "from_address",
      label: "From Address",
      required: true,
      type: "email",
      placeholder: "you@example.com",
    },
  ],
  sms: [
    { key: "auth_token", label: "Auth Token", secret: true, required: true },
    {
      key: "account_sid",
      label: "Account SID",
      required: true,
      placeholder: "AC...",
    },
    {
      key: "from_number",
      label: "From Number",
      required: true,
      placeholder: "+15551234567",
    },
  ],
  gobby_chat: [],
};
