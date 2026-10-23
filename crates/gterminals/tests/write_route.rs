//! Loopback write route over the Rust coordinator (#24570, plan section 2.6).
//!
//! Every test drives `Route::handle` with the JSON envelope the HTTP route
//! carries, so the wire format and the typed replies are pinned together.

mod common;

use std::collections::VecDeque;
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use base64::{Engine, engine::general_purpose::STANDARD};
use gobby_terminals::leases::{self, Registry, Viewer};
use gobby_terminals::repository::{Backend, Terminal};
use gobby_terminals::write::{
    BoxFuture, Coordinator, Delivery, Runtime, Runtimes, Stage, Step, WriteFailure,
};
use gobby_terminals::write_api::{Route, TokenLease, TokenSlot};
use serde_json::{Value, json};
use tokio::sync::oneshot;
use uuid::Uuid;

use common::{Hub, terminal};

type Scripted = Result<Delivery, WriteFailure>;

/// Records every dispatched payload, answers from a script, and can hold one
/// write inside the runtime until the test releases it.
#[derive(Default)]
struct FakeRuntime {
    payloads: StdMutex<Vec<String>>,
    script: StdMutex<VecDeque<Scripted>>,
    gate: StdMutex<Option<(oneshot::Sender<()>, oneshot::Receiver<()>)>>,
}

impl FakeRuntime {
    fn script(&self, outcomes: impl IntoIterator<Item = Scripted>) {
        self.script.lock().unwrap().extend(outcomes);
    }

    /// Holds the next write after it is recorded. The returned receiver fires
    /// once the write is inside the runtime; sending on the returned sender
    /// lets it finish.
    fn hold_next(&self) -> (oneshot::Receiver<()>, oneshot::Sender<()>) {
        let (entered_tx, entered_rx) = oneshot::channel();
        let (release_tx, release_rx) = oneshot::channel();
        *self.gate.lock().unwrap() = Some((entered_tx, release_rx));
        (entered_rx, release_tx)
    }

    fn payloads(&self) -> Vec<String> {
        self.payloads.lock().unwrap().clone()
    }
}

impl Runtime for FakeRuntime {
    fn write<'a>(&'a self, _terminal: &'a Terminal, step: &'a Step) -> BoxFuture<'a, Scripted> {
        Box::pin(async move {
            self.payloads.lock().unwrap().push(step.payload.clone());
            let gate = self.gate.lock().unwrap().take();
            if let Some((entered, release)) = gate {
                let _ = entered.send(());
                let _ = release.await;
            }
            self.script
                .lock()
                .unwrap()
                .pop_front()
                .unwrap_or(Ok(Delivery::Delivered))
        })
    }
}

struct Fixture {
    hub: Hub,
    terminal: Uuid,
    leases: Arc<Registry>,
    native: Arc<FakeRuntime>,
    token: TokenSlot,
    _token_lease: TokenLease,
    route: Arc<Route>,
}

async fn fixture() -> anyhow::Result<Fixture> {
    let hub = Hub::new().await?;
    let mut row = terminal();
    row.unresolved_writes = json!({});
    row.automatic_write_quarantined_at = None;
    row.automatic_write_quarantine_action_key = None;
    let id = row.id;
    hub.insert(&row).await?;
    let (leases, mut events) = Registry::new(256);
    tokio::spawn(async move { while events.recv().await.is_some() {} });
    let leases = Arc::new(leases);
    let native = Arc::new(FakeRuntime::default());
    let tmux = Arc::new(FakeRuntime::default());
    let runtimes = Runtimes {
        native: Some(native.clone() as Arc<dyn Runtime>),
        tmux: Some(tmux as Arc<dyn Runtime>),
    };
    let coordinator = Arc::new(Coordinator::new(
        hub.coordinator_pool()?,
        leases.clone(),
        runtimes,
    ));
    let token = TokenSlot::default();
    let token_lease = token.install("token-1".into());
    let route = Arc::new(Route::new(coordinator, token.clone()));
    Ok(Fixture {
        hub,
        terminal: id,
        leases,
        native,
        token,
        _token_lease: token_lease,
        route,
    })
}

/// Attaches a viewer and takes control, returning the attachment and the
/// lease claim as the route's JSON carries it.
async fn claim(leases: &Registry, terminal: Uuid) -> anyhow::Result<(leases::Attachment, Value)> {
    let attachment = leases
        .attach(
            terminal,
            Uuid::new_v4(),
            Viewer::Web,
            Backend::Native,
            leases::Delivery::Proxy,
        )
        .await?;
    let control = leases.take_control(attachment, true).await?;
    anyhow::ensure!(control.granted, "control granted");
    let lease = json!({"attachment_id": attachment.id, "generation": control.generation});
    Ok((attachment, lease))
}

fn step(kind: &str, payload: &[u8]) -> Value {
    json!({"kind": kind, "payload": STANDARD.encode(payload), "submit": false})
}

fn write_call(terminal: Uuid, origin: &str, action: &str, step: Value, lease: &Value) -> Value {
    json!({
        "call": "write",
        "terminal_id": terminal,
        "action_key": action,
        "origin": origin,
        "step": step,
        "lease": lease,
    })
}

fn held_write(terminal: Uuid, action: &str, step: Value, lease: &Value, hold: &Value) -> Value {
    let mut call = write_call(terminal, "operator", action, step, lease);
    call["hold"] = hold.clone();
    call
}

fn refusal_code(reply: &Value) -> &str {
    assert_eq!(reply["status"], "refusal", "{reply}");
    reply["code"].as_str().expect("refusal code")
}

/// Waits until no admitted write is executing, so a test can read its effect.
async fn wait_idle(route: &Route) {
    for _ in 0..500 {
        if route.active() == 0 {
            return;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    panic!("admitted write never settled");
}

#[tokio::test]
async fn route_calls_return_typed_per_step_outcomes() -> anyhow::Result<()> {
    let fx = fixture().await?;
    let (_attachment, lease) = claim(&fx.leases, fx.terminal).await?;
    let t = fx.terminal;

    let reply = fx
        .route
        .handle(write_call(
            t,
            "operator",
            "op-1",
            step("text", b"hi"),
            &lease,
        ))
        .await;
    assert_eq!(reply, json!({"status": "delivered"}));

    fx.native.script([Ok(Delivery::Uncertain {
        detail: "bytes may have landed".into(),
    })]);
    let reply = fx
        .route
        .handle(write_call(
            t,
            "operator",
            "op-2",
            step("text", b"u"),
            &lease,
        ))
        .await;
    assert_eq!(
        reply,
        json!({"status": "uncertain", "detail": "bytes may have landed"})
    );

    fx.native.script([Err(WriteFailure {
        stage: Stage::Partial,
        code: "host_failed".into(),
        detail: "half written".into(),
    })]);
    let reply = fx
        .route
        .handle(write_call(
            t,
            "operator",
            "op-3",
            step("text", b"p"),
            &lease,
        ))
        .await;
    assert_eq!(
        reply,
        json!({"status": "refusal", "stage": "partial", "code": "host_failed", "detail": "half written"})
    );

    fx.native.script([Err(WriteFailure {
        stage: Stage::None,
        code: "host_refused".into(),
        detail: "no bytes".into(),
    })]);
    let reply = fx
        .route
        .handle(write_call(
            t,
            "operator",
            "op-4",
            step("text", b"n"),
            &lease,
        ))
        .await;
    assert_eq!(
        reply,
        json!({"status": "refusal", "stage": "none", "code": "host_refused", "detail": "no bytes"})
    );

    let before = fx.native.payloads().len();
    let stale = json!({"attachment_id": Uuid::new_v4(), "generation": 1});
    let reply = fx
        .route
        .handle(write_call(
            t,
            "operator",
            "op-5",
            step("text", b"s"),
            &stale,
        ))
        .await;
    assert_eq!(refusal_code(&reply), "stale_lease");
    assert_eq!(reply["stage"], "none");
    assert_eq!(
        fx.native.payloads().len(),
        before,
        "a stale lease emits no bytes"
    );

    fx.route
        .handle(json!({"call": "retain_unresolved", "terminal_id": t, "action_key": "auto-1", "origin": "automatic"}))
        .await;
    let reply = fx
        .route
        .handle(write_call(
            t,
            "automatic",
            "auto-1",
            step("text", b"a"),
            &Value::Null,
        ))
        .await;
    assert_eq!(reply, json!({"status": "suppressed"}));

    fx.route
        .handle(json!({"call": "quarantine", "terminal_id": t, "action_key": "auto-1"}))
        .await;
    let reply = fx
        .route
        .handle(write_call(
            t,
            "automatic",
            "auto-2",
            step("text", b"b"),
            &Value::Null,
        ))
        .await;
    assert_eq!(reply, json!({"status": "quarantined"}));

    let sequence = fx
        .route
        .handle(json!({
            "call": "run_sequence",
            "terminal_id": t,
            "action_key": "seq-ok",
            "origin": "operator",
            "lease": lease,
            "latch": false,
            "steps": [{"write": step("text", b"a")}, {"delay_ms": 0}, {"write": step("text", b"b")}],
        }))
        .await;
    assert_eq!(
        sequence,
        json!({"failed_step": null, "outcome": {"status": "delivered"}})
    );

    fx.native.script([
        Ok(Delivery::Delivered),
        Ok(Delivery::Uncertain {
            detail: "second bytes unknown".into(),
        }),
    ]);
    let sequence = fx
        .route
        .handle(json!({
            "call": "run_sequence",
            "terminal_id": t,
            "action_key": "seq-uncertain",
            "origin": "operator",
            "lease": lease,
            "latch": false,
            "steps": [{"write": step("text", b"a")}, {"delay_ms": 0}, {"write": step("text", b"b")}, {"write": step("text", b"c")}],
        }))
        .await;
    assert_eq!(
        sequence,
        json!({"failed_step": 2, "outcome": {"status": "uncertain", "detail": "second bytes unknown"}})
    );
    Ok(())
}

#[tokio::test]
async fn held_action_binds_token_row_and_lease_generation() -> anyhow::Result<()> {
    let fx = fixture().await?;
    let t = fx.terminal;
    let (attachment, lease) = claim(&fx.leases, t).await?;

    let held = fx
        .route
        .handle(json!({"call": "acquire_action", "terminal_id": t, "lease": lease}))
        .await;
    assert_eq!(held["status"], "held", "{held}");
    let hold = held["hold"].clone();

    let reply = fx
        .route
        .handle(held_write(t, "held-1", step("text", b"a"), &lease, &hold))
        .await;
    assert_eq!(reply, json!({"status": "delivered"}));

    fx.leases.release_control(attachment).await?;
    let retaken = fx.leases.take_control(attachment, true).await?;
    anyhow::ensure!(retaken.granted, "control regranted");
    let current = json!({"attachment_id": attachment.id, "generation": retaken.generation});
    let before = fx.native.payloads().len();
    let reply = fx
        .route
        .handle(held_write(t, "held-2", step("text", b"b"), &lease, &hold))
        .await;
    assert_eq!(refusal_code(&reply), "stale_lease", "{reply}");
    assert_eq!(fx.native.payloads().len(), before);

    fx.hub.bump_attempt_generation(t).await?;
    let reply = fx
        .route
        .handle(held_write(t, "held-3", step("text", b"c"), &current, &hold))
        .await;
    assert_eq!(refusal_code(&reply), "stale_hold", "{reply}");
    assert_eq!(fx.native.payloads().len(), before);

    let _rotated = fx.token.install("token-2".into());
    let reply = fx
        .route
        .handle(held_write(t, "held-4", step("text", b"d"), &current, &hold))
        .await;
    assert_eq!(refusal_code(&reply), "stale_hold", "{reply}");
    assert_eq!(fx.native.payloads().len(), before);
    Ok(())
}

#[tokio::test]
async fn token_rotation_and_lost_replies_never_replay_writes() -> anyhow::Result<()> {
    let fx = fixture().await?;
    let t = fx.terminal;
    let (_attachment, lease) = claim(&fx.leases, t).await?;
    let held = fx
        .route
        .handle(json!({"call": "acquire_action", "terminal_id": t, "lease": lease}))
        .await;
    let hold = held["hold"].clone();

    let busy = fx
        .route
        .handle(json!({"call": "acquire_action", "terminal_id": t, "lease": lease}))
        .await;
    assert_eq!(refusal_code(&busy), "hold_busy", "{busy}");

    // The caller goes away while the admitted write is inside the runtime.
    let (entered, release) = fx.native.hold_next();
    let route = fx.route.clone();
    let call = held_write(t, "lost-1", step("text", b"d"), &lease, &hold);
    let caller = tokio::spawn(async move { route.handle(call).await });
    entered.await?;
    caller.abort();
    let _ = caller.await;
    release.send(()).expect("runtime still waiting");
    wait_idle(&fx.route).await;

    // A replayed step never reaches the runtime again.
    let replay = fx
        .route
        .handle(held_write(t, "lost-1", step("text", b"d"), &lease, &hold))
        .await;
    assert_ne!(replay["status"], "delivered", "{replay}");
    assert_eq!(fx.native.payloads(), vec!["d".to_string()]);

    // Uncertainty holds the terminal's action gate until it is resolved.
    let busy = fx
        .route
        .handle(json!({"call": "acquire_action", "terminal_id": t, "lease": lease}))
        .await;
    assert_eq!(refusal_code(&busy), "hold_busy", "{busy}");

    // A rotated token invalidates every hold it issued.
    let _rotated = fx.token.install("token-2".into());
    let released = fx
        .route
        .handle(json!({"call": "release_action", "hold": hold}))
        .await;
    assert_eq!(refusal_code(&released), "stale_hold", "{released}");
    Ok(())
}

#[tokio::test(flavor = "current_thread")]
async fn route_admits_each_operation_maximum_and_refuses_over_limit_steps() -> anyhow::Result<()> {
    let fx = fixture().await?;
    let t = fx.terminal;
    let (_attachment, lease) = claim(&fx.leases, t).await?;
    let paste_limit = 1024 * 1024;
    let input_limit = 64 * 1024;

    let reply = fx
        .route
        .handle(write_call(
            t,
            "operator",
            "paste-max",
            step("paste", &vec![b'p'; paste_limit]),
            &lease,
        ))
        .await;
    assert_eq!(reply, json!({"status": "delivered"}));
    let reply = fx
        .route
        .handle(write_call(
            t,
            "operator",
            "paste-over",
            step("paste", &vec![b'p'; paste_limit + 1]),
            &lease,
        ))
        .await;
    assert_eq!(refusal_code(&reply), "request_too_large", "{reply}");
    assert_eq!(reply["stage"], "none");

    let reply = fx
        .route
        .handle(write_call(
            t,
            "operator",
            "input-max",
            step("input", &vec![b'i'; input_limit]),
            &lease,
        ))
        .await;
    assert_eq!(reply, json!({"status": "delivered"}));
    let reply = fx
        .route
        .handle(write_call(
            t,
            "operator",
            "input-over",
            step("input", &vec![b'i'; input_limit + 1]),
            &lease,
        ))
        .await;
    assert_eq!(refusal_code(&reply), "request_too_large", "{reply}");

    // Plan approval sends C-r as two single-character inputs, then a literal.
    let sequence = fx
        .route
        .handle(json!({
            "call": "run_sequence",
            "terminal_id": t,
            "action_key": "plan-approval",
            "origin": "operator",
            "lease": lease,
            "latch": false,
            "steps": [{"write": step("input", b"\x12")}, {"write": step("input", b"y")}],
        }))
        .await;
    assert_eq!(
        sequence,
        json!({"failed_step": null, "outcome": {"status": "delivered"}})
    );

    let before = fx.native.payloads().len();
    let undecodable = json!({
        "call": "write",
        "terminal_id": t,
        "action_key": "bad",
        "origin": "operator",
        "step": {"kind": "text", "payload": "!!!", "submit": false},
        "lease": lease,
    });
    let reply = fx.route.handle(undecodable).await;
    assert_eq!(refusal_code(&reply), "undecodable_payload", "{reply}");
    assert_eq!(reply["stage"], "none");
    assert_eq!(
        fx.native.payloads().len(),
        before,
        "refused before the coordinator"
    );

    let oversized_text = "x".repeat(1_600_000);
    let reply = fx
        .route
        .handle(write_call(
            t,
            "operator",
            "text-over",
            step("text", oversized_text.as_bytes()),
            &lease,
        ))
        .await;
    assert_eq!(refusal_code(&reply), "request_too_large", "{reply}");
    assert_eq!(fx.native.payloads().len(), before);

    // Held-queue accounting keeps the 256 KiB bound. Four 64 KiB unheld writes
    // queue behind the hold; the fifth is refused at once.
    let held = fx
        .route
        .handle(json!({"call": "acquire_action", "terminal_id": t, "lease": lease}))
        .await;
    let hold = held["hold"].clone();
    let mut queued = Vec::new();
    for index in 0..4 {
        let route = fx.route.clone();
        let call = write_call(
            t,
            "operator",
            &format!("queued-{index}"),
            step("input", &vec![b'q'; input_limit]),
            &lease,
        );
        queued.push(tokio::spawn(async move { route.handle(call).await }));
        tokio::task::yield_now().await;
    }
    let reply = fx
        .route
        .handle(write_call(
            t,
            "operator",
            "queued-over",
            step("input", &vec![b'q'; input_limit]),
            &lease,
        ))
        .await;
    assert_eq!(refusal_code(&reply), "queue_full", "{reply}");

    let released = fx
        .route
        .handle(json!({"call": "release_action", "hold": hold}))
        .await;
    assert_eq!(released, json!({"status": "released"}));
    for queued in queued {
        assert_eq!(queued.await?, json!({"status": "delivered"}));
    }
    Ok(())
}

// Regression pin, not RED: the control-line bound already held at the control
// and native layers (control.rs, native_write.rs, write_runtimes.rs). The route
// must keep that refusal before any runtime and leave the terminal usable.
#[tokio::test]
async fn oversized_control_request_writes_zero_bytes_and_keeps_the_slot() -> anyhow::Result<()> {
    let fx = fixture().await?;
    let t = fx.terminal;
    let (_attachment, lease) = claim(&fx.leases, t).await?;

    let oversized = "A".repeat(gobby_terminals::control::MAX_CONTROL_LINE + 1);
    let reply = fx
        .route
        .handle(write_call(
            t,
            "operator",
            "oversized",
            json!({"kind": "text", "payload": oversized, "submit": false}),
            &lease,
        ))
        .await;
    assert_eq!(refusal_code(&reply), "request_too_large", "{reply}");
    assert_eq!(reply["stage"], "none");
    assert!(
        fx.native.payloads().is_empty(),
        "an oversized control request writes no bytes"
    );

    let reply = fx
        .route
        .handle(write_call(
            t,
            "operator",
            "after",
            step("text", b"ok"),
            &lease,
        ))
        .await;
    assert_eq!(reply, json!({"status": "delivered"}));
    assert_eq!(fx.native.payloads(), vec!["ok".to_string()]);
    Ok(())
}

#[tokio::test]
async fn queued_write_is_admitted_when_an_idle_hold_lapses() -> anyhow::Result<()> {
    let fx = fixture().await?;
    let t = fx.terminal;
    let (_attachment, lease) = claim(&fx.leases, t).await?;
    let held = fx
        .route
        .handle(json!({"call": "acquire_action", "terminal_id": t, "lease": lease}))
        .await;
    assert_eq!(held["status"], "held", "{held}");

    // The holder never returns, so only the hold's lapse can admit this write.
    let route = fx.route.clone();
    let call = write_call(t, "operator", "waits", step("text", b"q"), &lease);
    let waiter = tokio::spawn(async move { route.handle(call).await });
    let reply = tokio::time::timeout(gobby_terminals::hold::HOLD_TTL * 3, waiter).await??;
    assert_eq!(reply, json!({"status": "delivered"}));
    assert_eq!(fx.native.payloads(), vec!["q".to_string()]);
    Ok(())
}
