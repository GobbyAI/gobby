//! Hub-node command frames, payloads, results, and node bodies.
//!
//! Field order is the wire order in `docs/contracts/node-commands.md`.
//! Unknown fields fail deserialization. Re-encoding a corpus message is
//! byte-identical to the stored JSON text.

use serde::de::{self, Deserializer};
use serde::{Deserialize, Serialize, Serializer};

const LIST_LIMIT: usize = 1000;

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
literal_string!(StaleReportCode, "stale_report");

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

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Stage {
    None,
    Partial,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SandboxNetwork {
    None,
    Trusted,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SpawnMode {
    None,
    Worktree,
    Clone,
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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Sandbox {
    pub enabled: bool,
    pub network: SandboxNetwork,
    pub extra_write_paths: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SpawnWorkspace {
    pub mode: SpawnMode,
    pub branch_name: Option<String>,
    pub base_branch: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SpawnPayload {
    pub run_id: String,
    pub session_id: String,
    pub parent_session_id: Option<String>,
    pub project_id: String,
    pub terminal_id: String,
    pub agent_depth: u64,
    pub max_agent_depth: u64,
    pub workflow_name: Option<String>,
    pub provider: String,
    pub model: Option<String>,
    pub prompt: String,
    pub provider_args: Vec<String>,
    pub reasoning_effort: Option<String>,
    pub sandbox: Sandbox,
    pub timeout_seconds: u64,
    pub title: Option<String>,
    pub source_root: String,
    pub workspace: SpawnWorkspace,
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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum CommandFrame {
    Spawn(Box<SpawnCommand>),
    Cancel(CancelCommand),
    Write(WriteCommand),
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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum ReplyResult {
    Accepted(AcceptedResult),
    Stopped(StoppedResult),
    Empty(EmptyResult),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ErrorBody {
    pub code: String,
    pub message: String,
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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
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
    pub base_branch: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReportAgent {
    pub run_id: String,
    pub terminal_id: String,
    pub pid: u64,
    pub locator: String,
    pub locator_key: String,
    pub workspace: Option<ReportWorkspace>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReportExit {
    pub run_id: String,
    pub terminal_id: String,
    pub launched: bool,
    pub exit_code: Option<i64>,
    pub reason: ExitReason,
    pub workspace: Option<ReportWorkspace>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReportBody {
    #[serde(deserialize_with = "positive_u64")]
    pub v: u64,
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
#[serde(untagged)]
enum WireMessage {
    Command(Box<CommandFrame>),
    Reply(ReplyFrame),
    Checkout(CheckoutBody),
    CheckoutResult(CheckoutResult),
    Report(ReportBody),
    Stale(StaleReport),
}

fn decode(kind: &str, raw: &str) -> Result<WireMessage, serde_json::Error> {
    match kind {
        "command" => Ok(WireMessage::Command(Box::new(serde_json::from_str(raw)?))),
        "reply" => Ok(WireMessage::Reply(serde_json::from_str(raw)?)),
        "checkout" => Ok(WireMessage::Checkout(serde_json::from_str(raw)?)),
        "checkout_result" => Ok(WireMessage::CheckoutResult(serde_json::from_str(raw)?)),
        "report" => Ok(WireMessage::Report(serde_json::from_str(raw)?)),
        "stale_report" => Ok(WireMessage::Stale(serde_json::from_str(raw)?)),
        _ => Err(de::Error::custom("unknown node-command kind")),
    }
}

/// Parse `raw` as `kind` and encode it again with compact JSON.
pub fn round_trip_json(kind: &str, raw: &str) -> Result<String, serde_json::Error> {
    serde_json::to_string(&decode(kind, raw)?)
}
