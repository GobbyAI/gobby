//! Version 4 `checkout.read`: a read-only query of a node checkout.
//!
//! The payload is a tagged query. Its reply carries every nullable field
//! explicitly, and every error answers with `stage: "none"`.

use serde::de::{self, Deserializer};
use serde::{Deserialize, Serialize, Serializer};

use super::{CommandTag, positive_u64};

literal_string!(CheckoutReadName, "checkout.read");

/// The reply budget: the most `data` or git `stdout` bytes a reply carries.
const REPLY_BUDGET: u64 = 8 * 1024 * 1024;
/// The longest git command `timeout_ms` the node accepts.
const GIT_TIMEOUT_LIMIT: u64 = 600_000;

fn contract_version<'de, D>(deserializer: D) -> Result<u64, D::Error>
where
    D: Deserializer<'de>,
{
    let value = u64::deserialize(deserializer)?;
    if value != super::CONTRACT_VERSION {
        return Err(de::Error::custom("v must equal CONTRACT_VERSION"));
    }
    Ok(value)
}

fn read_limit<'de, D>(deserializer: D) -> Result<Option<u64>, D::Error>
where
    D: Deserializer<'de>,
{
    let value = Option::<u64>::deserialize(deserializer)?;
    if value.is_some_and(|limit| limit > REPLY_BUDGET) {
        return Err(de::Error::custom("read_limit must be at most 8388608"));
    }
    Ok(value)
}

fn git_timeout<'de, D>(deserializer: D) -> Result<u64, D::Error>
where
    D: Deserializer<'de>,
{
    let value = positive_u64(deserializer)?;
    if value > GIT_TIMEOUT_LIMIT {
        return Err(de::Error::custom("timeout_ms must be at most 600000"));
    }
    Ok(value)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum CheckoutReadQuery {
    Files(FilesQuery),
    Git(GitQuery),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FilesQuery {
    #[serde(deserialize_with = "super::at_most_1000")]
    pub paths: Vec<String>,
    #[serde(deserialize_with = "read_limit")]
    pub read_limit: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GitQuery {
    pub args: Vec<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub stdin: Option<String>,
    #[serde(deserialize_with = "git_timeout")]
    pub timeout_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CheckoutReadPayload {
    pub cwd: String,
    pub query: CheckoutReadQuery,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CheckoutReadCommand {
    #[serde(rename = "type")]
    pub frame_type: CommandTag,
    #[serde(deserialize_with = "contract_version")]
    pub v: u64,
    pub request_id: String,
    pub command: CheckoutReadName,
    pub payload: CheckoutReadPayload,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum EntryKind {
    File,
    Dir,
    Other,
    Missing,
    Outside,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FileEntry {
    pub path: String,
    pub resolved: String,
    pub kind: EntryKind,
    #[serde(deserialize_with = "Option::deserialize")]
    pub checkout_root: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub git_common_dir: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub project_root: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub project_id: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub data: Option<String>,
    pub truncated: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GitResult {
    #[serde(deserialize_with = "Option::deserialize")]
    pub exit_code: Option<i64>,
    pub timed_out: bool,
    pub stdout: String,
    pub stderr: String,
    pub truncated: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CheckoutReadResult {
    pub root: String,
    pub project_id: String,
    pub home: String,
    pub temp_dir: String,
    #[serde(deserialize_with = "Option::deserialize")]
    pub files: Option<Vec<FileEntry>>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub git: Option<GitResult>,
}
