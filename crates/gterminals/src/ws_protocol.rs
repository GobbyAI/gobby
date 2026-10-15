//! Backend-neutral terminal WebSocket codec.
//!
//! A port of `src/gobby/terminals/ws_protocol.py`: canonical JSON, safe
//! integer counters, `terminal_ws_fragment` wrapping and reassembly, inventory
//! pages, and the queue, paste and write-sequence bounds of the browser and
//! gclient WS contract. Byte parity with Python is pinned by
//! `tests/ws_golden.rs` against the shared `tests/fixtures/terminal_ws_golden`
//! corpus.

use std::collections::{BTreeMap, VecDeque};
use std::time::{Duration, Instant};

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use chrono::{DateTime, Utc};
use serde_json::{Map, Value, json};
use thiserror::Error;
use uuid::Uuid;

use crate::repository::{Backend, Ownership, Terminal};

pub const TERMINAL_WS_SAFE_INTEGER_MAX: u64 = (1 << 53) - 1;
pub const TERMINAL_LIST_DEFAULT_PAGE_SIZE: usize = 100;
pub const TERMINAL_LIST_MAX_PAGE_SIZE: usize = 500;
pub const TERMINAL_LIST_MAX_ENCODED_BYTES: usize = 1024 * 1024;
pub const TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES: usize = 16 * 1024 * 1024;
pub const TERMINAL_WS_FRAGMENT_MAX_SOCKET_REASSEMBLY_BYTES: usize = 64 * 1024 * 1024;
pub const TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES: usize = 2 * 1024 * 1024;
pub const TERMINAL_WS_FRAGMENT_REASSEMBLY_TIMEOUT: Duration = Duration::from_millis(5000);
pub const TERMINAL_WS_FRAME_QUEUE_ENTRIES: usize = 64;
pub const TERMINAL_WS_FRAME_QUEUE_BYTES: usize = 2 * 1024 * 1024;
pub const TERMINAL_WS_FRAME_SEND_TIMEOUT: Duration = Duration::from_secs(5);
pub const TERMINAL_WS_LIFECYCLE_RESERVE_MAX_ENTRIES: usize = 16;
pub const TERMINAL_WS_LIFECYCLE_RESERVE_MAX_BYTES: usize = 64 * 1024;
pub const TERMINAL_WS_LIFECYCLE_SEND_TIMEOUT: Duration = Duration::from_secs(2);
pub const PASTE_MAX_BYTES: usize = 1024 * 1024;
pub const WRITE_SEQ_CAPACITY: usize = 64;

/// Counters that must stay inside JavaScript's safe integer range wherever
/// they appear in a payload.
const SAFE_INTEGER_FIELDS: &[&str] = &[
    "message_seq",
    "lease_generation",
    "client_write_seq",
    "fragment_index",
    "seq",
];

const FRAGMENT_TYPE: &str = "terminal_ws_fragment";
const FRAGMENT_ENCODING: &str = "utf8-b64";

/// Codec failures. Display strings that name a wire code match Python's.
#[derive(Debug, Error)]
pub enum WsProtocolError {
    #[error("safe_integer_overflow")]
    SafeIntegerOverflow,
    #[error("terminal WS messages must not carry mode")]
    ModeForbidden,
    #[error("terminal WS payload must be an object")]
    NotObject,
    #[error("terminal WS event is missing string field {0}")]
    MissingField(&'static str),
    #[error("terminal_page_too_large")]
    PageTooLarge,
    #[error("canonical item cursor required for page truncation")]
    MissingItemCursor,
    #[error("fragment_too_large")]
    FragmentTooLarge,
    #[error("invalid_cursor")]
    InvalidCursor,
    #[error("{0}")]
    Json(#[from] serde_json::Error),
}

fn check_safe_int(value: &Value) -> Result<u64, WsProtocolError> {
    value
        .as_u64()
        .filter(|n| *n <= TERMINAL_WS_SAFE_INTEGER_MAX)
        .ok_or(WsProtocolError::SafeIntegerOverflow)
}

fn walk_safe_ints(payload: &Value) -> Result<(), WsProtocolError> {
    match payload {
        Value::Object(map) => map.iter().try_for_each(|(key, value)| {
            if SAFE_INTEGER_FIELDS.contains(&key.as_str()) {
                check_safe_int(value).map(drop)
            } else {
                walk_safe_ints(value)
            }
        }),
        Value::Array(items) => items.iter().try_for_each(walk_safe_ints),
        _ => Ok(()),
    }
}

/// Key-sorted copy, so canonical output does not depend on serde_json's map
/// ordering feature.
fn sort_value(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut keys = map.keys().collect::<Vec<_>>();
            keys.sort();
            Value::Object(
                keys.into_iter()
                    .map(|key| (key.clone(), sort_value(&map[key])))
                    .collect(),
            )
        }
        Value::Array(items) => Value::Array(items.iter().map(sort_value).collect()),
        other => other.clone(),
    }
}

/// Compact, key-sorted, non-ASCII-preserving JSON without a trailing newline.
pub fn canonical_json(message: &Value) -> Result<Vec<u8>, WsProtocolError> {
    let map = message.as_object().ok_or(WsProtocolError::NotObject)?;
    if map.contains_key("mode") {
        return Err(WsProtocolError::ModeForbidden);
    }
    walk_safe_ints(message)?;
    Ok(serde_json::to_vec(&sort_value(message))?)
}

/// Canonical JSON plus the newline frame terminator.
pub fn encode_message(message: &Value) -> Result<Vec<u8>, WsProtocolError> {
    let mut encoded = canonical_json(message)?;
    encoded.push(b'\n');
    Ok(encoded)
}

pub fn decode_message(raw: &[u8]) -> Result<Value, WsProtocolError> {
    let parsed: Value = serde_json::from_slice(raw)?;
    if !parsed.is_object() {
        return Err(WsProtocolError::NotObject);
    }
    walk_safe_ints(&parsed)?;
    Ok(parsed)
}

/// The fields every fragment of one wrapped message shares.
struct FragmentHeader<'a> {
    event: &'a str,
    terminal_id: &'a str,
    attachment_id: &'a str,
    message_seq: u64,
}

impl FragmentHeader<'_> {
    fn fragment(&self, index: usize, more: bool, chunk: &[u8]) -> Value {
        json!({
            "type": FRAGMENT_TYPE,
            "event": self.event,
            "terminal_id": self.terminal_id,
            "attachment_id": self.attachment_id,
            "message_seq": self.message_seq,
            "fragment_index": index,
            "more": more,
            "encoding": FRAGMENT_ENCODING,
            "payload": STANDARD.encode(chunk),
        })
    }

    /// Canonical length of the wrapped fragment, computed without encoding the
    /// chunk: base64 is ASCII and needs no JSON escaping.
    fn wrapped_len(
        &self,
        index: usize,
        more: bool,
        chunk_len: usize,
    ) -> Result<usize, WsProtocolError> {
        Ok(canonical_json(&self.fragment(index, more, &[]))?.len() + chunk_len.div_ceil(3) * 4)
    }

    /// Python's `_greedy_chunks`: the largest prefix that fits, halving on
    /// misses, refusing once a probe would shrink to a single byte.
    fn greedy_chunks<'b>(&self, complete: &'b [u8]) -> Result<Vec<&'b [u8]>, WsProtocolError> {
        let mut chunks = Vec::new();
        let mut offset = 0;
        while offset < complete.len() {
            let mut take = complete.len() - offset;
            loop {
                let more = offset + take < complete.len();
                if self.wrapped_len(chunks.len(), more, take)?
                    < TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES
                {
                    chunks.push(&complete[offset..offset + take]);
                    offset += take;
                    break;
                }
                take = (take / 2).max(1);
                if take == 1 {
                    return Err(WsProtocolError::FragmentTooLarge);
                }
            }
        }
        Ok(chunks)
    }
}

/// Split a complete JSON payload into `terminal_ws_fragment` messages: a
/// midpoint split when both halves fit the wrapped limit, greedy otherwise.
pub fn fragment_event(
    event: &str,
    terminal_id: &str,
    attachment_id: &str,
    message_seq: u64,
    complete_json: &[u8],
) -> Result<Vec<Value>, WsProtocolError> {
    check_safe_int(&json!(message_seq))?;
    if complete_json.len() > TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES {
        return Err(WsProtocolError::FragmentTooLarge);
    }
    let header = FragmentHeader {
        event,
        terminal_id,
        attachment_id,
        message_seq,
    };
    let midpoint = (complete_json.len() / 2).max(1).min(complete_json.len());
    let (head, tail) = complete_json.split_at(midpoint);
    let mut slices = vec![head, tail];
    let mut oversized = false;
    for (index, chunk) in slices.iter().enumerate() {
        if !chunk.is_empty()
            && header.wrapped_len(index, index + 1 < slices.len(), chunk.len())?
                >= TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES
        {
            oversized = true;
        }
    }
    if oversized {
        slices = header.greedy_chunks(complete_json)?;
    }
    let last = slices.len() - 1;
    Ok(slices
        .iter()
        .enumerate()
        .filter(|(index, chunk)| *index == 0 || !chunk.is_empty())
        .map(|(index, chunk)| header.fragment(index, index < last, chunk))
        .collect())
}

/// Each wire payload with its canonical bytes; events at or above the wrapped
/// limit are fragmented.
pub fn emit_proxied_event(
    event: &Value,
    message_seq: u64,
) -> Result<Vec<(Value, Vec<u8>)>, WsProtocolError> {
    let raw = canonical_json(event)?;
    if raw.len() > TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES {
        return Err(WsProtocolError::FragmentTooLarge);
    }
    if raw.len() < TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES {
        return Ok(vec![(event.clone(), raw)]);
    }
    let field = |name: &'static str| {
        event[name]
            .as_str()
            .ok_or(WsProtocolError::MissingField(name))
    };
    fragment_event(
        field("type")?,
        field("terminal_id")?,
        field("attachment_id")?,
        message_seq,
        &raw,
    )?
    .into_iter()
    .map(|fragment| {
        let raw = canonical_json(&fragment)?;
        Ok((fragment, raw))
    })
    .collect()
}

/// Why a fragment stream was dropped, with the web reducer's error codes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FragmentError {
    /// Malformed, duplicate, missing, out-of-order or cross-message fragment.
    Sequence,
    /// The assembly would exceed its byte budget.
    TooLarge,
    /// All assemblies together would exceed the socket budget.
    SocketBudget,
    /// The assembly outlived its deadline.
    Timeout,
}

impl FragmentError {
    pub const fn code(self) -> &'static str {
        match self {
            Self::Sequence => "fragment_sequence",
            Self::TooLarge => "fragment_too_large",
            Self::SocketBudget => "fragment_socket_budget",
            Self::Timeout => "fragment_timeout",
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct FragmentLimits {
    pub max_assembly_bytes: usize,
    pub max_socket_bytes: usize,
    pub timeout: Duration,
}

impl Default for FragmentLimits {
    fn default() -> Self {
        Self {
            max_assembly_bytes: TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES,
            max_socket_bytes: TERMINAL_WS_FRAGMENT_MAX_SOCKET_REASSEMBLY_BYTES,
            timeout: TERMINAL_WS_FRAGMENT_REASSEMBLY_TIMEOUT,
        }
    }
}

struct Assembly {
    event: String,
    terminal_id: String,
    message_seq: u64,
    next_index: u64,
    deadline: Instant,
    bytes: Vec<u8>,
}

/// One socket's in-flight fragment streams, keyed by attachment. Any refused
/// fragment drops its attachment's partial message.
pub struct FragmentAssembler {
    limits: FragmentLimits,
    assemblies: BTreeMap<String, Assembly>,
    held: usize,
}

impl FragmentAssembler {
    pub fn new(limits: FragmentLimits) -> Self {
        Self {
            limits,
            assemblies: BTreeMap::new(),
            held: 0,
        }
    }

    /// Accept one fragment; the final fragment returns the decoded event.
    pub fn push(&mut self, fragment: &Value, now: Instant) -> Result<Option<Value>, FragmentError> {
        let attachment = fragment["attachment_id"]
            .as_str()
            .ok_or(FragmentError::Sequence)?;
        let result = self.accept(attachment, fragment, now);
        if result.is_err() {
            self.drop_assembly(attachment);
        }
        result
    }

    fn accept(
        &mut self,
        attachment: &str,
        fragment: &Value,
        now: Instant,
    ) -> Result<Option<Value>, FragmentError> {
        if self
            .assemblies
            .get(attachment)
            .is_some_and(|assembly| assembly.deadline <= now)
        {
            return Err(FragmentError::Timeout);
        }
        let text = |name: &str| fragment[name].as_str().ok_or(FragmentError::Sequence);
        let counter =
            |name: &str| check_safe_int(&fragment[name]).map_err(|_| FragmentError::Sequence);
        if text("type")? != FRAGMENT_TYPE || text("encoding")? != FRAGMENT_ENCODING {
            return Err(FragmentError::Sequence);
        }
        let event = text("event")?;
        let terminal_id = text("terminal_id")?;
        let message_seq = counter("message_seq")?;
        let index = counter("fragment_index")?;
        let more = fragment["more"].as_bool().ok_or(FragmentError::Sequence)?;
        let encoded = text("payload")?;
        if encoded.len() % 4 != 0 {
            return Err(FragmentError::Sequence);
        }
        let padding = encoded
            .bytes()
            .rev()
            .take_while(|b| *b == b'=')
            .count()
            .min(2);
        let decoded_len = (encoded.len() / 4 * 3).saturating_sub(padding);

        let held_here = match self.assemblies.get(attachment) {
            None if index != 0 => return Err(FragmentError::Sequence),
            None => 0,
            Some(assembly) => {
                if assembly.message_seq != message_seq
                    || assembly.event != event
                    || assembly.terminal_id != terminal_id
                    || assembly.next_index != index
                {
                    return Err(FragmentError::Sequence);
                }
                assembly.bytes.len()
            }
        };
        // Budgets are checked from the encoded length, before decoding allocates.
        if held_here + decoded_len > self.limits.max_assembly_bytes {
            return Err(FragmentError::TooLarge);
        }
        if self.held + decoded_len > self.limits.max_socket_bytes {
            return Err(FragmentError::SocketBudget);
        }
        let chunk = STANDARD
            .decode(encoded)
            .map_err(|_| FragmentError::Sequence)?;
        let deadline = now + self.limits.timeout;
        let assembly = self
            .assemblies
            .entry(attachment.to_owned())
            .or_insert_with(|| Assembly {
                event: event.to_owned(),
                terminal_id: terminal_id.to_owned(),
                message_seq,
                next_index: 0,
                deadline,
                bytes: Vec::new(),
            });
        assembly.bytes.extend_from_slice(&chunk);
        assembly.next_index += 1;
        self.held += chunk.len();
        if more {
            return Ok(None);
        }
        let complete = self
            .drop_assembly(attachment)
            .ok_or(FragmentError::Sequence)?;
        let value = decode_message(&complete.bytes).map_err(|_| FragmentError::Sequence)?;
        if value["type"].as_str() != Some(complete.event.as_str()) {
            return Err(FragmentError::Sequence);
        }
        Ok(Some(value))
    }

    fn drop_assembly(&mut self, attachment: &str) -> Option<Assembly> {
        let assembly = self.assemblies.remove(attachment)?;
        self.held -= assembly.bytes.len();
        Some(assembly)
    }

    /// Drop every assembly whose deadline has passed; returns their
    /// attachment ids in order.
    pub fn expire(&mut self, now: Instant) -> Vec<String> {
        let expired = self
            .assemblies
            .iter()
            .filter(|(_, assembly)| assembly.deadline <= now)
            .map(|(attachment, _)| attachment.clone())
            .collect::<Vec<_>>();
        for attachment in &expired {
            self.drop_assembly(attachment);
        }
        expired
    }

    /// Decoded bytes held across all in-flight assemblies.
    pub fn held_bytes(&self) -> usize {
        self.held
    }
}

/// Python's `datetime.isoformat()` for a UTC timestamp: microseconds only
/// when nonzero, and a `+00:00` offset.
pub fn isoformat(at: &DateTime<Utc>) -> String {
    let seconds = at.format("%Y-%m-%dT%H:%M:%S");
    match at.timestamp_subsec_micros() {
        0 => format!("{seconds}+00:00"),
        micros => format!("{seconds}.{micros:06}+00:00"),
    }
}

/// The `<created_at iso>|<terminal_id>` cursor naming a row's page position.
pub fn list_cursor(row: &Terminal) -> String {
    format!("{}|{}", isoformat(&row.created_at), row.id)
}

/// Parse a page cursor. Falsy input, as in Python, is the first page.
/// Cursors are minted from timezone-aware rows, so the timestamp needs an
/// offset.
pub fn parse_list_cursor(
    raw: Option<&Value>,
) -> Result<Option<(DateTime<Utc>, Uuid)>, WsProtocolError> {
    let Some(raw) = raw.filter(|value| !is_falsy(value)) else {
        return Ok(None);
    };
    let text = raw.as_str().ok_or(WsProtocolError::InvalidCursor)?;
    let (created_at, terminal_id) = text.split_once('|').ok_or(WsProtocolError::InvalidCursor)?;
    let created_at = DateTime::parse_from_rfc3339(created_at)
        .map_err(|_| WsProtocolError::InvalidCursor)?
        .with_timezone(&Utc);
    let terminal_id = Uuid::parse_str(terminal_id).map_err(|_| WsProtocolError::InvalidCursor)?;
    Ok(Some((created_at, terminal_id)))
}

fn is_falsy(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::Bool(flag) => !flag,
        Value::Number(number) => number.as_f64() == Some(0.0),
        Value::String(text) => text.is_empty(),
        Value::Array(items) => items.is_empty(),
        Value::Object(map) => map.is_empty(),
    }
}

/// The backend-neutral inventory row. Handlers add per-request fields such as
/// `command` and `cwd` afterwards.
pub fn inventory_item(row: &Terminal, lease_holder: Option<Value>) -> Map<String, Value> {
    let backend = match row.backend {
        Backend::Native => "native",
        Backend::Tmux => "tmux",
    };
    let ownership = match row.ownership {
        Ownership::Gobby => "gobby",
        Ownership::External => "external",
    };
    let dims = match (row.rows, row.cols) {
        (Some(rows), Some(cols)) => json!({"rows": rows, "cols": cols}),
        _ => Value::Null,
    };
    let mut item = Map::new();
    item.insert("terminal_id".into(), json!(row.id.to_string()));
    item.insert("backend".into(), json!(backend));
    item.insert("ownership".into(), json!(ownership));
    item.insert("state".into(), json!(row.state.as_str()));
    item.insert("title".into(), json!(row.title));
    item.insert(
        "session_id".into(),
        json!(row.session_id.map(|id| id.to_string())),
    );
    item.insert(
        "agent_run_id".into(),
        json!(row.agent_run_id.map(|id| id.to_string())),
    );
    item.insert("lease_holder".into(), lease_holder.unwrap_or(Value::Null));
    item.insert("dims".into(), dims);
    item.insert("updated_at".into(), json!(isoformat(&row.updated_at)));
    item
}

/// One inventory page request: the rows in cursor order, the cursor after the
/// last row when more exist, and each row's own cursor for truncation.
pub struct Page<'a> {
    pub items: &'a [Value],
    pub next_cursor: Option<&'a str>,
    pub snapshot: Option<&'a Value>,
    pub item_cursors: &'a [String],
    pub envelope: &'a Map<String, Value>,
}

impl Page<'_> {
    /// The page under the 1 MiB encoded ceiling.
    pub fn encode(&self) -> Result<Value, WsProtocolError> {
        self.encode_within(TERMINAL_LIST_MAX_ENCODED_BYTES)
    }

    /// The longest prefix of rows that fits `max_bytes`, ending in the cursor
    /// of its last row when rows were cut. Each row is measured once.
    pub fn encode_within(&self, max_bytes: usize) -> Result<Value, WsProtocolError> {
        if self.items.is_empty() {
            let empty = self.payload(0, self.next_cursor);
            if python_encoded_len(&empty)? > max_bytes {
                return Err(WsProtocolError::PageTooLarge);
            }
            return Ok(empty);
        }
        let empty_size = python_encoded_len(&self.payload(0, None))? - "null".len();
        let mut rows_size = 0;
        let mut selected = None;
        for count in 1..=self.items.len() {
            let cursor = if count < self.items.len() {
                let cursor = self
                    .item_cursors
                    .get(count - 1)
                    .ok_or(WsProtocolError::MissingItemCursor)?;
                Some(cursor.as_str())
            } else {
                self.next_cursor
            };
            rows_size += python_encoded_len(&self.items[count - 1])? + usize::from(count > 1);
            if empty_size + rows_size + python_encoded_len(&json!(cursor))? > max_bytes {
                break;
            }
            selected = Some((count, cursor));
        }
        let (count, cursor) = selected.ok_or(WsProtocolError::PageTooLarge)?;
        Ok(self.payload(count, cursor))
    }

    fn payload(&self, count: usize, cursor: Option<&str>) -> Value {
        let mut payload = self.envelope.clone();
        payload.insert("items".into(), Value::Array(self.items[..count].to_vec()));
        payload.insert("next_cursor".into(), json!(cursor));
        payload.insert(
            "snapshot".into(),
            self.snapshot.cloned().unwrap_or(Value::Null),
        );
        Value::Object(payload)
    }
}

/// Length of Python's `json.dumps(value, separators=(",", ":"))`, which
/// escapes every non-ASCII character and DEL as `\uXXXX` (a surrogate pair
/// above the BMP). Page budgets are measured this way, so truncation lands on
/// the same row as Python.
fn python_encoded_len(value: &Value) -> Result<usize, WsProtocolError> {
    Ok(serde_json::to_string(value)?
        .chars()
        .map(|c| match u32::from(c) {
            0..0x7f => 1,
            0x7f..=0xffff => 6,
            _ => 12,
        })
        .sum())
}

/// Which bounded send queue an entry is offered to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QueueKind {
    /// Terminal frames and output.
    Frame,
    /// Lifecycle messages, reserved apart from frames.
    Lifecycle,
}

impl QueueKind {
    const fn max_entries(self) -> usize {
        match self {
            Self::Frame => TERMINAL_WS_FRAME_QUEUE_ENTRIES,
            Self::Lifecycle => TERMINAL_WS_LIFECYCLE_RESERVE_MAX_ENTRIES,
        }
    }

    const fn max_bytes(self) -> usize {
        match self {
            Self::Frame => TERMINAL_WS_FRAME_QUEUE_BYTES,
            Self::Lifecycle => TERMINAL_WS_LIFECYCLE_RESERVE_MAX_BYTES,
        }
    }
}

/// A refused queue entry, with the relay's close reason.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct QueueOverflow(pub QueueKind);

impl QueueOverflow {
    pub const fn code(self) -> &'static str {
        match self.0 {
            QueueKind::Frame => "relay_overflow",
            QueueKind::Lifecycle => "reserve_overflow",
        }
    }
}

/// A FIFO bounded by entry count and encoded bytes; an entry that would cross
/// either bound is refused before it is queued.
pub struct SendQueue<T> {
    kind: QueueKind,
    items: VecDeque<(T, usize)>,
    bytes: usize,
}

impl<T> SendQueue<T> {
    pub fn new(kind: QueueKind) -> Self {
        Self {
            kind,
            items: VecDeque::new(),
            bytes: 0,
        }
    }

    pub fn push(&mut self, item: T, size: usize) -> Result<(), QueueOverflow> {
        if self.items.len() >= self.kind.max_entries() || self.bytes + size > self.kind.max_bytes()
        {
            return Err(QueueOverflow(self.kind));
        }
        self.items.push_back((item, size));
        self.bytes += size;
        Ok(())
    }

    pub fn pop(&mut self) -> Option<T> {
        let (item, size) = self.items.pop_front()?;
        self.bytes -= size;
        Some(item)
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    pub fn bytes(&self) -> usize {
        self.bytes
    }
}

/// Write refusals reported as `terminal_write_outcome` reasons.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WriteRefusal {
    /// A paste above [`PASTE_MAX_BYTES`].
    Oversize,
    /// [`WRITE_SEQ_CAPACITY`] writes are already in flight.
    WriteSeqCapacity,
}

impl WriteRefusal {
    pub const fn code(self) -> &'static str {
        match self {
            Self::Oversize => "oversize",
            Self::WriteSeqCapacity => "write_seq_capacity",
        }
    }
}

/// Paste text is bounded in UTF-8 bytes.
pub fn check_paste(text: &str) -> Result<(), WriteRefusal> {
    if text.len() > PASTE_MAX_BYTES {
        return Err(WriteRefusal::Oversize);
    }
    Ok(())
}

/// Refuse a new write sequence while the in-flight window is full.
pub fn check_write_capacity(inflight: usize) -> Result<(), WriteRefusal> {
    if inflight >= WRITE_SEQ_CAPACITY {
        return Err(WriteRefusal::WriteSeqCapacity);
    }
    Ok(())
}

/// The `terminal_write_outcome` envelope.
pub fn write_outcome(
    terminal_id: &str,
    attachment_id: &str,
    client_write_seq: u64,
    outcome: &str,
    reason: Option<&str>,
) -> Result<Value, WsProtocolError> {
    check_safe_int(&json!(client_write_seq))?;
    Ok(json!({
        "type": "terminal_write_outcome",
        "terminal_id": terminal_id,
        "attachment_id": attachment_id,
        "client_write_seq": client_write_seq,
        "outcome": outcome,
        "reason": reason,
    }))
}
