//! Name-based dispatch for command and reply frames.
//!
//! A command names its variant in `command` and a reply names it in `ok`.
//! Dispatching on those fields lets the concrete frame struct decide, so each
//! rejection carries that struct's own reason instead of an untagged-enum
//! summary.

use serde::Deserialize;
use serde::de::{self, Deserializer};
use serde_json::Value;

use super::checkout_read::CheckoutReadCommand;
use super::{
    CancelCommand, CommandFrame, ErrReply, MergeCommand, OkReply, RemoveCommand, ReplyFrame,
    SpawnCommand, StatusCommand, WriteCommand,
};

impl<'de> Deserialize<'de> for CommandFrame {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = Value::deserialize(deserializer)?;
        command_frame(&value).map_err(de::Error::custom)
    }
}

impl<'de> Deserialize<'de> for ReplyFrame {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = Value::deserialize(deserializer)?;
        reply_frame(&value).map_err(de::Error::custom)
    }
}

fn command_frame(value: &Value) -> Result<CommandFrame, serde_json::Error> {
    let name = match value.get("command").map(Value::as_str) {
        Some(Some(name)) => name,
        Some(None) => return Err(de::Error::custom("command must be a string")),
        None => return Err(de::Error::missing_field("command")),
    };
    Ok(match name {
        "agent.spawn" => CommandFrame::Spawn(Box::new(SpawnCommand::deserialize(value)?)),
        "agent.cancel" => CommandFrame::Cancel(CancelCommand::deserialize(value)?),
        "terminal.write" => CommandFrame::Write(WriteCommand::deserialize(value)?),
        "workspace.merge" => CommandFrame::Merge(MergeCommand::deserialize(value)?),
        "workspace.remove" => CommandFrame::Remove(RemoveCommand::deserialize(value)?),
        "checkout.status" => CommandFrame::Status(StatusCommand::deserialize(value)?),
        "checkout.read" => CommandFrame::Read(CheckoutReadCommand::deserialize(value)?),
        other => return Err(de::Error::custom(format!("unknown command `{other}`"))),
    })
}

fn reply_frame(value: &Value) -> Result<ReplyFrame, serde_json::Error> {
    let ok = match value.get("ok").map(Value::as_bool) {
        Some(Some(ok)) => ok,
        Some(None) => return Err(de::Error::custom("ok must be a boolean")),
        None => return Err(de::Error::missing_field("ok")),
    };
    if ok {
        OkReply::deserialize(value).map(ReplyFrame::Ok)
    } else {
        ErrReply::deserialize(value).map(ReplyFrame::Err)
    }
}
