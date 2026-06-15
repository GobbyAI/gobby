import { NumberField, SwitchField, TextField } from '../../activity/fields'
import { KeyValueMapField, StringListField, TypedListField } from '../fields'
import {
  asMap,
  asNumber,
  asString,
  asStringList,
  asTypedList,
} from './configAccessors'
import {
  NumberConfigField,
  StringListConfigField,
  Subsection,
  SwitchConfigField,
  TextConfigField,
} from './configFields'
import { SettingsSection, type SettingsSectionFields } from './SettingsSection'

/**
 * Config rows this section owns, per the configuration audit (IA section 7,
 * "Integrations & Hooks"). The draft is scoped to exactly these dotted paths;
 * every keep-row appears once. Two rows the legacy form rendered as a single
 * free-text fallback are migrated to real editors here:
 * `hook_extensions.websocket.broadcast_events` (string list) and
 * `hook_extensions.webhooks.endpoints` (structured object list).
 */
const OWNED_PATHS: readonly string[] = [
  'communications.enabled',
  'communications.webhook_base_url',
  'communications.channel_defaults.rate_limit_per_minute',
  'communications.channel_defaults.burst',
  'communications.channel_defaults.retry_count',
  'communications.channel_defaults.poll_interval_seconds',
  'communications.channel_defaults.retention_days',
  'communications.inbound_enabled',
  'communications.outbound_enabled',
  'communications.auto_create_sessions',
  'hook_extensions.websocket.enabled',
  'hook_extensions.websocket.broadcast_events',
  'hook_extensions.websocket.include_payload',
  'hook_extensions.webhooks.enabled',
  'hook_extensions.webhooks.endpoints',
  'hook_extensions.webhooks.default_timeout',
  'hook_extensions.webhooks.async_dispatch',
]

const WEBHOOK_ENDPOINTS_PATH = 'hook_extensions.webhooks.endpoints'

/** Editable view of one `WebhookEndpointConfig` (src/gobby/config/extensions.py). */
interface WebhookEndpoint {
  name: string
  url: string
  events: string[]
  headers: Record<string, string>
  timeout: number | null
  retry_count: number | null
  retry_delay: number | null
  can_block: boolean
  enabled: boolean
}

/** Coerce a stored endpoint entry into the editor shape, defaulting safely. */
function asEndpoint(value: unknown): WebhookEndpoint {
  const entry =
    value && typeof value === 'object' && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {}
  return {
    name: asString(entry.name),
    url: asString(entry.url),
    events: asStringList(entry.events),
    headers: asMap<string>(entry.headers),
    timeout: asNumber(entry.timeout),
    retry_count: asNumber(entry.retry_count),
    retry_delay: asNumber(entry.retry_delay),
    can_block: entry.can_block === true,
    // The daemon default is True; only an explicit stored `false` disables it.
    enabled: entry.enabled !== false,
  }
}

/** A blank endpoint mirroring the config model's field defaults. */
function createEndpoint(): WebhookEndpoint {
  return {
    name: '',
    url: '',
    events: [],
    headers: {},
    timeout: 10,
    retry_count: 3,
    retry_delay: 1,
    can_block: false,
    enabled: true,
  }
}

function CommunicationsGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Communications"
      hint="Inbound/outbound messaging channels and the base URL channels call back to."
    >
      <SwitchConfigField
        fields={fields}
        path="communications.enabled"
        label="Enable communications"
        ariaLabel="Enable communications"
      />
      <TextConfigField
        fields={fields}
        path="communications.webhook_base_url"
        label="Webhook base URL"
        ariaLabel="Communications webhook base URL"
        placeholder="https://your-host/communications"
      />
      <SwitchConfigField
        fields={fields}
        path="communications.inbound_enabled"
        label="Accept inbound messages"
        ariaLabel="Enable inbound communications"
      />
      <SwitchConfigField
        fields={fields}
        path="communications.outbound_enabled"
        label="Send outbound messages"
        ariaLabel="Enable outbound communications"
      />
      <SwitchConfigField
        fields={fields}
        path="communications.auto_create_sessions"
        label="Auto-create sessions for inbound messages"
        ariaLabel="Auto-create sessions for inbound messages"
      />
    </Subsection>
  )
}

function ChannelDefaultsGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Channel defaults"
      hint="Rate limiting, retry, and retention applied to communication channels unless overridden."
    >
      <NumberConfigField
        fields={fields}
        path="communications.channel_defaults.rate_limit_per_minute"
        label="Rate limit per minute"
        ariaLabel="Channel rate limit per minute"
      />
      <NumberConfigField
        fields={fields}
        path="communications.channel_defaults.burst"
        label="Burst allowance"
        ariaLabel="Channel burst allowance"
      />
      <NumberConfigField
        fields={fields}
        path="communications.channel_defaults.retry_count"
        label="Retry count"
        ariaLabel="Channel retry count"
      />
      <NumberConfigField
        fields={fields}
        path="communications.channel_defaults.poll_interval_seconds"
        label="Poll interval (seconds)"
        ariaLabel="Channel poll interval (seconds)"
      />
      <NumberConfigField
        fields={fields}
        path="communications.channel_defaults.retention_days"
        label="Message retention (days)"
        ariaLabel="Channel message retention (days)"
      />
    </Subsection>
  )
}

function WebSocketBroadcastGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="WebSocket broadcasting"
      hint="Stream hook events to connected WebSocket clients."
    >
      <SwitchConfigField
        fields={fields}
        path="hook_extensions.websocket.enabled"
        label="Enable WebSocket broadcasting"
        ariaLabel="Enable WebSocket broadcasting"
      />
      <StringListConfigField
        fields={fields}
        path="hook_extensions.websocket.broadcast_events"
        label="Broadcast events"
        ariaLabel="Broadcast event"
        addLabel="Add broadcast event"
        placeholder="post-tool-use"
      />
      <SwitchConfigField
        fields={fields}
        path="hook_extensions.websocket.include_payload"
        label="Include event payload in broadcasts"
        ariaLabel="Include event payload in broadcasts"
      />
    </Subsection>
  )
}

function WebhookEndpointFields({
  endpoint,
  index,
  onChange,
}: {
  endpoint: WebhookEndpoint
  index: number
  onChange: (next: WebhookEndpoint) => void
}) {
  const name = endpoint.name || `endpoint ${index + 1}`
  return (
    <>
      <TextField
        label="Name"
        ariaLabel={`${name} name`}
        value={endpoint.name}
        placeholder="unique-name"
        onChange={(value) => onChange({ ...endpoint, name: value })}
      />
      <TextField
        label="URL"
        ariaLabel={`${name} URL`}
        value={endpoint.url}
        placeholder="https://example.com/hook"
        onChange={(value) => onChange({ ...endpoint, url: value })}
      />
      <StringListField
        label="Events"
        ariaLabel={`${name} events`}
        value={endpoint.events}
        addLabel="Add event"
        placeholder="post-tool-use (empty = all events)"
        onChange={(value) => onChange({ ...endpoint, events: value })}
      />
      <KeyValueMapField
        label="Headers"
        ariaLabel={`${name} headers`}
        value={endpoint.headers}
        keyPlaceholder="Header name"
        addLabel="Add header"
        onChange={(value) => onChange({ ...endpoint, headers: value })}
      />
      <NumberField
        label="Timeout (seconds)"
        ariaLabel={`${name} timeout (seconds)`}
        value={endpoint.timeout}
        min={1}
        max={60}
        onChange={(value) => onChange({ ...endpoint, timeout: value })}
      />
      <NumberField
        label="Retry count"
        ariaLabel={`${name} retry count`}
        value={endpoint.retry_count}
        min={0}
        max={10}
        onChange={(value) => onChange({ ...endpoint, retry_count: value })}
      />
      <NumberField
        label="Retry delay (seconds)"
        ariaLabel={`${name} retry delay (seconds)`}
        value={endpoint.retry_delay}
        min={0.1}
        max={30}
        step={0.1}
        onChange={(value) => onChange({ ...endpoint, retry_delay: value })}
      />
      <SwitchField
        label="Can block the action"
        ariaLabel={`${name} can block the action`}
        value={endpoint.can_block}
        onChange={(value) => onChange({ ...endpoint, can_block: value })}
      />
      <SwitchField
        label="Enabled"
        ariaLabel={`${name} enabled`}
        value={endpoint.enabled}
        onChange={(value) => onChange({ ...endpoint, enabled: value })}
      />
    </>
  )
}

function WebhooksGroup({ fields }: { fields: SettingsSectionFields }) {
  const endpoints = asTypedList<unknown>(
    fields.getValue(WEBHOOK_ENDPOINTS_PATH),
  ).map(asEndpoint)
  return (
    <Subsection
      title="Webhooks"
      hint="Dispatch hook events to external HTTP endpoints."
    >
      <SwitchConfigField
        fields={fields}
        path="hook_extensions.webhooks.enabled"
        label="Enable webhook dispatch"
        ariaLabel="Enable webhook dispatch"
      />
      <TypedListField<WebhookEndpoint>
        label="Endpoints"
        ariaLabel="Webhook endpoint"
        addLabel="Add webhook endpoint"
        value={endpoints}
        createItem={createEndpoint}
        itemLabel={(item, index) =>
          item.name || item.url || `Endpoint ${index + 1}`
        }
        onChange={(value) => fields.setValue(WEBHOOK_ENDPOINTS_PATH, value)}
        renderItem={(item, onItemChange, index) => (
          <WebhookEndpointFields
            endpoint={item}
            index={index}
            onChange={onItemChange}
          />
        )}
      />
      <NumberConfigField
        fields={fields}
        path="hook_extensions.webhooks.default_timeout"
        label="Default webhook timeout (seconds)"
        ariaLabel="Default webhook timeout (seconds)"
        step={0.5}
      />
      <SwitchConfigField
        fields={fields}
        path="hook_extensions.webhooks.async_dispatch"
        label="Dispatch webhooks asynchronously"
        ariaLabel="Dispatch webhooks asynchronously"
      />
    </Subsection>
  )
}

/**
 * Integrations & Hooks: the communications framework plus the hook-extension
 * broadcasting and webhook surfaces, all backed by DaemonConfig rows and saved
 * through the section's draft Save/Discard footer.
 */
export function IntegrationsHooksSection() {
  return (
    <SettingsSection sectionId="integrations-hooks" ownedPaths={OWNED_PATHS}>
      {(fields) => (
        <>
          <CommunicationsGroup fields={fields} />
          <ChannelDefaultsGroup fields={fields} />
          <WebSocketBroadcastGroup fields={fields} />
          <WebhooksGroup fields={fields} />
        </>
      )}
    </SettingsSection>
  )
}
