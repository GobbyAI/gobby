use chrono::{SecondsFormat, Utc};

pub(super) fn now_iso8601() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false)
}
