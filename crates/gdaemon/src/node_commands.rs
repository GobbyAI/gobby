//! Hub-node command frames, payloads, results, and node bodies.
//!
//! Field order is the wire order in `docs/contracts/node-commands.md`.
//! Unknown fields fail deserialization. Re-encoding a corpus message is
//! byte-identical to the stored JSON text.

use std::collections::BTreeMap;

use serde::de::{self, Deserializer};
use serde::{Deserialize, Serialize, Serializer};
use serde_json::value::RawValue;

use checkout_read::{CheckoutReadCommand, CheckoutReadResult};

/// The contract version every frame and node body carries as `v`.
pub const CONTRACT_VERSION: u64 = 4;

const LIST_LIMIT: usize = 1000;
const EVENT_LIMIT: usize = 100;
const VIOLATION_COUNT_LIMIT: u64 = 10_000;

macro_rules! literal_string {
    ($name:ident, $text:literal) => {
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub struct $name;

        impl Serialize for $name {
            fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
                serializer.serialize_str($text)
            }
        }

        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
                let value = String::deserialize(deserializer)?;
                if value == $text {
                    Ok(Self)
                } else {
                    Err(de::Error::custom(concat!("expected ", $text)))
                }
            }
        }
    };
}

literal_string!(CommandTag, "command");
literal_string!(ReplyTag, "reply");
literal_string!(SpawnCommandName, "agent.spawn");
literal_string!(CancelCommandName, "agent.cancel");
literal_string!(WriteCommandName, "terminal.write");
literal_string!(WorkspaceMergeName, "workspace.merge");
literal_string!(WorkspaceRemoveName, "workspace.remove");
literal_string!(CheckoutStatusName, "checkout.status");
literal_string!(StaleReportCode, "stale_report");

mod checkout_read;
mod frame_dispatch;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TrueFlag;

impl Serialize for TrueFlag {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_bool(true)
    }
}

impl<'de> Deserialize<'de> for TrueFlag {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        if bool::deserialize(deserializer)? {
            Ok(Self)
        } else {
            Err(de::Error::custom("expected true"))
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FalseFlag;

impl Serialize for FalseFlag {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_bool(false)
    }
}

impl<'de> Deserialize<'de> for FalseFlag {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        if bool::deserialize(deserializer)? {
            Err(de::Error::custom("expected false"))
        } else {
            Ok(Self)
        }
    }
}

fn positive_u64<'de, D>(deserializer: D) -> Result<u64, D::Error>
where
    D: Deserializer<'de>,
{
    let value = u64::deserialize(deserializer)?;
    if value == 0 {
        return Err(de::Error::custom("value must be >= 1"));
    }
    Ok(value)
}

fn at_most_1000<'de, D, T>(deserializer: D) -> Result<Vec<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    let items = Vec::<T>::deserialize(deserializer)?;
    if items.len() > LIST_LIMIT {
        return Err(de::Error::custom("at most 1000 entries"));
    }
    Ok(items)
}

fn at_most_100_events<'de, D>(deserializer: D) -> Result<Option<Vec<ViolationEvent>>, D::Error>
where
    D: Deserializer<'de>,
{
    let events = Option::<Vec<ViolationEvent>>::deserialize(deserializer)?;
    if events
        .as_ref()
        .is_some_and(|events| events.len() > EVENT_LIMIT)
    {
        return Err(de::Error::custom("at most 100 events"));
    }
    Ok(events)
}

fn violation_count<'de, D>(deserializer: D) -> Result<u64, D::Error>
where
    D: Deserializer<'de>,
{
    let value = u64::deserialize(deserializer)?;
    if value > VIOLATION_COUNT_LIMIT {
        return Err(de::Error::custom("violation_count is at most 10000"));
    }
    Ok(value)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Stage {
    None,
    Partial,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum SandboxBackend {
    Srt,
    ProviderNative,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SandboxMode {
    Permissive,
    Restrictive,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SpawnMode {
    None,
    Worktree,
    Clone,
    Existing,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DroidMode {
    Exec,
    Interactive,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReportMode {
    Worktree,
    Clone,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum CancelSignal {
    #[serde(rename = "TERM")]
    Term,
    #[serde(rename = "KILL")]
    Kill,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WriteKind {
    Text,
    Key,
    Input,
    Paste,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExitReason {
    PrepareFailed,
    PrepareTimeout,
    LaunchFailed,
    Cancelled,
    Exited,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ComposerState {
    Empty,
    Draft,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IdleStatus {
    Unknown,
    ContextFull,
    Idle,
    Active,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TerminateCause {
    DoomLoop,
    ProviderStall,
    Timeout,
    InitTimeout,
}

/// The resolved `SandboxConfig` the node launches under.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Sandbox {
    pub enabled: bool,
    pub backend: SandboxBackend,
    pub mode: SandboxMode,
    pub allow_network: bool,
    pub extra_read_paths: Vec<String>,
    pub extra_write_paths: Vec<String>,
    pub extra_deny_read_paths: Vec<String>,
    pub extra_deny_write_paths: Vec<String>,
    pub allowed_domains: Vec<String>,
    pub denied_domains: Vec<String>,
    pub allow_git_network: bool,
    pub allow_package_registries: bool,
    pub allow_unix_sockets: Vec<String>,
}

/// `path` names an `existing` workspace and is null with every other mode.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "SpawnWorkspaceFields")]
pub struct SpawnWorkspace {
    pub mode: SpawnMode,
    pub path: Option<String>,
    pub branch_name: Option<String>,
    pub base_branch: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SpawnWorkspaceFields {
    mode: SpawnMode,
    #[serde(deserialize_with = "Option::deserialize")]
    path: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    branch_name: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    base_branch: Option<String>,
}

impl TryFrom<SpawnWorkspaceFields> for SpawnWorkspace {
    type Error = &'static str;

    fn try_from(fields: SpawnWorkspaceFields) -> Result<Self, Self::Error> {
        if (fields.mode == SpawnMode::Existing) != fields.path.is_some() {
            return Err("path is required with mode existing and null otherwise");
        }
        Ok(Self {
            mode: fields.mode,
            path: fields.path,
            branch_name: fields.branch_name,
            base_branch: fields.base_branch,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SpawnMonitor {
    pub auto_enter_agent_terminals: bool,
    pub auto_enter_agent_interval_seconds: u64,
    pub auto_enter_approval_prompts: bool,
    pub init_timeout_seconds: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SpawnPayload {
    pub run_id: String,
    pub session_id: String,
    #[serde(deserialize_with = "Option::deserialize")]
    pub parent_session_id: Option<String>,
    pub project_id: String,
    pub terminal_id: String,
    pub agent_depth: u64,
    pub max_agent_depth: u64,
    #[serde(deserialize_with = "Option::deserialize")]
    pub workflow_name: Option<String>,
    pub provider: String,
    #[serde(deserialize_with = "Option::deserialize")]
    pub model: Option<String>,
    pub prompt: String,
    pub provider_args: Vec<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub reasoning_effort: Option<String>,
    pub sandbox: Sandbox,
    pub timeout_seconds: u64,
    #[serde(deserialize_with = "Option::deserialize")]
    pub title: Option<String>,
    pub source_root: String,
    pub workspace: SpawnWorkspace,
    #[serde(deserialize_with = "Option::deserialize")]
    pub agent_name: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub api_base: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub api_token: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub codex_oss_provider: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub resume_session_id: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub execution_mode: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub code_index_preflight: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub task_id: Option<String>,
    pub extra_env: BTreeMap<String, String>,
    pub codex_config_overrides: Vec<String>,
    pub droid_mode: DroidMode,
    pub prewarm_pre_commit_store: bool,
    pub monitor: SpawnMonitor,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CancelPayload {
    pub run_id: String,
    pub terminal_id: String,
    pub signal: CancelSignal,
    pub close_terminal: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WritePayload {
    pub terminal_id: String,
    pub kind: WriteKind,
    pub data: String,
    pub submit: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SpawnCommand {
    #[serde(rename = "type")]
    pub frame_type: CommandTag,
    #[serde(deserialize_with = "positive_u64")]
    pub v: u64,
    pub request_id: String,
    pub command: SpawnCommandName,
    pub payload: SpawnPayload,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CancelCommand {
    #[serde(rename = "type")]
    pub frame_type: CommandTag,
    #[serde(deserialize_with = "positive_u64")]
    pub v: u64,
    pub request_id: String,
    pub command: CancelCommandName,
    pub payload: CancelPayload,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WriteCommand {
    #[serde(rename = "type")]
    pub frame_type: CommandTag,
    #[serde(deserialize_with = "positive_u64")]
    pub v: u64,
    pub request_id: String,
    pub command: WriteCommandName,
    pub payload: WritePayload,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkspaceMode {
    Worktree,
    Clone,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MergePayload {
    pub project_id: String,
    pub mode: WorkspaceMode,
    pub path: String,
    pub branch_name: String,
    pub target_branch: String,
    pub source_root: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RemovePayload {
    pub project_id: String,
    pub mode: WorkspaceMode,
    pub path: String,
    pub branch_name: String,
    pub source_root: String,
    pub force: bool,
    pub force_delete_branch: bool,
    #[serde(deserialize_with = "Option::deserialize")]
    pub merged_into: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StatusPayload {
    pub project_id: String,
    pub root_path: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MergeCommand {
    #[serde(rename = "type")]
    pub frame_type: CommandTag,
    #[serde(deserialize_with = "positive_u64")]
    pub v: u64,
    pub request_id: String,
    pub command: WorkspaceMergeName,
    pub payload: MergePayload,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RemoveCommand {
    #[serde(rename = "type")]
    pub frame_type: CommandTag,
    #[serde(deserialize_with = "positive_u64")]
    pub v: u64,
    pub request_id: String,
    pub command: WorkspaceRemoveName,
    pub payload: RemovePayload,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StatusCommand {
    #[serde(rename = "type")]
    pub frame_type: CommandTag,
    #[serde(deserialize_with = "positive_u64")]
    pub v: u64,
    pub request_id: String,
    pub command: CheckoutStatusName,
    pub payload: StatusPayload,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum CommandFrame {
    Spawn(Box<SpawnCommand>),
    Cancel(CancelCommand),
    Write(WriteCommand),
    Merge(MergeCommand),
    Remove(RemoveCommand),
    Status(StatusCommand),
    Read(CheckoutReadCommand),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AcceptedResult {
    pub accepted: TrueFlag,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StoppedResult {
    pub stopped: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EmptyResult {}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MergeOutcome {
    Merged,
    AlreadyMerged,
    Conflict,
    TargetDirty,
    Busy,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RemoveOutcome {
    Removed,
    Dirty,
    Unmerged,
    Busy,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "MergeResultFields")]
pub struct MergeResult {
    pub outcome: MergeOutcome,
    pub merge_sha: Option<String>,
    pub target_head_sha: Option<String>,
    pub conflicted_files: Vec<String>,
    pub dirty_paths: Vec<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MergeResultFields {
    outcome: MergeOutcome,
    #[serde(deserialize_with = "Option::deserialize")]
    merge_sha: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    target_head_sha: Option<String>,
    conflicted_files: Vec<String>,
    dirty_paths: Vec<String>,
}

impl TryFrom<MergeResultFields> for MergeResult {
    type Error = &'static str;

    fn try_from(fields: MergeResultFields) -> Result<Self, Self::Error> {
        let heads = matches!(
            fields.outcome,
            MergeOutcome::Merged | MergeOutcome::AlreadyMerged
        );
        if fields.outcome == MergeOutcome::Merged && fields.merge_sha.is_none() {
            return Err("merge_sha is required with outcome merged");
        }
        if fields.outcome != MergeOutcome::Merged && fields.merge_sha.is_some() {
            return Err("merge_sha is set only with outcome merged");
        }
        if heads && fields.target_head_sha.is_none() {
            return Err("target_head_sha is required with outcome merged or already_merged");
        }
        if !heads && fields.target_head_sha.is_some() {
            return Err("target_head_sha is set only with outcome merged or already_merged");
        }
        if !fields.conflicted_files.is_empty() && fields.outcome != MergeOutcome::Conflict {
            return Err("conflicted_files is set only with outcome conflict");
        }
        if !fields.dirty_paths.is_empty() && fields.outcome != MergeOutcome::TargetDirty {
            return Err("dirty_paths is set only with outcome target_dirty");
        }
        Ok(Self {
            outcome: fields.outcome,
            merge_sha: fields.merge_sha,
            target_head_sha: fields.target_head_sha,
            conflicted_files: fields.conflicted_files,
            dirty_paths: fields.dirty_paths,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RemoveResult {
    pub outcome: RemoveOutcome,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StatusResult {
    #[serde(deserialize_with = "Option::deserialize")]
    pub current_branch: Option<String>,
    pub branch_count: u64,
    #[serde(deserialize_with = "Option::deserialize")]
    pub ahead: Option<u64>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub behind: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum ReplyResult {
    Accepted(AcceptedResult),
    Stopped(StoppedResult),
    Empty(EmptyResult),
    Merge(MergeResult),
    Remove(RemoveResult),
    Status(StatusResult),
    CheckoutRead(CheckoutReadResult),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ErrorBody {
    pub code: String,
    pub message: String,
    #[serde(deserialize_with = "Option::deserialize")]
    pub stage: Option<Stage>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OkReply {
    #[serde(rename = "type")]
    pub frame_type: ReplyTag,
    #[serde(deserialize_with = "positive_u64")]
    pub v: u64,
    pub request_id: String,
    pub ok: TrueFlag,
    pub result: ReplyResult,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ErrReply {
    #[serde(rename = "type")]
    pub frame_type: ReplyTag,
    #[serde(deserialize_with = "positive_u64")]
    pub v: u64,
    pub request_id: String,
    pub ok: FalseFlag,
    pub error: ErrorBody,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum ReplyFrame {
    Ok(OkReply),
    Err(ErrReply),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CheckoutBody {
    #[serde(deserialize_with = "positive_u64")]
    pub v: u64,
    pub project_id: String,
    pub root_path: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CheckoutResult {
    pub root_path: String,
    pub created: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReportWorkspace {
    pub mode: ReportMode,
    pub path: String,
    pub branch_name: String,
    #[serde(deserialize_with = "Option::deserialize")]
    pub base_branch: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AttentionOption {
    pub option: u64,
    pub label: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AttentionPayload {
    pub kind: String,
    pub excerpt: String,
    pub options: Vec<AttentionOption>,
    pub fingerprint: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaneAttention {
    pub reason: String,
    pub kind: String,
    pub fingerprint: String,
    pub payload: AttentionPayload,
    pub dismissed: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaneComposer {
    pub state: ComposerState,
    #[serde(deserialize_with = "Option::deserialize")]
    pub line: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaneQuota {
    pub provider: String,
    #[serde(deserialize_with = "Option::deserialize")]
    pub reset_time: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaneIdle {
    pub status: IdleStatus,
    pub turn_in_flight: bool,
    pub unsubmitted_input: bool,
    pub queued_message_prompt: bool,
    #[serde(deserialize_with = "Option::deserialize")]
    pub capacity_message: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub quota: Option<PaneQuota>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReportPane {
    #[serde(deserialize_with = "Option::deserialize")]
    pub attention: Option<PaneAttention>,
    pub injections: u64,
    pub composer: PaneComposer,
    #[serde(deserialize_with = "Option::deserialize")]
    pub idle: Option<PaneIdle>,
    pub tail: String,
}

/// One decoded violation-log event, kept as its JSON text so re-encoding
/// preserves its key order.
#[derive(Debug, Clone, Serialize)]
#[serde(transparent)]
pub struct ViolationEvent(Box<RawValue>);

impl PartialEq for ViolationEvent {
    fn eq(&self, other: &Self) -> bool {
        self.0.get() == other.0.get()
    }
}

impl Eq for ViolationEvent {}

impl<'de> Deserialize<'de> for ViolationEvent {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = Box::<RawValue>::deserialize(deserializer)?;
        if !raw.get().starts_with('{') {
            return Err(de::Error::custom("a violation event is a JSON object"));
        }
        Ok(Self(raw))
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReportSandbox {
    pub runtime_version: String,
    pub policy_schema_version: u64,
    pub policy_hash: String,
    #[serde(deserialize_with = "violation_count")]
    pub violation_count: u64,
    pub violation_count_truncated: bool,
    pub denial_operations: BTreeMap<String, u64>,
    #[serde(deserialize_with = "at_most_100_events")]
    pub recent_violations: Option<Vec<ViolationEvent>>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReportAgent {
    pub run_id: String,
    pub terminal_id: String,
    pub pid: u64,
    pub locator: String,
    pub locator_key: String,
    #[serde(deserialize_with = "Option::deserialize")]
    pub workspace: Option<ReportWorkspace>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub pane: Option<ReportPane>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub sandbox: Option<ReportSandbox>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReportExit {
    pub run_id: String,
    pub terminal_id: String,
    pub launched: bool,
    #[serde(deserialize_with = "Option::deserialize")]
    pub exit_code: Option<i64>,
    pub reason: ExitReason,
    #[serde(deserialize_with = "Option::deserialize")]
    pub workspace: Option<ReportWorkspace>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub sandbox: Option<ReportSandbox>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReportBody {
    #[serde(deserialize_with = "positive_u64")]
    pub v: u64,
    #[serde(deserialize_with = "Option::deserialize")]
    pub hub_epoch: Option<u64>,
    #[serde(deserialize_with = "positive_u64")]
    pub revision: u64,
    pub host_epoch: u64,
    #[serde(deserialize_with = "at_most_1000")]
    pub accepted: Vec<String>,
    #[serde(deserialize_with = "at_most_1000")]
    pub agents: Vec<ReportAgent>,
    #[serde(deserialize_with = "at_most_1000")]
    pub exits: Vec<ReportExit>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StaleReport {
    pub code: StaleReportCode,
    pub hub_epoch: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PromptDeliveryFailureBody {
    #[serde(deserialize_with = "positive_u64")]
    pub v: u64,
    pub run_id: String,
    pub code: String,
    pub detail: String,
    #[serde(deserialize_with = "Option::deserialize")]
    pub pane: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PromptDeliveryFailureResult {
    pub failed: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CodeIndexWarning {
    pub preflight: String,
    pub cwd: String,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CodeIndexWarningBody {
    #[serde(deserialize_with = "positive_u64")]
    pub v: u64,
    pub run_id: String,
    pub warning: CodeIndexWarning,
    pub prompt: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CodeIndexWarningResult {
    pub prompt: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TerminateCheckpoint {
    pub ref_name: String,
    pub commit_sha: String,
    pub parent_sha: String,
    pub files_changed: u64,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TerminateBody {
    #[serde(deserialize_with = "positive_u64")]
    pub v: u64,
    pub run_id: String,
    pub cause: TerminateCause,
    pub detail: String,
    pub pane_tail: String,
    #[serde(deserialize_with = "Option::deserialize")]
    pub checkpoint: Option<TerminateCheckpoint>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TerminateResult {
    pub terminated: bool,
    pub retry: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DetectionManifestEntry {
    pub provider_id: String,
    pub content: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DetectionManifestsResult {
    pub manifests: Vec<DetectionManifestEntry>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
enum WireMessage {
    Command(Box<CommandFrame>),
    Reply(ReplyFrame),
    Checkout(CheckoutBody),
    CheckoutResult(CheckoutResult),
    MergeResult(MergeResult),
    Report(ReportBody),
    Stale(StaleReport),
    PromptDeliveryFailure(PromptDeliveryFailureBody),
    PromptDeliveryFailureResult(PromptDeliveryFailureResult),
    CodeIndexWarning(CodeIndexWarningBody),
    CodeIndexWarningResult(CodeIndexWarningResult),
    Terminate(TerminateBody),
    TerminateResult(TerminateResult),
    DetectionManifests(DetectionManifestsResult),
}

fn decode(kind: &str, raw: &str) -> Result<WireMessage, serde_json::Error> {
    Ok(match kind {
        "command" => WireMessage::Command(Box::new(serde_json::from_str(raw)?)),
        "reply" => WireMessage::Reply(serde_json::from_str(raw)?),
        "checkout" => WireMessage::Checkout(serde_json::from_str(raw)?),
        "checkout_result" => WireMessage::CheckoutResult(serde_json::from_str(raw)?),
        "merge_result" => WireMessage::MergeResult(serde_json::from_str(raw)?),
        "report" => WireMessage::Report(serde_json::from_str(raw)?),
        "stale_report" => WireMessage::Stale(serde_json::from_str(raw)?),
        "prompt_delivery_failure" => WireMessage::PromptDeliveryFailure(serde_json::from_str(raw)?),
        "prompt_delivery_failure_result" => {
            WireMessage::PromptDeliveryFailureResult(serde_json::from_str(raw)?)
        }
        "code_index_warning" => WireMessage::CodeIndexWarning(serde_json::from_str(raw)?),
        "code_index_warning_result" => {
            WireMessage::CodeIndexWarningResult(serde_json::from_str(raw)?)
        }
        "terminate" => WireMessage::Terminate(serde_json::from_str(raw)?),
        "terminate_result" => WireMessage::TerminateResult(serde_json::from_str(raw)?),
        "detection_manifests" => WireMessage::DetectionManifests(serde_json::from_str(raw)?),
        _ => return Err(de::Error::custom("unknown node-command kind")),
    })
}

/// Parse `raw` as `kind` and encode it again with compact JSON.
pub fn round_trip_json(kind: &str, raw: &str) -> Result<String, serde_json::Error> {
    serde_json::to_string(&decode(kind, raw)?)
}
