import { NumberField, SwitchField, TextField } from "../../activity/fields";
import { BoundedSelectField, TypedListField } from "../fields";
import { CHAT_MODES, type ChatMode } from "../../../types/chat";
import type {
  UseSettingsReturn,
  VoiceInputMode,
} from "../../../hooks/useSettings";
import { SettingsSection, type SettingsSectionFields } from "./SettingsSection";
import { useSettingsSectionContext } from "./SettingsSectionContext";
import { asTypedList } from "./configAccessors";
import {
  NumberConfigField,
  OptionsSelectField,
  SchemaSelectField,
  StringListConfigField,
  Subsection,
  SwitchConfigField,
  TextConfigField,
} from "./configFields";

// Client default-mode options reuse the chat mode catalog the chat header uses,
// so the overlay and the in-chat selector stay in lockstep (Plan/Act/YOLO).
const CLIENT_CHAT_MODE_OPTIONS = CHAT_MODES.map((mode) => ({
  value: mode.id,
  label: mode.label,
}));

const VOICE_INPUT_MODE_OPTIONS = [
  { value: "ptt", label: "Push-to-talk" },
  { value: "vad", label: "Voice activity (auto)" },
];

// The daemon's chat.default_mode is bounded to these four by a field validator
// but typed as a plain string, so the option list is supplied explicitly.
const CHAT_DEFAULT_MODE_OPTIONS = [
  { value: "normal", label: "Normal" },
  { value: "accept_edits", label: "Accept edits" },
  { value: "bypass", label: "Bypass" },
  { value: "plan", label: "Plan" },
];

// Voice provider/device/whisper fields are likewise bounded-by-docstring plain
// strings in the schema; their allowed values come from VoiceConfig.
const TTS_PROVIDER_OPTIONS = [{ value: "chatterbox", label: "Chatterbox" }];
const TTS_DEVICE_OPTIONS = [
  { value: "auto", label: "Auto" },
  { value: "cuda", label: "CUDA" },
  { value: "mps", label: "Metal (MPS)" },
  { value: "cpu", label: "CPU" },
];
const WHISPER_MODEL_SIZE_OPTIONS = [
  { value: "tiny", label: "Tiny" },
  { value: "base", label: "Base" },
  { value: "small", label: "Small" },
  { value: "medium", label: "Medium" },
];
const WHISPER_DEVICE_OPTIONS = [
  { value: "auto", label: "Auto" },
  { value: "cpu", label: "CPU" },
  { value: "cuda", label: "CUDA" },
];
const WHISPER_COMPUTE_TYPE_OPTIONS = [
  { value: "int8", label: "int8" },
  { value: "float16", label: "float16" },
  { value: "float32", label: "float32" },
];

const CHAT_PATHS = [
  "chat.profile",
  "chat.default_mode",
  "chat.candidates",
  "chat.attachment_max_file_bytes",
  "chat.attachment_max_total_bytes_per_message",
  "chat.attachment_max_files_per_message",
  "chat.attachment_unbound_retention_hours",
  "chat.attachment_gc_interval_minutes",
];

const VOICE_PATHS = [
  "voice.enabled",
  "voice.tts_enabled",
  "voice.tts_provider",
  "voice.tts_reference_audio",
  "voice.tts_reference_text",
  "voice.tts_temperature",
  "voice.tts_chatterbox_max_generation_tokens",
  "voice.tts_clause_max_chars",
  "voice.tts_device",
  "voice.stt_enabled",
  "voice.transcription_timeout_seconds",
  "voice.whisper_model_size",
  "voice.whisper_device",
  "voice.whisper_compute_type",
  "voice.whisper_prompt",
  "voice.whisper_vocabulary",
  "voice.openai_compatible_audio",
];

const OWNED_PATHS: readonly string[] = [...CHAT_PATHS, ...VOICE_PATHS];
const AUDIO_BINDINGS_PATH = "voice.openai_compatible_audio";

// One OpenAI-compatible audio binding (VoiceConfig.openai_compatible_audio item).
interface AudioBinding {
  provider?: string;
  url?: string;
  model?: string;
  api_key?: string | null;
  transcription_enabled?: boolean;
  translation_enabled?: boolean;
  timeout_seconds?: number;
}

/**
 * Per-browser preferences owned by `useSettings`. These apply immediately (and
 * now persist server-side) rather than going through the section draft, so they
 * sit in their own subsection above the daemon-config rows.
 */
function ClientPreferences({
  clientSettings,
}: {
  clientSettings?: UseSettingsReturn;
}) {
  if (!clientSettings) {
    return (
      <Subsection title="Voice & chat preferences">
        <p className="rounded-lg border border-border bg-muted px-5 py-4 text-sm leading-[1.5] text-foreground-muted">
          Voice and chat preferences are unavailable — the settings provider is
          not mounted.
        </p>
      </Subsection>
    );
  }

  const { settings } = clientSettings;

  return (
    <Subsection
      title="Voice & chat preferences"
      hint="Applies immediately — your default chat mode and chat-input voice controls."
    >
      <BoundedSelectField
        label="Default chat mode"
        ariaLabel="Default chat mode"
        value={settings.defaultChatMode}
        options={CLIENT_CHAT_MODE_OPTIONS}
        onChange={(value) =>
          clientSettings.updateDefaultChatMode(value as ChatMode)
        }
      />
      <SwitchField
        label="Speech-to-text"
        ariaLabel="Speech-to-text enabled"
        value={settings.sttEnabled}
        onChange={clientSettings.updateSttEnabled}
      />
      <SwitchField
        label="Text-to-speech"
        ariaLabel="Text-to-speech enabled"
        value={settings.ttsEnabled}
        onChange={clientSettings.updateTtsEnabled}
      />
      <BoundedSelectField
        label="Voice input mode"
        ariaLabel="Voice input mode"
        value={settings.voiceInputMode}
        options={VOICE_INPUT_MODE_OPTIONS}
        onChange={(value) =>
          clientSettings.updateVoiceInputMode(value as VoiceInputMode)
        }
      />
    </Subsection>
  );
}

function ChatGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Chat"
      hint="Daemon-side chat defaults and attachment limits."
    >
      <SchemaSelectField
        fields={fields}
        path="chat.profile"
        label="Capability profile"
        ariaLabel="Chat capability profile"
      />
      <OptionsSelectField
        fields={fields}
        path="chat.default_mode"
        label="Daemon default chat mode"
        ariaLabel="Daemon default chat mode"
        options={CHAT_DEFAULT_MODE_OPTIONS}
      />
      <StringListConfigField
        fields={fields}
        path="chat.candidates"
        label="Provider/model candidates"
        ariaLabel="Chat candidates"
        addLabel="Add candidate"
        placeholder="provider/model (e.g. claude/sonnet)"
      />
      <NumberConfigField
        fields={fields}
        path="chat.attachment_max_file_bytes"
        label="Maximum attachment size (bytes)"
        ariaLabel="Maximum attachment size (bytes)"
      />
      <NumberConfigField
        fields={fields}
        path="chat.attachment_max_total_bytes_per_message"
        label="Maximum total attachment bytes per message"
        ariaLabel="Maximum total attachment bytes per message"
      />
      <NumberConfigField
        fields={fields}
        path="chat.attachment_max_files_per_message"
        label="Maximum attachments per message"
        ariaLabel="Maximum attachments per message"
      />
      <NumberConfigField
        fields={fields}
        path="chat.attachment_unbound_retention_hours"
        label="Unbound attachment retention (hours)"
        ariaLabel="Unbound attachment retention (hours)"
      />
      <NumberConfigField
        fields={fields}
        path="chat.attachment_gc_interval_minutes"
        label="Attachment cleanup interval (minutes)"
        ariaLabel="Attachment cleanup interval (minutes)"
      />
    </Subsection>
  );
}

function VoiceGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Voice"
      hint="Master switch for the daemon voice subsystem (STT + TTS)."
    >
      <SwitchConfigField
        fields={fields}
        path="voice.enabled"
        label="Voice features"
        ariaLabel="Voice features enabled"
      />
    </Subsection>
  );
}

function SpeechToTextGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection title="Speech-to-text" hint="Local Whisper transcription.">
      <SwitchConfigField
        fields={fields}
        path="voice.stt_enabled"
        label="Speech-to-text"
        ariaLabel="Daemon speech-to-text enabled"
      />
      <NumberConfigField
        fields={fields}
        path="voice.transcription_timeout_seconds"
        label="Transcription timeout (seconds)"
        ariaLabel="Transcription timeout (seconds)"
      />
      <OptionsSelectField
        fields={fields}
        path="voice.whisper_model_size"
        label="Whisper model size"
        ariaLabel="Whisper model size"
        options={WHISPER_MODEL_SIZE_OPTIONS}
      />
      <OptionsSelectField
        fields={fields}
        path="voice.whisper_device"
        label="Whisper device"
        ariaLabel="Whisper device"
        options={WHISPER_DEVICE_OPTIONS}
      />
      <OptionsSelectField
        fields={fields}
        path="voice.whisper_compute_type"
        label="Whisper compute type"
        ariaLabel="Whisper compute type"
        options={WHISPER_COMPUTE_TYPE_OPTIONS}
      />
      <TextConfigField
        fields={fields}
        path="voice.whisper_prompt"
        label="Whisper bias prompt"
        ariaLabel="Whisper bias prompt"
        placeholder="e.g. Gobby"
      />
      <StringListConfigField
        fields={fields}
        path="voice.whisper_vocabulary"
        label="Whisper vocabulary"
        ariaLabel="Whisper vocabulary"
        addLabel="Add term"
        placeholder="proper noun or technical term"
      />
    </Subsection>
  );
}

function TextToSpeechGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Text-to-speech"
      hint="Spoken responses via the configured TTS provider."
    >
      <SwitchConfigField
        fields={fields}
        path="voice.tts_enabled"
        label="Text-to-speech"
        ariaLabel="Daemon text-to-speech enabled"
      />
      <OptionsSelectField
        fields={fields}
        path="voice.tts_provider"
        label="TTS provider"
        ariaLabel="TTS provider"
        options={TTS_PROVIDER_OPTIONS}
      />
      <OptionsSelectField
        fields={fields}
        path="voice.tts_device"
        label="TTS device"
        ariaLabel="TTS device"
        options={TTS_DEVICE_OPTIONS}
      />
      <TextConfigField
        fields={fields}
        path="voice.tts_reference_audio"
        label="Reference audio path"
        ariaLabel="TTS reference audio path"
        placeholder="~/.gobby/voice/reference.wav"
      />
      <TextConfigField
        fields={fields}
        path="voice.tts_reference_text"
        label="Reference transcript"
        ariaLabel="TTS reference transcript"
        placeholder="optional transcript of the reference audio"
        nullable
      />
      <NumberConfigField
        fields={fields}
        path="voice.tts_temperature"
        label="TTS temperature"
        ariaLabel="TTS temperature"
        step={0.05}
      />
      <NumberConfigField
        fields={fields}
        path="voice.tts_chatterbox_max_generation_tokens"
        label="Max generation tokens"
        ariaLabel="TTS max generation tokens"
      />
      <NumberConfigField
        fields={fields}
        path="voice.tts_clause_max_chars"
        label="Max characters per clause"
        ariaLabel="TTS max characters per clause"
      />
    </Subsection>
  );
}

function AudioBindingEditor({
  value,
  onChange,
}: {
  value: AudioBinding;
  onChange: (next: AudioBinding) => void;
}) {
  const name = value.provider || "new binding";
  const patch = (partial: Partial<AudioBinding>) =>
    onChange({ ...value, ...partial });

  return (
    <div className="flex min-w-0 flex-1 flex-col gap-3">
      <TextField
        label="Provider id"
        ariaLabel={`Audio provider id (${name})`}
        value={value.provider ?? ""}
        placeholder="unique provider id"
        onChange={(provider) => patch({ provider })}
      />
      <TextField
        label="API URL"
        ariaLabel={`Audio API URL (${name})`}
        value={value.url ?? ""}
        placeholder="http://host:port/v1"
        onChange={(url) => patch({ url })}
      />
      <TextField
        label="Model"
        ariaLabel={`Audio model (${name})`}
        value={value.model ?? ""}
        onChange={(model) => patch({ model })}
      />
      <TextField
        label="API key"
        ariaLabel={`Audio API key (${name})`}
        value={value.api_key ?? ""}
        placeholder="$secret:NAME for encrypted storage"
        onChange={(apiKey) => patch({ api_key: apiKey === "" ? null : apiKey })}
      />
      <SwitchField
        label="Transcription"
        ariaLabel={`Transcription enabled (${name})`}
        value={value.transcription_enabled !== false}
        onChange={(transcription_enabled) => patch({ transcription_enabled })}
      />
      <SwitchField
        label="Translation"
        ariaLabel={`Translation enabled (${name})`}
        value={value.translation_enabled !== false}
        onChange={(translation_enabled) => patch({ translation_enabled })}
      />
      <NumberField
        label="Timeout (seconds)"
        ariaLabel={`Audio timeout seconds (${name})`}
        value={
          typeof value.timeout_seconds === "number"
            ? value.timeout_seconds
            : null
        }
        min={0}
        onChange={(timeout_seconds) =>
          patch({ timeout_seconds: timeout_seconds ?? undefined })
        }
      />
    </div>
  );
}

function AudioBindingsGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="OpenAI-compatible audio"
      hint="External transcription/translation endpoints exposed to the audio capability registry."
    >
      <TypedListField<AudioBinding>
        label="Audio bindings"
        ariaLabel="Audio binding"
        value={asTypedList<AudioBinding>(fields.getValue(AUDIO_BINDINGS_PATH))}
        addLabel="Add binding"
        createItem={() => ({
          provider: "",
          url: "",
          model: "",
          transcription_enabled: true,
          translation_enabled: true,
          timeout_seconds: 120,
        })}
        itemLabel={(item, index) => item.provider || `Binding ${index + 1}`}
        onChange={(next) => fields.setValue(AUDIO_BINDINGS_PATH, next)}
        renderItem={(item, onItemChange) => (
          <AudioBindingEditor value={item} onChange={onItemChange} />
        )}
      />
    </Subsection>
  );
}

export function ChatVoiceSection() {
  const { clientSettings } = useSettingsSectionContext();

  return (
    <SettingsSection sectionId="chat-voice" ownedPaths={OWNED_PATHS}>
      {(fields) => (
        <>
          <ClientPreferences clientSettings={clientSettings} />
          <ChatGroup fields={fields} />
          <VoiceGroup fields={fields} />
          <SpeechToTextGroup fields={fields} />
          <TextToSpeechGroup fields={fields} />
          <AudioBindingsGroup fields={fields} />
        </>
      )}
    </SettingsSection>
  );
}
