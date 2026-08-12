import { useState, useEffect, useCallback, useRef } from "react";
import { useWebSocketEvent } from "./useWebSocketEvent";

export type ChannelType =
  "slack" | "telegram" | "discord" | "teams" | "email" | "sms" | "gobby_chat";

export interface Channel {
  id: string;
  channel_type: ChannelType;
  name: string;
  enabled: boolean;
  config_json: Record<string, unknown>;
  webhook_secret: string | null;
  created_at: string;
  updated_at: string;
}

export interface ChannelStatus {
  name: string;
  channel_type: string;
  status: string;
  active: boolean;
  enabled: boolean;
  supports_webhooks?: boolean;
  supports_polling?: boolean;
  is_polling?: boolean;
}

export interface CommsMessage {
  id: string;
  channel_id: string;
  identity_id: string | null;
  direction: "inbound" | "outbound";
  content: string;
  content_type: string;
  platform_message_id: string | null;
  platform_thread_id: string | null;
  session_id: string | null;
  status: string;
  error: string | null;
  metadata_json: Record<string, unknown>;
  created_at: string;
}

export interface MessageFilters {
  channelId: string | null;
  direction: "inbound" | "outbound" | null;
  limit: number;
  offset: number;
}

function getBaseUrl(): string {
  return "";
}

export function useIntegrations() {
  const [channels, setChannels] = useState<Channel[]>([]);
  const [messages, setMessages] = useState<CommsMessage[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [searchText, setSearchText] = useState("");
  const [channelTypeFilter, setChannelTypeFilter] =
    useState<ChannelType | null>(null);
  const [messageFilters, setMessageFiltersRaw] = useState<MessageFilters>({
    channelId: null,
    direction: null,
    limit: 50,
    offset: 0,
  });
  const setMessageFilters = useCallback((update: Partial<MessageFilters>) => {
    setMessageFiltersRaw((prev) => ({ ...prev, ...update }));
  }, []);
  const debounceRef = useRef<number | null>(null);

  const fetchChannels = useCallback(async () => {
    try {
      const baseUrl = getBaseUrl();
      const response = await fetch(`${baseUrl}/api/comms/channels`);
      if (response.ok) {
        const data = await response.json();
        setChannels(Array.isArray(data) ? data : data.channels || []);
      }
    } catch (e) {
      console.error("Failed to fetch channels:", e);
    }
  }, []);

  const createChannel = useCallback(
    async (
      channelType: string,
      name: string,
      config: Record<string, unknown>,
      secrets?: Record<string, unknown>,
    ): Promise<boolean> => {
      try {
        const baseUrl = getBaseUrl();
        const response = await fetch(`${baseUrl}/api/comms/channels`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            channel_type: channelType,
            name,
            config,
            secrets: secrets || null,
          }),
        });
        if (response.ok) {
          await fetchChannels();
          return true;
        }
      } catch (e) {
        console.error("Failed to create channel:", e);
      }
      return false;
    },
    [fetchChannels],
  );

  const updateChannel = useCallback(
    async (
      channelId: string,
      updates: { config?: Record<string, unknown>; enabled?: boolean },
    ): Promise<boolean> => {
      try {
        const baseUrl = getBaseUrl();
        const response = await fetch(
          `${baseUrl}/api/comms/channels/${encodeURIComponent(channelId)}`,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(updates),
          },
        );
        if (response.ok) {
          await fetchChannels();
          return true;
        }
      } catch (e) {
        console.error("Failed to update channel:", e);
      }
      return false;
    },
    [fetchChannels],
  );

  const removeChannel = useCallback(
    async (channelId: string): Promise<boolean> => {
      try {
        const baseUrl = getBaseUrl();
        const response = await fetch(
          `${baseUrl}/api/comms/channels/${encodeURIComponent(channelId)}`,
          {
            method: "DELETE",
          },
        );
        if (response.ok) {
          await fetchChannels();
          return true;
        }
      } catch (e) {
        console.error("Failed to remove channel:", e);
      }
      return false;
    },
    [fetchChannels],
  );

  const fetchChannelStatus = useCallback(
    async (channelId: string): Promise<ChannelStatus | null> => {
      try {
        const baseUrl = getBaseUrl();
        const response = await fetch(
          `${baseUrl}/api/comms/channels/${encodeURIComponent(channelId)}/status`,
        );
        if (response.ok) {
          return await response.json();
        }
      } catch (e) {
        console.error("Failed to fetch channel status:", e);
      }
      return null;
    },
    [],
  );

  const fetchMessages = useCallback(
    async (filters?: Partial<MessageFilters>) => {
      const merged = { ...messageFilters, ...filters };
      try {
        const baseUrl = getBaseUrl();
        const params = new URLSearchParams();
        if (merged.channelId) params.set("channel_id", merged.channelId);
        if (merged.direction) params.set("direction", merged.direction);
        params.set("limit", String(merged.limit));
        params.set("offset", String(merged.offset));

        const response = await fetch(`${baseUrl}/api/comms/messages?${params}`);
        if (response.ok) {
          const data = await response.json();
          setMessages(Array.isArray(data) ? data : data.messages || []);
        }
      } catch (e) {
        console.error("Failed to fetch messages:", e);
      }
    },
    [messageFilters],
  );

  // Auto-fetch channels on mount
  useEffect(() => {
    const load = async () => {
      setIsLoading(true);
      await fetchChannels();
      setIsLoading(false);
    };
    load();
  }, [fetchChannels]);

  // Cleanup debounce timer on unmount
  useEffect(() => {
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current);
    };
  }, []);

  // Real-time updates via WebSocket (debounced)
  useWebSocketEvent(
    "comms_event",
    useCallback(() => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current);
      debounceRef.current = window.setTimeout(() => {
        fetchChannels();
      }, 500);
    }, [fetchChannels]),
  );

  return {
    channels,
    messages,
    isLoading,
    searchText,
    setSearchText,
    channelTypeFilter,
    setChannelTypeFilter,
    messageFilters,
    setMessageFilters,
    fetchChannels,
    createChannel,
    updateChannel,
    removeChannel,
    fetchChannelStatus,
    fetchMessages,
  };
}
