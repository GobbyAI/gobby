import { useCallback, useEffect, useMemo, useState } from "react";

import type { Channel, CommsMessage } from "../../../hooks/useIntegrations";
import { cn } from "../../../lib/utils";
import { Button } from "../../ui/Button";
import { ActivityPanelEmpty, TasksEmptyIcon } from "../ActivityPanelEmpty";
import { DetailActionButton, DetailPaneHeader } from "../fields";
import { loadChannelMessages } from "./IntegrationsTabData";
import { IntegrationPlatformIcon } from "./IntegrationPlatformIcon";
import { formatRelativeTime } from "./IntegrationsTabModel";
import { CHANNEL_DISPLAY_NAMES } from "./channelMetadata";

const MESSAGE_LIMIT_STEP = 20;

interface MessagesViewProps {
  channel: Channel;
  onClose: () => void;
}

function directionLabel(direction: CommsMessage["direction"]): string {
  return direction === "outbound" ? "Outbound" : "Inbound";
}

function DirectionIcon({
  direction,
}: {
  direction: CommsMessage["direction"];
}) {
  const isOutbound = direction === "outbound";
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {isOutbound ? (
        <>
          <path d="M7 17 17 7" />
          <path d="M9 7h8v8" />
        </>
      ) : (
        <>
          <path d="M17 7 7 17" />
          <path d="M15 17H7V9" />
        </>
      )}
    </svg>
  );
}

function normalizeStatus(status: string): string {
  return status.trim() || "unknown";
}

export function MessagesView({ channel, onClose }: MessagesViewProps) {
  const [messages, setMessages] = useState<CommsMessage[]>([]);
  const [limit, setLimit] = useState(MESSAGE_LIMIT_STEP);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void loadChannelMessages(channel.id, limit)
      .then((loaded) => {
        if (!cancelled) setMessages(loaded);
      })
      .catch((loadError) => {
        if (!cancelled) {
          setError(
            loadError instanceof Error
              ? loadError.message
              : "Failed to load messages",
          );
          setMessages([]);
        }
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [channel.id, limit]);

  const sortedMessages = useMemo(
    () =>
      [...messages].sort(
        (left, right) =>
          new Date(left.created_at).getTime() -
          new Date(right.created_at).getTime(),
      ),
    [messages],
  );

  const loadOlder = useCallback(() => {
    setIsLoading(true);
    setError(null);
    setLimit((current) => current + MESSAGE_LIMIT_STEP);
  }, []);

  return (
    <div className="flex h-full min-h-0 flex-col bg-[var(--bg-primary)]">
      <DetailPaneHeader
        title={
          <div className="flex min-w-0 items-center gap-2">
            <IntegrationPlatformIcon type={channel.channel_type} size={16} />
            <h2 className="m-0 truncate text-sm font-medium text-foreground">
              Messages
            </h2>
            <span className="truncate text-xs font-normal text-muted-foreground">
              {CHANNEL_DISPLAY_NAMES[channel.channel_type]} / {channel.name}
            </span>
          </div>
        }
        dirty={false}
        onSave={() => undefined}
        onDiscard={() => undefined}
        actions={
          <DetailActionButton label="Close messages" onClick={onClose} />
        }
      />

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {isLoading && sortedMessages.length === 0 ? (
          <ActivityPanelEmpty
            icon={<TasksEmptyIcon />}
            heading="Messages"
            body="Loading recent channel messages..."
          />
        ) : error ? (
          <ActivityPanelEmpty
            icon={<TasksEmptyIcon />}
            heading="Messages unavailable"
            body={error}
          />
        ) : sortedMessages.length === 0 ? (
          <ActivityPanelEmpty
            icon={<TasksEmptyIcon />}
            heading="No messages yet"
            body={`Recent inbound and outbound messages for ${channel.name} will appear here.`}
          />
        ) : (
          <div className="flex min-h-full flex-col justify-end gap-3">
            <div className="flex justify-center">
              <Button
                type="button"
                size="sm"
                disabled={isLoading}
                onClick={loadOlder}
              >
                {isLoading ? "Loading..." : "Load older messages"}
              </Button>
            </div>

            <ol
              className="grid grid-cols-1 gap-2"
              aria-label={`Messages for ${channel.name}`}
            >
              {sortedMessages.map((message) => {
                const isOutbound = message.direction === "outbound";
                return (
                  <li
                    key={message.id}
                    className={cn(
                      "flex",
                      isOutbound ? "justify-end" : "justify-start",
                    )}
                  >
                    <article
                      className={cn(
                        "max-w-[88%] rounded-lg border border-border bg-[var(--bg-secondary)] p-3",
                        "text-sm text-foreground shadow-sm",
                        isOutbound
                          ? "items-end text-right"
                          : "items-start text-left",
                      )}
                    >
                      <div
                        className={cn(
                          "mb-2 flex items-center gap-2 text-xs text-muted-foreground",
                          isOutbound && "justify-end",
                        )}
                      >
                        <DirectionIcon direction={message.direction} />
                        <span>{directionLabel(message.direction)}</span>
                        <span>/</span>
                        <span>{normalizeStatus(message.status)}</span>
                        <span>/</span>
                        <time dateTime={message.created_at}>
                          {formatRelativeTime(message.created_at)}
                        </time>
                      </div>
                      <p className="m-0 leading-relaxed break-words whitespace-pre-wrap">
                        {message.content}
                      </p>
                      {message.error && (
                        <div className="mt-2 rounded-md bg-error-soft px-2 py-1 text-xs text-error">
                          {message.error}
                        </div>
                      )}
                    </article>
                  </li>
                );
              })}
            </ol>
          </div>
        )}
      </div>
    </div>
  );
}
