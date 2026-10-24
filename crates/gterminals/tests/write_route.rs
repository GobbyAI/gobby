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
    BatchResult, BatchRuntime, BatchTarget, BoxFuture, Coordinator, Delivery, Runtime, Runtimes,
    Stage, Step, WriteFailure,
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
    panic_next: StdMutex<bool>,
    batch: FakeBatch,
}

/// The native host's atomic batch: records every operation it is asked to send.
#[derive(Default)]
struct FakeBatch {
    payloads: StdMutex<Vec<String>>,
}

impl BatchRuntime for FakeBatch {
    fn write_batch(
        &self,
        targets: Vec<BatchTarget>,
    ) -> BoxFuture<'_, Result<Vec<BatchResult>, String>> {
        Box::pin(async move {
            let mut results = Vec::new();
            for target in targets {
                for operation in &target.operations {
                    self.payloads
                        .lock()
                        .unwrap()
                        .push(operation.payload.clone());
                }
                results.push(BatchResult {
                    result_id: target.result_id,
                    outcome: Ok(Delivery::Delivered),
                });
            }
            Ok(results)
        })
    }
}

impl FakeRuntime {
    fn script(&self, outcomes: impl IntoIterator<Item = Scripted>) {
        self.script.lock().unwrap().extend(outcomes);
    }

    /// The next write panics inside the runtime, after it is recorded.
    fn arm_panic(&self) {
        *self.panic_next.lock().unwrap() = true;
    }

    fn batch_payloads(&self) -> Vec<String> {
        self.batch.payloads.lock().unwrap().clone()
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
    fn batch(&self) -> Option<&dyn BatchRuntime> {
        Some(&self.batch)
    }

    fn write<'a>(&'a self, _terminal: &'a Terminal, step: &'a Step) -> BoxFuture<'a, Scripted> {
        Box::pin(async move {
            self.payloads.lock().unwrap().push(step.payload.clone());
            let panic = std::mem::take(&mut *self.panic_next.lock().unwrap());
            assert!(!panic, "the runtime panicked inside a write");
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

impl Fixture {
    /// The token in force now, as the door would have admitted a caller under it.
    fn authority(&self) -> Arc<str> {
        self.token.current().expect("a token is installed")
    }
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
        .handle(
            &fx.authority(),
            write_call(t, "operator", "op-1", step("text", b"hi"), &lease),
        )
        .await;
    assert_eq!(reply, json!({"status": "delivered"}));

    fx.native.script([Ok(Delivery::Uncertain {
        detail: "bytes may have landed".into(),
    })]);
    let reply = fx
        .route
        .handle(
            &fx.authority(),
            write_call(t, "operator", "op-2", step("text", b"u"), &lease),
        )
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
        .handle(
            &fx.authority(),
            write_call(t, "operator", "op-3", step("text", b"p"), &lease),
        )
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
        .handle(
            &fx.authority(),
            write_call(t, "operator", "op-4", step("text", b"n"), &lease),
        )
        .await;
    assert_eq!(
        reply,
        json!({"status": "refusal", "stage": "none", "code": "host_refused", "detail": "no bytes"})
    );

    let before = fx.native.payloads().len();
    let stale = json!({"attachment_id": Uuid::new_v4(), "generation": 1});
    let reply = fx
        .route
        .handle(
            &fx.authority(),
            write_call(t, "operator", "op-5", step("text", b"s"), &stale),
        )
        .await;
    assert_eq!(refusal_code(&reply), "stale_lease");
    assert_eq!(reply["stage"], "none");
    assert_eq!(
        fx.native.payloads().len(),
        before,
        "a stale lease emits no bytes"
    );

    fx.route
        .handle(&fx.authority(), json!({"call": "retain_unresolved", "terminal_id": t, "action_key": "auto-1", "origin": "automatic"}))
        .await;
    let reply = fx
        .route
        .handle(
            &fx.authority(),
            write_call(t, "automatic", "auto-1", step("text", b"a"), &Value::Null),
        )
        .await;
    assert_eq!(reply, json!({"status": "suppressed"}));

    fx.route
        .handle(
            &fx.authority(),
            json!({"call": "quarantine", "terminal_id": t, "action_key": "auto-1"}),
        )
        .await;
    let reply = fx
        .route
        .handle(
            &fx.authority(),
            write_call(t, "automatic", "auto-2", step("text", b"b"), &Value::Null),
        )
        .await;
    assert_eq!(reply, json!({"status": "quarantined"}));

    let sequence = fx
        .route
        .handle(&fx.authority(), json!({
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
        .handle(&fx.authority(), json!({
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
        .handle(
            &fx.authority(),
            json!({"call": "acquire_action", "terminal_id": t, "lease": lease}),
        )
        .await;
    assert_eq!(held["status"], "held", "{held}");
    let hold = held["hold"].clone();

    let reply = fx
        .route
        .handle(
            &fx.authority(),
            held_write(t, "held-1", step("text", b"a"), &lease, &hold),
        )
        .await;
    assert_eq!(reply, json!({"status": "delivered"}));

    fx.leases.release_control(attachment).await?;
    let retaken = fx.leases.take_control(attachment, true).await?;
    anyhow::ensure!(retaken.granted, "control regranted");
    let current = json!({"attachment_id": attachment.id, "generation": retaken.generation});
    let before = fx.native.payloads().len();
    let reply = fx
        .route
        .handle(
            &fx.authority(),
            held_write(t, "held-2", step("text", b"b"), &lease, &hold),
        )
        .await;
    assert_eq!(refusal_code(&reply), "stale_lease", "{reply}");
    assert_eq!(fx.native.payloads().len(), before);

    // Each axis gets its own fresh hold, so a refusal can only come from that axis.
    let released = fx
        .route
        .handle(
            &fx.authority(),
            json!({"call": "release_action", "hold": hold}),
        )
        .await;
    assert_eq!(released, json!({"status": "released"}), "{released}");
    let fresh = fx
        .route
        .handle(
            &fx.authority(),
            json!({"call": "acquire_action", "terminal_id": t, "lease": current}),
        )
        .await;
    assert_eq!(fresh["status"], "held", "{fresh}");
    let row_hold = fresh["hold"].clone();
    fx.hub.bump_attempt_generation(t).await?;
    let reply = fx
        .route
        .handle(
            &fx.authority(),
            held_write(t, "held-3", step("text", b"c"), &current, &row_hold),
        )
        .await;
    assert_eq!(refusal_code(&reply), "stale_hold", "{reply}");
    assert_eq!(fx.native.payloads().len(), before);

    let released = fx
        .route
        .handle(
            &fx.authority(),
            json!({"call": "release_action", "hold": row_hold}),
        )
        .await;
    assert_eq!(released, json!({"status": "released"}), "{released}");
    let fresh = fx
        .route
        .handle(
            &fx.authority(),
            json!({"call": "acquire_action", "terminal_id": t, "lease": current}),
        )
        .await;
    assert_eq!(fresh["status"], "held", "{fresh}");
    let token_hold = fresh["hold"].clone();
    let _rotated = fx.token.install("token-2".into());
    let reply = fx
        .route
        .handle(
            &fx.authority(),
            held_write(t, "held-4", step("text", b"d"), &current, &token_hold),
        )
        .await;
    assert_eq!(refusal_code(&reply), "stale_hold", "{reply}");
    assert_eq!(fx.native.payloads().len(), before);
    Ok(())
}

// A hold binds the registry's control state, not the lease claim, so a lease-less
// logical hold still refuses automatic writes once control moves.
#[tokio::test]
async fn a_logical_hold_binds_the_registry_snapshot_for_every_origin() -> anyhow::Result<()> {
    let fx = fixture().await?;
    let t = fx.terminal;
    let (_attachment, _lease) = claim(&fx.leases, t).await?;
    let held = fx
        .route
        .handle(
            &fx.authority(),
            json!({"call": "acquire_action", "terminal_id": t}),
        )
        .await;
    assert_eq!(held["status"], "held", "{held}");

    let mut automatic = write_call(
        t,
        "automatic",
        "held-auto",
        step("text", b"a"),
        &Value::Null,
    );
    automatic["hold"] = held["hold"].clone();
    let reply = fx.route.handle(&fx.authority(), automatic.clone()).await;
    assert_eq!(reply, json!({"status": "delivered"}), "{reply}");

    let other = fx
        .leases
        .attach(
            t,
            Uuid::new_v4(),
            Viewer::Web,
            Backend::Native,
            leases::Delivery::Proxy,
        )
        .await?;
    let taken = fx.leases.take_control(other, true).await?;
    anyhow::ensure!(taken.granted, "control taken by another attachment");
    let reply = fx.route.handle(&fx.authority(), automatic).await;
    assert_eq!(refusal_code(&reply), "stale_lease", "{reply}");
    assert_eq!(fx.native.payloads(), vec!["a".to_string()]);
    Ok(())
}

#[tokio::test]
async fn token_rotation_and_lost_replies_never_replay_writes() -> anyhow::Result<()> {
    let fx = fixture().await?;
    let t = fx.terminal;
    let (_attachment, lease) = claim(&fx.leases, t).await?;
    let held = fx
        .route
        .handle(
            &fx.authority(),
            json!({"call": "acquire_action", "terminal_id": t, "lease": lease}),
        )
        .await;
    let hold = held["hold"].clone();

    let busy = fx
        .route
        .handle(
            &fx.authority(),
            json!({"call": "acquire_action", "terminal_id": t, "lease": lease}),
        )
        .await;
    assert_eq!(refusal_code(&busy), "hold_busy", "{busy}");

    // The caller goes away while the admitted write is inside the runtime.
    let (entered, release) = fx.native.hold_next();
    let route = fx.route.clone();
    let call = held_write(t, "lost-1", step("text", b"d"), &lease, &hold);
    let authority = fx.authority();
    let caller = tokio::spawn(async move { route.handle(&authority, call).await });
    entered.await?;
    caller.abort();
    let _ = caller.await;
    release.send(()).expect("runtime still waiting");
    wait_idle(&fx.route).await;

    // A replayed step never reaches the runtime again.
    let replay = fx
        .route
        .handle(
            &fx.authority(),
            held_write(t, "lost-1", step("text", b"d"), &lease, &hold),
        )
        .await;
    assert_ne!(replay["status"], "delivered", "{replay}");
    assert_eq!(fx.native.payloads(), vec!["d".to_string()]);

    // Uncertainty holds the terminal's action gate until it is resolved.
    let busy = fx
        .route
        .handle(
            &fx.authority(),
            json!({"call": "acquire_action", "terminal_id": t, "lease": lease}),
        )
        .await;
    assert_eq!(refusal_code(&busy), "hold_busy", "{busy}");

    // A rotated token invalidates every hold it issued.
    let _rotated = fx.token.install("token-2".into());
    let released = fx
        .route
        .handle(
            &fx.authority(),
            json!({"call": "release_action", "hold": hold}),
        )
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
        .handle(
            &fx.authority(),
            write_call(
                t,
                "operator",
                "paste-max",
                step("paste", &vec![b'p'; paste_limit]),
                &lease,
            ),
        )
        .await;
    assert_eq!(reply, json!({"status": "delivered"}));
    let reply = fx
        .route
        .handle(
            &fx.authority(),
            write_call(
                t,
                "operator",
                "paste-over",
                step("paste", &vec![b'p'; paste_limit + 1]),
                &lease,
            ),
        )
        .await;
    assert_eq!(refusal_code(&reply), "request_too_large", "{reply}");
    assert_eq!(reply["stage"], "none");

    let reply = fx
        .route
        .handle(
            &fx.authority(),
            write_call(
                t,
                "operator",
                "input-max",
                step("input", &vec![b'i'; input_limit]),
                &lease,
            ),
        )
        .await;
    assert_eq!(reply, json!({"status": "delivered"}));
    let reply = fx
        .route
        .handle(
            &fx.authority(),
            write_call(
                t,
                "operator",
                "input-over",
                step("input", &vec![b'i'; input_limit + 1]),
                &lease,
            ),
        )
        .await;
    assert_eq!(refusal_code(&reply), "request_too_large", "{reply}");

    // Plan approval sends C-r as two single-character inputs, then a literal.
    let sequence = fx
        .route
        .handle(
            &fx.authority(),
            json!({
                "call": "run_sequence",
                "terminal_id": t,
                "action_key": "plan-approval",
                "origin": "operator",
                "lease": lease,
                "latch": false,
                "steps": [{"write": step("input", b"\x12")}, {"write": step("input", b"y")}],
            }),
        )
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
    let reply = fx.route.handle(&fx.authority(), undecodable).await;
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
        .handle(
            &fx.authority(),
            write_call(
                t,
                "operator",
                "text-over",
                step("text", oversized_text.as_bytes()),
                &lease,
            ),
        )
        .await;
    assert_eq!(refusal_code(&reply), "request_too_large", "{reply}");
    assert_eq!(fx.native.payloads().len(), before);

    // Held-queue accounting keeps the 256 KiB bound. Four 64 KiB unheld writes
    // queue behind the hold; the fifth is refused at once.
    let held = fx
        .route
        .handle(
            &fx.authority(),
            json!({"call": "acquire_action", "terminal_id": t, "lease": lease}),
        )
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
        let authority = fx.authority();
        queued.push(tokio::spawn(
            async move { route.handle(&authority, call).await },
        ));
        tokio::task::yield_now().await;
    }
    let reply = fx
        .route
        .handle(
            &fx.authority(),
            write_call(
                t,
                "operator",
                "queued-over",
                step("input", &vec![b'q'; input_limit]),
                &lease,
            ),
        )
        .await;
    assert_eq!(refusal_code(&reply), "queue_full", "{reply}");

    let released = fx
        .route
        .handle(
            &fx.authority(),
            json!({"call": "release_action", "hold": hold}),
        )
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
        .handle(
            &fx.authority(),
            write_call(
                t,
                "operator",
                "oversized",
                json!({"kind": "text", "payload": oversized, "submit": false}),
                &lease,
            ),
        )
        .await;
    assert_eq!(refusal_code(&reply), "request_too_large", "{reply}");
    assert_eq!(reply["stage"], "none");
    assert!(
        fx.native.payloads().is_empty(),
        "an oversized control request writes no bytes"
    );

    let reply = fx
        .route
        .handle(
            &fx.authority(),
            write_call(t, "operator", "after", step("text", b"ok"), &lease),
        )
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
        .handle(
            &fx.authority(),
            json!({"call": "acquire_action", "terminal_id": t, "lease": lease}),
        )
        .await;
    assert_eq!(held["status"], "held", "{held}");

    // The holder never returns, so only the hold's lapse can admit this write.
    let route = fx.route.clone();
    let call = write_call(t, "operator", "waits", step("text", b"q"), &lease);
    let authority = fx.authority();
    let waiter = tokio::spawn(async move { route.handle(&authority, call).await });
    let reply = tokio::time::timeout(gobby_terminals::hold::HOLD_TTL * 3, waiter).await??;
    assert_eq!(reply, json!({"status": "delivered"}));
    assert_eq!(fx.native.payloads(), vec!["q".to_string()]);
    Ok(())
}

// The token a caller was admitted under fences every queued effect: a rotation
// while the write waits refuses it before any byte is dispatched.
#[tokio::test]
async fn queued_writes_are_refused_once_their_token_retires() -> anyhow::Result<()> {
    let fx = fixture().await?;
    let t = fx.terminal;
    let (_attachment, lease) = claim(&fx.leases, t).await?;

    let (entered, release) = fx.native.hold_next();
    let route = fx.route.clone();
    let first = write_call(t, "operator", "in-flight", step("text", b"a"), &lease);
    let authority = fx.authority();
    let runner = tokio::spawn(async move { route.handle(&authority, first).await });
    entered.await?;

    let route = fx.route.clone();
    let queued = write_call(t, "automatic", "queued", step("text", b"b"), &Value::Null);
    let authority = fx.authority();
    let waiter = tokio::spawn(async move { route.handle(&authority, queued).await });
    tokio::time::sleep(Duration::from_millis(50)).await;

    let _rotated = fx.token.install("token-2".into());
    release.send(()).expect("runtime still waiting");
    assert_eq!(runner.await?, json!({"status": "delivered"}));
    let reply = waiter.await?;
    assert_eq!(refusal_code(&reply), "retired_token", "{reply}");
    assert_eq!(fx.native.payloads(), vec!["a".to_string()]);
    Ok(())
}

// A caller admitted under a retired token cannot write under a hold that the
// current token took: the fence follows the caller, not the hold.
#[tokio::test]
async fn a_held_write_from_a_retired_token_is_refused_under_the_current_hold() -> anyhow::Result<()>
{
    let fx = fixture().await?;
    let t = fx.terminal;
    let (_attachment, lease) = claim(&fx.leases, t).await?;
    let retired = fx.authority();
    let _rotated = fx.token.install("token-2".into());
    let current = fx.authority();

    let held = fx
        .route
        .handle(
            &current,
            json!({"call": "acquire_action", "terminal_id": t, "lease": lease}),
        )
        .await;
    assert_eq!(held["status"], "held", "{held}");

    let reply = fx
        .route
        .handle(
            &retired,
            held_write(t, "operator", step("text", b"x"), &lease, &held["hold"]),
        )
        .await;
    assert_eq!(refusal_code(&reply), "retired_token", "{reply}");
    assert!(fx.native.payloads().is_empty());
    Ok(())
}

// A delay can outlive the token the sequence was admitted under: the step after
// the delay is fenced, and the earlier delivered step stands.
#[tokio::test]
async fn a_delayed_sequence_step_is_refused_once_its_token_retires() -> anyhow::Result<()> {
    let fx = fixture().await?;
    let t = fx.terminal;
    let sequence = json!({
        "call": "run_sequence",
        "terminal_id": t,
        "action_key": "sequence",
        "origin": "automatic",
        "latch": true,
        "steps": [
            {"write": step("text", b"a")},
            {"delay_ms": 300},
            {"write": step("text", b"b")},
        ],
    });
    let route = fx.route.clone();
    let authority = fx.authority();
    let runner = tokio::spawn(async move { route.handle(&authority, sequence).await });
    for _ in 0..500 {
        if !fx.native.payloads().is_empty() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    let _rotated = fx.token.install("token-2".into());

    let reply = runner.await?;
    assert_eq!(reply["failed_step"], 2, "{reply}");
    assert_eq!(reply["outcome"]["code"], "retired_token", "{reply}");
    assert_eq!(fx.native.payloads(), vec!["a".to_string()]);
    Ok(())
}

// An abandoned caller's queued effect never dispatches, so it cannot wedge the gate.
#[tokio::test]
async fn an_abandoned_queued_write_never_reaches_the_runtime() -> anyhow::Result<()> {
    let fx = fixture().await?;
    let t = fx.terminal;
    let (_attachment, lease) = claim(&fx.leases, t).await?;

    let (entered, release) = fx.native.hold_next();
    let route = fx.route.clone();
    let first = write_call(t, "operator", "in-flight", step("text", b"a"), &lease);
    let authority = fx.authority();
    let runner = tokio::spawn(async move { route.handle(&authority, first).await });
    entered.await?;

    let route = fx.route.clone();
    let queued = write_call(t, "operator", "abandoned", step("text", b"b"), &lease);
    let authority = fx.authority();
    let caller = tokio::spawn(async move { route.handle(&authority, queued).await });
    tokio::time::sleep(Duration::from_millis(50)).await;
    caller.abort();
    let _ = caller.await;

    release.send(()).expect("runtime still waiting");
    assert_eq!(runner.await?, json!({"status": "delivered"}));
    wait_idle(&fx.route).await;
    assert_eq!(fx.native.payloads(), vec!["a".to_string()]);

    let reply = fx
        .route
        .handle(
            &fx.authority(),
            write_call(t, "operator", "later", step("text", b"c"), &lease),
        )
        .await;
    assert_eq!(reply, json!({"status": "delivered"}), "{reply}");
    assert_eq!(fx.native.payloads(), vec!["a".to_string(), "c".to_string()]);
    Ok(())
}

// Acquire waits behind admitted unheld work (plan 2.4: a second acquire waits).
#[tokio::test]
async fn acquire_waits_behind_unheld_work_in_flight() -> anyhow::Result<()> {
    let fx = fixture().await?;
    let t = fx.terminal;
    let (_attachment, lease) = claim(&fx.leases, t).await?;

    let (entered, release) = fx.native.hold_next();
    let route = fx.route.clone();
    let first = write_call(t, "operator", "in-flight", step("text", b"a"), &lease);
    let authority = fx.authority();
    let runner = tokio::spawn(async move { route.handle(&authority, first).await });
    entered.await?;

    let route = fx.route.clone();
    let acquire = json!({"call": "acquire_action", "terminal_id": t, "lease": lease});
    let authority = fx.authority();
    let acquirer = tokio::spawn(async move { route.handle(&authority, acquire).await });
    tokio::time::sleep(Duration::from_millis(50)).await;
    release.send(()).expect("runtime still waiting");
    assert_eq!(runner.await?, json!({"status": "delivered"}));

    let held = acquirer.await?;
    assert_eq!(held["status"], "held", "{held}");
    Ok(())
}

// The lifecycle and wake calls return typed outcomes, and a wake whose token
// retired is refused before its batch dispatches.
#[tokio::test]
async fn wake_and_lifecycle_calls_return_typed_outcomes() -> anyhow::Result<()> {
    let fx = fixture().await?;
    let t = fx.terminal;
    let wake = json!({
        "call": "run_wake_batch",
        "requests": [{
            "result_id": "wake-1",
            "terminal_id": t,
            "clear_action_key": "wake-clear:t",
            "wake_action_key": "wake:t",
            "operations": [{"kind": "text", "payload": "hi", "delay_ms": 0}],
        }],
    });
    let reply = fx.route.handle(&fx.authority(), wake.clone()).await;
    assert_eq!(reply["results"][0]["result_id"], "wake-1", "{reply}");
    assert_eq!(
        reply["results"][0]["outcome"],
        json!({"status": "delivered"}),
        "{reply}"
    );
    assert_eq!(
        fx.native.batch_payloads().first().map(String::as_str),
        Some("hi")
    );

    let typed = [
        json!({"call": "retain_unresolved", "terminal_id": t, "action_key": "k", "origin": "automatic"}),
        json!({"call": "observe_resolved", "terminal_id": t, "action_key": "k"}),
        json!({"call": "observe_operator_input", "terminal_id": t}),
        json!({"call": "clear_on_exit", "terminal_id": t}),
    ];
    for call in typed {
        let reply = fx.route.handle(&fx.authority(), call.clone()).await;
        assert_eq!(reply, json!({"status": "ok"}), "{call} -> {reply}");
    }

    let old = fx.authority();
    let _rotated = fx.token.install("token-2".into());
    let reply = fx.route.handle(&old, wake).await;
    assert_eq!(refusal_code(&reply), "retired_token", "{reply}");
    assert_eq!(fx.native.batch_payloads().len(), 1);
    Ok(())
}

/// Waits until the wake's latch is persisted, which happens after admission.
async fn await_latch(fx: &Fixture, key: &str) -> anyhow::Result<()> {
    for _ in 0..1000 {
        if fx
            .hub
            .get(fx.terminal)
            .await?
            .unresolved_writes
            .get(key)
            .is_some()
        {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    anyhow::bail!("the latch {key} was never persisted")
}

/// Waits until the host has been submitted `payload`.
async fn await_payload(native: &FakeRuntime, payload: &str) -> anyhow::Result<()> {
    for _ in 0..1000 {
        if native.batch_payloads().iter().any(|p| p == payload) {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    anyhow::bail!("the host never received {payload}")
}

// An Enter-only wake sleeps through its pace before it submits. A token retired
// during that sleep must not send the Enter.
#[tokio::test]
async fn an_enter_only_wake_is_refused_when_its_token_retires_during_the_pace() -> anyhow::Result<()>
{
    let fx = fixture().await?;
    let t = fx.terminal;
    let wake = json!({
        "call": "run_wake_batch",
        "requests": [{
            "result_id": "wake-1",
            "terminal_id": t,
            "clear_action_key": "wake-clear:t",
            "wake_action_key": "wake:t",
            "operations": [{"kind": "key", "payload": "enter", "delay_ms": 300}],
        }],
    });
    let route = fx.route.clone();
    let authority = fx.authority();
    let runner = tokio::spawn(async move { route.handle(&authority, wake).await });
    // Admitted once the latch persists; the pace then sleeps for 300ms.
    await_latch(&fx, "wake:t").await?;
    tokio::time::sleep(Duration::from_millis(20)).await;
    let _rotated = fx.token.install("token-2".into());
    let reply = runner.await?;
    assert_eq!(
        reply["results"][0]["outcome"]["code"], "retired_token",
        "{reply}"
    );
    assert!(fx.native.batch_payloads().is_empty(), "{reply}");
    Ok(())
}

// The prelude reaches the composer before the pace; a token retired after that
// must refuse the Enter and keep the latch, since the text is already typed.
#[tokio::test]
async fn a_wake_retired_between_its_prelude_and_enter_keeps_the_partial_latch() -> anyhow::Result<()>
{
    let fx = fixture().await?;
    let t = fx.terminal;
    let wake = json!({
        "call": "run_wake_batch",
        "requests": [{
            "result_id": "wake-1",
            "terminal_id": t,
            "clear_action_key": "wake-clear:t",
            "wake_action_key": "wake:t",
            "operations": [
                {"kind": "text", "payload": "hi", "delay_ms": 0},
                {"kind": "key", "payload": "enter", "delay_ms": 300},
            ],
        }],
    });
    let route = fx.route.clone();
    let authority = fx.authority();
    let runner = tokio::spawn(async move { route.handle(&authority, wake).await });
    // The prelude has reached the host; the Enter waits out its 300ms pace.
    await_payload(&fx.native, "hi").await?;
    let _rotated = fx.token.install("token-2".into());
    let reply = runner.await?;
    let outcome = &reply["results"][0];
    assert_eq!(outcome["step"], "enter", "{reply}");
    assert_eq!(outcome["outcome"]["code"], "retired_token", "{reply}");
    assert_eq!(outcome["outcome"]["stage"], "partial", "{reply}");
    assert_eq!(fx.native.batch_payloads(), vec!["hi".to_string()]);
    assert!(
        fx.hub
            .get(t)
            .await?
            .unresolved_writes
            .get("wake:t")
            .is_some()
    );
    Ok(())
}

// A panicking runtime settles its effect as uncertain and releases the route's
// active count. The uncertain gate then holds until proof settles it (plan 2.8).
#[tokio::test]
async fn a_panicking_runtime_settles_its_effect_as_uncertain() -> anyhow::Result<()> {
    let fx = fixture().await?;
    let t = fx.terminal;
    let (_attachment, lease) = claim(&fx.leases, t).await?;

    fx.native.arm_panic();
    let reply = fx
        .route
        .handle(
            &fx.authority(),
            write_call(t, "operator", "boom", step("text", b"x"), &lease),
        )
        .await;
    assert_eq!(reply["status"], "uncertain", "{reply}");
    wait_idle(&fx.route).await;
    assert_eq!(fx.route.active(), 0);
    Ok(())
}
