//! Rust replay of the canonical backend-neutral terminal WS corpus.
//!
//! The corpus at `tests/fixtures/terminal_ws_golden/` is shared, read-only
//! evidence: Python and gclient already replay it, and this test proves the
//! daemon-side codec produces the same bytes. Expected page and fragment
//! values that are not fixtures were computed with
//! `src/gobby/terminals/ws_protocol.py` on the same inputs.

use std::collections::BTreeSet;
use std::fs;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use chrono::{DateTime, TimeZone, Utc};
use gobby_terminals::repository::{Backend, Ownership, State, Terminal};
use gobby_terminals::ws_protocol::{
    FragmentAssembler, FragmentError, FragmentLimits, PASTE_MAX_BYTES, Page, QueueKind, SendQueue,
    TERMINAL_LIST_DEFAULT_PAGE_SIZE, TERMINAL_LIST_MAX_ENCODED_BYTES, TERMINAL_LIST_MAX_PAGE_SIZE,
    TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES, TERMINAL_WS_FRAGMENT_MAX_SOCKET_REASSEMBLY_BYTES,
    TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES, TERMINAL_WS_FRAGMENT_REASSEMBLY_TIMEOUT,
    TERMINAL_WS_FRAME_QUEUE_BYTES, TERMINAL_WS_FRAME_QUEUE_ENTRIES, TERMINAL_WS_FRAME_SEND_TIMEOUT,
    TERMINAL_WS_LIFECYCLE_RESERVE_MAX_BYTES, TERMINAL_WS_LIFECYCLE_RESERVE_MAX_ENTRIES,
    TERMINAL_WS_LIFECYCLE_SEND_TIMEOUT, TERMINAL_WS_SAFE_INTEGER_MAX, WRITE_SEQ_CAPACITY,
    WriteRefusal, WsProtocolError, canonical_json, check_paste, check_write_capacity,
    decode_message, emit_proxied_event, encode_message, fragment_event, inventory_item, isoformat,
    list_cursor, parse_list_cursor, write_outcome,
};
use serde_json::{Map, Value, json};
use uuid::Uuid;

const TERMINAL: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ATTACHMENT: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn golden_dir() -> PathBuf {
    repo_root().join("tests/fixtures/terminal_ws_golden")
}

fn fixture(name: &str) -> Vec<u8> {
    fs::read(golden_dir().join(name)).unwrap_or_else(|e| panic!("unreadable fixture {name}: {e}"))
}

fn object(value: Value) -> Map<String, Value> {
    match value {
        Value::Object(map) => map,
        other => panic!("expected a JSON object, got {other}"),
    }
}

#[test]
fn canonical_terminal_ws_corpus_replays_byte_identically() {
    let manifest =
        object(serde_json::from_slice(&fixture("manifest.json")).expect("manifest must be JSON"));
    assert_eq!(manifest.keys().collect::<Vec<_>>(), ["fixtures"]);
    let names = manifest["fixtures"]
        .as_array()
        .expect("fixtures must be an array")
        .iter()
        .map(|name| {
            name.as_str()
                .expect("fixture name must be a string")
                .to_owned()
        })
        .collect::<Vec<_>>();
    let listed = names.iter().cloned().collect::<BTreeSet<_>>();
    assert_eq!(listed.len(), names.len(), "manifest lists a fixture twice");
    let on_disk = fs::read_dir(golden_dir())
        .expect("corpus directory")
        .map(|entry| {
            entry
                .expect("corpus entry")
                .file_name()
                .into_string()
                .expect("fixture names are UTF-8")
        })
        .filter(|name| name != "manifest.json")
        .collect::<BTreeSet<_>>();
    assert_eq!(
        listed, on_disk,
        "every corpus file is a manifest case and vice versa"
    );

    let mut replayed = 0;
    for name in &names {
        let raw = fixture(name);
        let decoded = decode_message(&raw).unwrap_or_else(|e| panic!("{name} decode: {e}"));
        let encoded = encode_message(&decoded).unwrap_or_else(|e| panic!("{name} encode: {e}"));
        assert_eq!(encoded, raw, "{name} must re-encode byte-for-byte");
        let mut canonical = canonical_json(&decoded).expect("canonical bytes");
        canonical.push(b'\n');
        assert_eq!(
            canonical, raw,
            "{name} canonical form differs only by the newline"
        );
        replayed += 1;
    }
    assert_eq!(replayed, names.len());
    assert!(replayed > 0, "the manifest must name at least one case");

    let moded = json!({"type": "terminal_attach", "mode": "direct"});
    assert!(matches!(
        encode_message(&moded),
        Err(WsProtocolError::ModeForbidden)
    ));
    assert!(matches!(
        canonical_json(&moded),
        Err(WsProtocolError::ModeForbidden)
    ));
    assert!(matches!(
        decode_message(b"[1]"),
        Err(WsProtocolError::NotObject)
    ));
    for counter in [
        "message_seq",
        "lease_generation",
        "client_write_seq",
        "fragment_index",
        "seq",
    ] {
        for bad in [
            json!(TERMINAL_WS_SAFE_INTEGER_MAX + 1),
            json!(-1),
            json!(true),
            json!("1"),
        ] {
            let message = json!({"type": "terminal_event", "nested": [{counter: bad}]});
            assert!(
                matches!(
                    encode_message(&message),
                    Err(WsProtocolError::SafeIntegerOverflow)
                ),
                "{counter}={bad} must be refused on encode"
            );
            let raw = serde_json::to_vec(&message).expect("raw JSON");
            assert!(
                matches!(
                    decode_message(&raw),
                    Err(WsProtocolError::SafeIntegerOverflow)
                ),
                "{counter}={bad} must be refused on decode"
            );
        }
    }
    let at_max = json!({"type": "terminal_event", "seq": TERMINAL_WS_SAFE_INTEGER_MAX});
    assert!(encode_message(&at_max).is_ok());
}

fn fragment(index: u64, seq: u64, more: bool, payload: &[u8]) -> Value {
    fragment_for(ATTACHMENT, "terminal_output", index, seq, more, payload)
}

fn fragment_for(
    attachment: &str,
    event: &str,
    index: u64,
    seq: u64,
    more: bool,
    payload: &[u8],
) -> Value {
    use base64::Engine as _;
    json!({
        "type": "terminal_ws_fragment",
        "event": event,
        "terminal_id": TERMINAL,
        "attachment_id": attachment,
        "message_seq": seq,
        "fragment_index": index,
        "more": more,
        "encoding": "utf8-b64",
        "payload": base64::engine::general_purpose::STANDARD.encode(payload),
    })
}

#[test]
fn fragment_limits_and_identity_match_python() {
    // Identity: the corpus fragments are Python's midpoint split of the
    // newline-terminated attach_history message.
    let history = fixture("attach_history.json");
    let fragments = fragment_event("terminal_attach_history", TERMINAL, ATTACHMENT, 1, &history)
        .expect("fragment attach history");
    let encoded = fragments
        .iter()
        .map(|f| encode_message(f).expect("encode fragment"))
        .collect::<Vec<_>>();
    assert_eq!(
        encoded,
        [fixture("fragment.json"), fixture("fragment_last.json")]
    );

    let start = Instant::now();
    let mut assembler = FragmentAssembler::new(FragmentLimits::default());
    assert_eq!(assembler.push(&fragments[0], start), Ok(None));
    assert_eq!(assembler.held_bytes(), 106);
    let restored = assembler
        .push(&fragments[1], start)
        .expect("reassemble")
        .expect("final fragment completes the event");
    assert_eq!(restored, decode_message(&history).expect("history"));
    assert_eq!(assembler.held_bytes(), 0);

    // Proxied events below the wrapped limit pass through with canonical bytes.
    let event = decode_message(&history).expect("history");
    assert_eq!(
        emit_proxied_event(&event, 1).expect("small event"),
        [(event.clone(), canonical_json(&event).expect("canonical"))]
    );

    // A 3 MiB event fragments exactly as Python does and reassembles to the
    // canonical event.
    let big = json!({
        "type": "terminal_output",
        "terminal_id": TERMINAL,
        "attachment_id": ATTACHMENT,
        "data": "x".repeat(3 * 1024 * 1024),
    });
    let wrapped = emit_proxied_event(&big, 9).expect("large event");
    let chunk_lengths = wrapped
        .iter()
        .map(|(payload, _)| {
            use base64::Engine as _;
            base64::engine::general_purpose::STANDARD
                .decode(payload["payload"].as_str().expect("payload"))
                .expect("base64")
                .len()
        })
        .collect::<Vec<_>>();
    assert_eq!(chunk_lengths, PYTHON_BIG_CHUNKS);
    let wire_lengths = wrapped.iter().map(|(_, raw)| raw.len()).collect::<Vec<_>>();
    assert_eq!(wire_lengths, PYTHON_BIG_WIRE);
    let mut assembler = FragmentAssembler::new(FragmentLimits::default());
    let mut completed = None;
    for (index, (payload, raw)) in wrapped.iter().enumerate() {
        assert_eq!(&canonical_json(payload).expect("canonical"), raw);
        assert!(raw.len() < TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES);
        assert_eq!(payload["fragment_index"], json!(index));
        assert_eq!(payload["more"], json!(index + 1 < wrapped.len()));
        completed = assembler.push(payload, start).expect("reassemble big");
    }
    assert_eq!(completed, Some(big));

    // Oversized inputs are refused before any fragment is built.
    let oversized = vec![b'x'; TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES + 1];
    assert!(matches!(
        fragment_event("terminal_output", TERMINAL, ATTACHMENT, 1, &oversized),
        Err(WsProtocolError::FragmentTooLarge)
    ));
    assert!(matches!(
        fragment_event(
            "terminal_output",
            TERMINAL,
            ATTACHMENT,
            TERMINAL_WS_SAFE_INTEGER_MAX + 1,
            b"{}"
        ),
        Err(WsProtocolError::SafeIntegerOverflow)
    ));
    let huge = json!({
        "type": "terminal_output",
        "terminal_id": TERMINAL,
        "attachment_id": ATTACHMENT,
        "data": "x".repeat(TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES),
    });
    assert!(matches!(
        emit_proxied_event(&huge, 1),
        Err(WsProtocolError::FragmentTooLarge)
    ));

    // Reassembly limits, scaled down so the test does not allocate megabytes.
    let limits = FragmentLimits {
        max_assembly_bytes: 64,
        max_socket_bytes: 100,
        timeout: Duration::from_millis(5000),
    };
    let body = br#"{"type":"terminal_output","data":"abcdefghijklmnop"}"#;
    let (head, tail) = body.split_at(20);

    // Excessive: one assembly crossing its own budget.
    let mut assembler = FragmentAssembler::new(limits);
    assert_eq!(
        assembler.push(&fragment(0, 1, true, &[b' '; 60]), start),
        Ok(None)
    );
    assert_eq!(
        assembler.push(&fragment(1, 1, true, &[b' '; 5]), start),
        Err(FragmentError::TooLarge)
    );
    assert_eq!(assembler.held_bytes(), 0, "a refused assembly is dropped");

    // Excessive: concurrent assemblies crossing the socket budget.
    let mut assembler = FragmentAssembler::new(limits);
    let first = fragment_for("first", "terminal_output", 0, 1, true, &[b' '; 60]);
    let second = fragment_for("second", "terminal_output", 0, 1, true, &[b' '; 41]);
    assert_eq!(assembler.push(&first, start), Ok(None));
    assert_eq!(
        assembler.push(&second, start),
        Err(FragmentError::SocketBudget)
    );
    assert_eq!(assembler.held_bytes(), 60);

    // Duplicate index.
    let mut assembler = FragmentAssembler::new(limits);
    assert_eq!(assembler.push(&fragment(0, 1, true, head), start), Ok(None));
    assert_eq!(
        assembler.push(&fragment(0, 1, true, head), start),
        Err(FragmentError::Sequence)
    );
    assert_eq!(assembler.held_bytes(), 0);

    // Missing index: a stream that does not start at zero, and a gap.
    let mut assembler = FragmentAssembler::new(limits);
    assert_eq!(
        assembler.push(&fragment(1, 1, false, tail), start),
        Err(FragmentError::Sequence)
    );
    assert_eq!(assembler.push(&fragment(0, 1, true, head), start), Ok(None));
    assert_eq!(
        assembler.push(&fragment(2, 1, false, tail), start),
        Err(FragmentError::Sequence)
    );
    assert_eq!(assembler.held_bytes(), 0);

    // Expired: the deadline is fixed at the first fragment and does not slide.
    let mut assembler = FragmentAssembler::new(limits);
    assert_eq!(
        assembler.push(&fragment(0, 1, true, &head[..10]), start),
        Ok(None)
    );
    let almost = start + Duration::from_millis(4999);
    assert_eq!(
        assembler.push(&fragment(1, 1, true, &head[10..]), almost),
        Ok(None)
    );
    assert_eq!(
        assembler.push(
            &fragment(2, 1, false, tail),
            start + Duration::from_millis(5000)
        ),
        Err(FragmentError::Timeout)
    );
    assert_eq!(assembler.held_bytes(), 0);
    let mut assembler = FragmentAssembler::new(limits);
    assert_eq!(assembler.push(&fragment(0, 1, true, head), start), Ok(None));
    assert!(assembler.expire(almost).is_empty());
    assert_eq!(
        assembler.expire(start + Duration::from_millis(5000)),
        [ATTACHMENT.to_owned()]
    );
    assert_eq!(assembler.held_bytes(), 0);

    // Cross-message: a different message_seq, or a different event, mid-stream.
    let mut assembler = FragmentAssembler::new(limits);
    assert_eq!(assembler.push(&fragment(0, 1, true, head), start), Ok(None));
    assert_eq!(
        assembler.push(&fragment(1, 2, false, tail), start),
        Err(FragmentError::Sequence)
    );
    let mut assembler = FragmentAssembler::new(limits);
    assert_eq!(assembler.push(&fragment(0, 1, true, head), start), Ok(None));
    let other_event = fragment_for(ATTACHMENT, "terminal_frame", 1, 1, false, tail);
    assert_eq!(
        assembler.push(&other_event, start),
        Err(FragmentError::Sequence)
    );
    // ...or a completed payload whose type is not the announced event.
    let mut assembler = FragmentAssembler::new(limits);
    let mislabeled = fragment_for(ATTACHMENT, "terminal_frame", 0, 1, false, body);
    assert_eq!(
        assembler.push(&mislabeled, start),
        Err(FragmentError::Sequence)
    );
    assert_eq!(assembler.held_bytes(), 0);

    // The same split completes when it is well formed.
    let mut assembler = FragmentAssembler::new(limits);
    assert_eq!(assembler.push(&fragment(0, 1, true, head), start), Ok(None));
    assert_eq!(
        assembler.push(&fragment(1, 1, false, tail), start),
        Ok(Some(
            json!({"type": "terminal_output", "data": "abcdefghijklmnop"})
        ))
    );

    let codes = [
        FragmentError::Sequence,
        FragmentError::TooLarge,
        FragmentError::SocketBudget,
        FragmentError::Timeout,
    ]
    .map(|error| error.code());
    assert_eq!(
        codes,
        [
            "fragment_sequence",
            "fragment_too_large",
            "fragment_socket_budget",
            "fragment_timeout"
        ]
    );
    assert_eq!(
        FragmentLimits::default().max_assembly_bytes,
        TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES
    );
    assert_eq!(
        FragmentLimits::default().max_socket_bytes,
        TERMINAL_WS_FRAGMENT_MAX_SOCKET_REASSEMBLY_BYTES
    );
    assert_eq!(
        FragmentLimits::default().timeout,
        TERMINAL_WS_FRAGMENT_REASSEMBLY_TIMEOUT
    );
}

/// `emit_proxied_event` chunk and wire sizes for the 3 MiB event, from Python.
const PYTHON_BIG_CHUNKS: [usize; 3] = [786_468, 1_179_702, 1_179_702];
const PYTHON_BIG_WIRE: [usize; 3] = [1_048_871, 1_573_183, 1_573_184];

fn timestamp(seconds: i64, micros: u32) -> DateTime<Utc> {
    Utc.timestamp_opt(seconds, micros * 1000)
        .single()
        .expect("timestamp")
}

fn terminal(
    id: &str,
    backend: Backend,
    title: &str,
    created_at: DateTime<Utc>,
    updated_at: DateTime<Utc>,
) -> Terminal {
    Terminal {
        id: Uuid::parse_str(id).expect("terminal id"),
        backend,
        ownership: Ownership::Gobby,
        state: State::Live,
        spawn_key: None,
        machine_id: Uuid::nil(),
        project_id: Uuid::nil(),
        session_id: None,
        agent_run_id: None,
        locator: None,
        locator_key: None,
        session_name: None,
        window_id: None,
        title: Some(title.to_owned()),
        host_epoch: None,
        rows: Some(24),
        cols: Some(80),
        attempt_generation: 1,
        attempt_started_at: created_at,
        process: None,
        unresolved_writes: json!([]),
        automatic_write_quarantined_at: None,
        automatic_write_quarantine_action_key: None,
        created_at,
        updated_at,
        liveness_at: None,
    }
}

/// 2026-10-01T13:11:48.886203+00:00
const BASE_SECONDS: i64 = 1_790_860_308;
const BASE_MICROS: u32 = 886_203;

fn page_rows() -> Vec<Terminal> {
    (0..6_i64)
        .map(|i| {
            let created = if i == 2 {
                timestamp(BASE_SECONDS, 0)
            } else {
                timestamp(BASE_SECONDS + i * 60, BASE_MICROS)
            };
            let updated = if i == 3 {
                timestamp(BASE_SECONDS, 0)
            } else {
                timestamp(BASE_SECONDS + i, BASE_MICROS)
            };
            let backend = if i % 2 == 1 {
                Backend::Tmux
            } else {
                Backend::Native
            };
            terminal(
                &format!("{i:08x}-0000-4000-8000-000000000000"),
                backend,
                &format!("{}-{i}", "终端".repeat(20)),
                created,
                updated,
            )
        })
        .collect()
}

#[test]
fn inventory_pagination_matches_python() {
    // Identity: the corpus list page rebuilt from its row.
    let epoch = timestamp(1_767_225_600, 0);
    let row = terminal(TERMINAL, Backend::Native, "sess", epoch, epoch);
    let mut item = inventory_item(&row, None);
    item.insert("command".into(), Value::Null);
    item.insert("cwd".into(), Value::Null);
    let envelope = object(json!({"type": "terminal_list", "request_id": "req-list-1"}));
    let snapshot = json!({"daemon_epoch": "00000000-0000-4000-8000-000000000000", "seq": 0});
    let page = Page {
        items: &[Value::Object(item)],
        next_cursor: None,
        snapshot: Some(&snapshot),
        item_cursors: &[],
        envelope: &envelope,
    }
    .encode()
    .expect("list page");
    assert_eq!(
        encode_message(&page).expect("encode"),
        fixture("list_snapshot.json")
    );

    // Cursors and timestamps render exactly as Python's isoformat.
    let rows = page_rows();
    let cursors = rows.iter().map(list_cursor).collect::<Vec<_>>();
    assert_eq!(cursors, PYTHON_CURSORS);
    let items = rows
        .iter()
        .map(|row| Value::Object(inventory_item(row, None)))
        .collect::<Vec<_>>();
    let updated = items
        .iter()
        .map(|item| item["updated_at"].as_str().expect("updated_at").to_owned())
        .collect::<Vec<_>>();
    assert_eq!(updated, PYTHON_UPDATED);
    assert_eq!(isoformat(&rows[3].updated_at), "2026-10-01T13:11:48+00:00");
    for (cursor, row) in cursors.iter().zip(&rows) {
        assert_eq!(
            parse_list_cursor(Some(&json!(cursor))).expect("cursor"),
            Some((row.created_at, row.id))
        );
    }

    // Truncation lands on the same row and cursor as Python. Python measures
    // with ASCII escapes, so the CJK titles cost six bytes per character.
    let envelope = object(json!({"type": "terminal_list", "request_id": "req-page"}));
    let page = |next: Option<&'static str>| Page {
        items: &items,
        next_cursor: next,
        snapshot: None,
        item_cursors: &cursors,
        envelope: &envelope,
    };
    let truncated = page(None)
        .encode_within(PYTHON_TRUNCATION_CEILING)
        .expect("truncated page");
    let kept = truncated["items"].as_array().expect("items");
    assert_eq!(kept.len(), PYTHON_TRUNCATION_COUNT);
    assert_eq!(
        truncated["next_cursor"],
        json!(cursors[PYTHON_TRUNCATION_COUNT - 1])
    );
    for (kept, row) in kept.iter().zip(&rows) {
        assert_eq!(kept["terminal_id"], json!(row.id.to_string()));
        assert_eq!(
            kept["backend"],
            json!(if row.backend == Backend::Tmux {
                "tmux"
            } else {
                "native"
            })
        );
    }
    let at_three = page(None)
        .encode_within(PYTHON_THREE_ROW_CEILING)
        .expect("three rows");
    assert_eq!(at_three["items"].as_array().expect("items").len(), 3);
    let below_three = page(None)
        .encode_within(PYTHON_THREE_ROW_CEILING - 1)
        .expect("two rows");
    assert_eq!(below_three["items"].as_array().expect("items").len(), 2);
    assert_eq!(below_three["next_cursor"], json!(cursors[1]));
    assert!(matches!(
        page(None).encode_within(PYTHON_FIRST_FIT - 1),
        Err(WsProtocolError::PageTooLarge)
    ));
    let first = page(None)
        .encode_within(PYTHON_FIRST_FIT)
        .expect("first fit");
    assert_eq!(first["items"].as_array().expect("items").len(), 1);
    assert_eq!(first["next_cursor"], json!(cursors[0]));
    let full = page(Some(PYTHON_CURSORS[5])).encode().expect("full page");
    assert_eq!(full["items"], Value::Array(items.clone()));
    assert_eq!(full["next_cursor"], json!(cursors[5]));
    assert_eq!(full["snapshot"], Value::Null);
    let missing_cursors = Page {
        items: &items,
        next_cursor: None,
        snapshot: None,
        item_cursors: &cursors[..1],
        envelope: &envelope,
    };
    assert!(matches!(
        missing_cursors.encode_within(PYTHON_TRUNCATION_CEILING),
        Err(WsProtocolError::MissingItemCursor)
    ));
    let empty = Page {
        items: &[],
        next_cursor: None,
        snapshot: None,
        item_cursors: &[],
        envelope: &envelope,
    };
    assert_eq!(
        empty.encode().expect("empty page"),
        json!({"type": "terminal_list", "request_id": "req-page", "items": [], "next_cursor": null, "snapshot": null})
    );
    assert!(matches!(
        empty.encode_within(10),
        Err(WsProtocolError::PageTooLarge)
    ));

    // Pages are emitted through the codec, so their counters stay safe integers.
    let unsafe_snapshot = json!({"daemon_epoch": "e", "seq": TERMINAL_WS_SAFE_INTEGER_MAX + 1});
    let unsafe_page = Page {
        items: &[],
        next_cursor: None,
        snapshot: Some(&unsafe_snapshot),
        item_cursors: &[],
        envelope: &envelope,
    }
    .encode()
    .expect("page");
    assert!(matches!(
        encode_message(&unsafe_page),
        Err(WsProtocolError::SafeIntegerOverflow)
    ));

    // Cursor parsing matches Python: falsy input is the first page, and the id
    // is normalized through UUID parsing.
    for first_page in [
        None,
        Some(json!(null)),
        Some(json!("")),
        Some(json!(0)),
        Some(json!(false)),
    ] {
        assert_eq!(
            parse_list_cursor(first_page.as_ref()).expect("first page"),
            None
        );
    }
    assert_eq!(
        parse_list_cursor(Some(&json!(
            "2026-10-01T13:11:48+00:00|{AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA}"
        )))
        .expect("braced id"),
        Some((
            timestamp(BASE_SECONDS, 0),
            Uuid::parse_str(TERMINAL).expect("id")
        ))
    );
    for bad in [
        json!("2026-10-01T13:11:48+00:00"),
        json!("2026-10-01T13:11:48+00:00|"),
        json!("2026-10-01T13:11:48+00:00|not-a-uuid"),
        json!("yesterday|aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        json!(7),
    ] {
        assert!(
            matches!(
                parse_list_cursor(Some(&bad)),
                Err(WsProtocolError::InvalidCursor)
            ),
            "{bad} must be an invalid cursor"
        );
    }

    let holder = json!({"attachment_id": ATTACHMENT, "session_id": null});
    let held = inventory_item(&row, Some(holder.clone()));
    assert_eq!(held["lease_holder"], holder);
    assert_eq!(held["dims"], json!({"rows": 24, "cols": 80}));
    let mut no_dims = row.clone();
    no_dims.cols = None;
    assert_eq!(inventory_item(&no_dims, None)["dims"], Value::Null);
}

/// Python cursors, timestamps and truncation results for `page_rows()`.
const PYTHON_CURSORS: [&str; 6] = [
    "2026-10-01T13:11:48.886203+00:00|00000000-0000-4000-8000-000000000000",
    "2026-10-01T13:12:48.886203+00:00|00000001-0000-4000-8000-000000000000",
    "2026-10-01T13:11:48+00:00|00000002-0000-4000-8000-000000000000",
    "2026-10-01T13:14:48.886203+00:00|00000003-0000-4000-8000-000000000000",
    "2026-10-01T13:15:48.886203+00:00|00000004-0000-4000-8000-000000000000",
    "2026-10-01T13:16:48.886203+00:00|00000005-0000-4000-8000-000000000000",
];
const PYTHON_UPDATED: [&str; 6] = [
    "2026-10-01T13:11:48.886203+00:00",
    "2026-10-01T13:11:49.886203+00:00",
    "2026-10-01T13:11:50.886203+00:00",
    "2026-10-01T13:11:48+00:00",
    "2026-10-01T13:11:52.886203+00:00",
    "2026-10-01T13:11:53.886203+00:00",
];
/// Python keeps 3 rows here; a UTF-8 measurement would keep 4.
const PYTHON_TRUNCATION_CEILING: usize = 1700;
const PYTHON_TRUNCATION_COUNT: usize = 3;
/// The smallest ceiling at which Python keeps 3 rows (2 rows one byte below).
const PYTHON_THREE_ROW_CEILING: usize = 1642;
/// The smallest ceiling at which Python emits any row.
const PYTHON_FIRST_FIT: usize = 657;

/// Parse `NAME: Final[...] = <expr>` constants out of the Python contract.
fn python_constants() -> Vec<(String, f64)> {
    let source = fs::read_to_string(repo_root().join("src/gobby/terminals/ws_protocol.py"))
        .expect("Python WS contract");
    source
        .lines()
        .filter_map(|line| {
            let (name, rest) = line.split_once(": Final[")?;
            let (_, expr) = rest.split_once("] = ")?;
            let value = expr.trim();
            if value.starts_with("frozenset") {
                return None;
            }
            Some((name.to_owned(), eval_python(value)))
        })
        .collect()
}

/// Evaluate the integer/float literal forms the contract uses: `a * b`,
/// `a**b - c`, and plain literals.
fn eval_python(expr: &str) -> f64 {
    let (minuend, subtrahend) = match expr.split_once(" - ") {
        Some((left, right)) => (left, eval_python(right)),
        None => (expr, 0.0),
    };
    let product = minuend
        .split(" * ")
        .map(|factor| match factor.split_once("**") {
            Some((base, exponent)) => {
                let base = base.parse::<f64>().expect("power base");
                let exponent = exponent.parse::<i32>().expect("power exponent");
                base.powi(exponent)
            }
            None => factor
                .parse::<f64>()
                .unwrap_or_else(|_| panic!("literal {factor}")),
        })
        .product::<f64>();
    product - subtrahend
}

#[test]
fn wire_limits_and_failure_envelopes_match_python() {
    #[allow(clippy::cast_precision_loss)]
    let rust = [
        (
            "TERMINAL_WS_SAFE_INTEGER_MAX",
            TERMINAL_WS_SAFE_INTEGER_MAX as f64,
        ),
        (
            "TERMINAL_LIST_DEFAULT_PAGE_SIZE",
            TERMINAL_LIST_DEFAULT_PAGE_SIZE as f64,
        ),
        (
            "TERMINAL_LIST_MAX_PAGE_SIZE",
            TERMINAL_LIST_MAX_PAGE_SIZE as f64,
        ),
        (
            "TERMINAL_LIST_MAX_ENCODED_BYTES",
            TERMINAL_LIST_MAX_ENCODED_BYTES as f64,
        ),
        (
            "TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES",
            TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES as f64,
        ),
        (
            "TERMINAL_WS_FRAGMENT_MAX_SOCKET_REASSEMBLY_BYTES",
            TERMINAL_WS_FRAGMENT_MAX_SOCKET_REASSEMBLY_BYTES as f64,
        ),
        (
            "TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES",
            TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES as f64,
        ),
        (
            "TERMINAL_WS_FRAGMENT_REASSEMBLY_TIMEOUT_MS",
            TERMINAL_WS_FRAGMENT_REASSEMBLY_TIMEOUT.as_millis() as f64,
        ),
        (
            "TERMINAL_WS_FRAME_QUEUE_ENTRIES",
            TERMINAL_WS_FRAME_QUEUE_ENTRIES as f64,
        ),
        (
            "TERMINAL_WS_FRAME_QUEUE_BYTES",
            TERMINAL_WS_FRAME_QUEUE_BYTES as f64,
        ),
        (
            "TERMINAL_WS_FRAME_SEND_TIMEOUT_S",
            TERMINAL_WS_FRAME_SEND_TIMEOUT.as_secs_f64(),
        ),
        (
            "TERMINAL_WS_LIFECYCLE_RESERVE_MAX_ENTRIES",
            TERMINAL_WS_LIFECYCLE_RESERVE_MAX_ENTRIES as f64,
        ),
        (
            "TERMINAL_WS_LIFECYCLE_RESERVE_MAX_BYTES",
            TERMINAL_WS_LIFECYCLE_RESERVE_MAX_BYTES as f64,
        ),
        (
            "TERMINAL_WS_LIFECYCLE_SEND_TIMEOUT_S",
            TERMINAL_WS_LIFECYCLE_SEND_TIMEOUT.as_secs_f64(),
        ),
        ("PASTE_MAX_BYTES", PASTE_MAX_BYTES as f64),
        ("WRITE_SEQ_CAPACITY", WRITE_SEQ_CAPACITY as f64),
    ];
    let python = python_constants();
    assert_eq!(
        python
            .iter()
            .map(|(name, _)| name.as_str())
            .collect::<Vec<_>>(),
        rust.iter().map(|(name, _)| *name).collect::<Vec<_>>(),
        "every Python wire constant has a Rust twin, in order"
    );
    for ((name, expected), (_, actual)) in python.iter().zip(rust) {
        assert!(
            (expected - actual).abs() < f64::EPSILON,
            "{name}: {actual} != {expected}"
        );
    }

    // Write refusals use the corpus envelopes.
    assert_eq!(check_write_capacity(WRITE_SEQ_CAPACITY - 1), Ok(()));
    let refusal = check_write_capacity(WRITE_SEQ_CAPACITY).expect_err("capacity reached");
    assert_eq!(refusal, WriteRefusal::WriteSeqCapacity);
    let capacity = write_outcome(TERMINAL, ATTACHMENT, 64, "refused", Some(refusal.code()))
        .expect("capacity outcome");
    assert_eq!(
        encode_message(&capacity).expect("encode"),
        fixture("write_outcome_capacity.json")
    );
    let held = write_outcome(TERMINAL, ATTACHMENT, 3, "refused", Some("held")).expect("held");
    assert_eq!(
        encode_message(&held).expect("encode"),
        fixture("write_outcome_refused.json")
    );
    let delivered = write_outcome(TERMINAL, ATTACHMENT, 1, "delivered", None).expect("delivered");
    assert_eq!(
        encode_message(&delivered).expect("encode"),
        fixture("write_outcome.json")
    );
    assert!(matches!(
        write_outcome(
            TERMINAL,
            ATTACHMENT,
            TERMINAL_WS_SAFE_INTEGER_MAX + 1,
            "refused",
            None
        ),
        Err(WsProtocolError::SafeIntegerOverflow)
    ));

    // Paste is bounded in UTF-8 bytes, not characters.
    assert_eq!(check_paste(&"a".repeat(PASTE_MAX_BYTES)), Ok(()));
    assert_eq!(
        check_paste(&"a".repeat(PASTE_MAX_BYTES + 1)),
        Err(WriteRefusal::Oversize)
    );
    assert_eq!(check_paste(&"é".repeat(PASTE_MAX_BYTES / 2)), Ok(()));
    assert_eq!(
        check_paste(&format!("{}a", "é".repeat(PASTE_MAX_BYTES / 2))),
        Err(WriteRefusal::Oversize)
    );
    assert_eq!(WriteRefusal::Oversize.code(), "oversize");
    assert_eq!(WriteRefusal::WriteSeqCapacity.code(), "write_seq_capacity");

    // Frame queue and lifecycle reserve refuse before admitting the entry.
    for (kind, entries, bytes, code) in [
        (
            QueueKind::Frame,
            TERMINAL_WS_FRAME_QUEUE_ENTRIES,
            TERMINAL_WS_FRAME_QUEUE_BYTES,
            "relay_overflow",
        ),
        (
            QueueKind::Lifecycle,
            TERMINAL_WS_LIFECYCLE_RESERVE_MAX_ENTRIES,
            TERMINAL_WS_LIFECYCLE_RESERVE_MAX_BYTES,
            "reserve_overflow",
        ),
    ] {
        let mut queue = SendQueue::new(kind);
        for n in 0..entries {
            assert_eq!(queue.push(n, 1), Ok(()));
        }
        let overflow = queue.push(entries, 1).expect_err("entry bound");
        assert_eq!(overflow.code(), code);
        assert_eq!(queue.len(), entries);
        assert_eq!(queue.pop(), Some(0));
        assert_eq!(queue.pop(), Some(1));
        assert_eq!(queue.push(entries, bytes - (entries - 2)), Ok(()));
        assert_eq!(queue.bytes(), bytes);
        assert_eq!(queue.len(), entries - 1, "one entry slot is still free");
        let overflow = queue.push(entries + 1, 1).expect_err("byte bound");
        assert_eq!(overflow.code(), code);
        assert_eq!(queue.bytes(), bytes, "a refused entry is not charged");
    }

    // Typed failures carry the Python error codes.
    assert_eq!(
        WsProtocolError::SafeIntegerOverflow.to_string(),
        "safe_integer_overflow"
    );
    assert_eq!(
        WsProtocolError::PageTooLarge.to_string(),
        "terminal_page_too_large"
    );
    assert_eq!(
        WsProtocolError::FragmentTooLarge.to_string(),
        "fragment_too_large"
    );
    assert_eq!(WsProtocolError::InvalidCursor.to_string(), "invalid_cursor");
}
