//! Native ownership cutover: route-mode latch, activation gate, and the
//! per-terminal action hold that admits one host effect at a time.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use gobby_core::bootstrap::RouteBackend;
use gobby_terminals::family::{
    RouteLatch, RouteModeError, SupervisorMode, TerminalFamily, WsRoutes,
};
use gobby_terminals::hold::{
    ActionGate, HOLD_TTL, HoldError, Proof, QUEUE_MAX_AGE, QUEUE_MAX_BYTES, QUEUE_MAX_ITEMS,
    Resolution, UncertainCause,
};
use gobby_terminals::host::{EpochAuthority, EpochBinding, HostFailure, HostOptions};
use gobby_terminals::write_api::{TokenSlot, WriteRefused};
use tokio::time::Instant;

const ROUTES: [RouteBackend; 3] = [
    RouteBackend::Proxy,
    RouteBackend::Native,
    RouteBackend::Compare,
];

struct NoBindings;

impl EpochAuthority for NoBindings {
    fn bindings<'a>(
        &'a self,
        _epoch: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<EpochBinding>, HostFailure>> + Send + 'a>> {
        Box::pin(async { Ok(Vec::new()) })
    }
}

fn options() -> HostOptions {
    HostOptions {
        socket_dir: "/nonexistent/gterm".into(),
        binary_path: "/nonexistent/gterm/gterm".into(),
        machine_id: "00000000-0000-0000-0000-000000000000".into(),
        gobby_home: None,
        health_interval: Duration::from_secs(1),
        retry_ceiling: Duration::from_secs(1),
        shutdown_grace: Duration::from_secs(1),
        spawn_args: Vec::new(),
    }
}

fn owner_for(route: RouteBackend) -> SupervisorMode {
    if route == RouteBackend::Native {
        SupervisorMode::Owner
    } else {
        SupervisorMode::Observer
    }
}

#[test]
fn cutover_requires_restart_and_has_one_owner() {
    for route in ROUTES {
        let latch = RouteLatch::new(route, WsRoutes::Registered).unwrap();
        let expected = owner_for(route);
        assert_eq!(latch.mode(), expected);
        let family = TerminalFamily::new(&latch, options(), Arc::new(NoBindings));
        assert_eq!(family.mode(), expected);
        assert_eq!(latch.select(route), Ok(expected));
        for requested in ROUTES.into_iter().filter(|r| *r != route) {
            assert_eq!(
                latch.select(requested),
                Err(RouteModeError::RestartRequired {
                    started: route,
                    requested
                })
            );
            assert_eq!(latch.route(), route);
            assert_eq!(latch.mode(), expected);
        }
    }
}

#[test]
fn native_activation_refuses_without_the_terminal_family() {
    assert_eq!(
        RouteLatch::new(RouteBackend::Native, WsRoutes::Unregistered).err(),
        Some(RouteModeError::FamilyUnregistered)
    );
    for route in [RouteBackend::Proxy, RouteBackend::Compare] {
        let latch = RouteLatch::new(route, WsRoutes::Unregistered).unwrap();
        assert_eq!(latch.mode(), SupervisorMode::Observer);
        let family = TerminalFamily::new(&latch, options(), Arc::new(NoBindings));
        assert_eq!(family.mode(), SupervisorMode::Observer);
    }
}

#[test]
fn write_route_requires_the_per_start_supervisor_pipe_token() {
    let slot = TokenSlot::default();
    assert_eq!(slot.authorize(None), Err(WriteRefused::NoBackend));
    assert_eq!(
        slot.authorize(Some("first-start")),
        Err(WriteRefused::NoBackend)
    );

    let first = slot.install("first-start".to_owned());
    assert_eq!(slot.authorize(Some("first-start")), Ok(()));
    assert_eq!(slot.authorize(None), Err(WriteRefused::MissingToken));
    assert_eq!(slot.authorize(Some("")), Err(WriteRefused::MissingToken));
    assert_eq!(
        slot.authorize(Some("first-starT")),
        Err(WriteRefused::WrongToken)
    );
    assert_eq!(
        slot.authorize(Some("first-start-")),
        Err(WriteRefused::WrongToken)
    );

    let shared = slot.clone();
    let second = slot.install("second-start".to_owned());
    assert_eq!(
        shared.authorize(Some("first-start")),
        Err(WriteRefused::WrongToken)
    );
    assert_eq!(shared.authorize(Some("second-start")), Ok(()));

    drop(first);
    assert_eq!(shared.authorize(Some("second-start")), Ok(()));
    drop(second);
    assert_eq!(
        shared.authorize(Some("second-start")),
        Err(WriteRefused::NoBackend)
    );
}

#[test]
fn route_hold_queue_is_bounded_and_token_scoped() {
    let t0 = Instant::now();

    let mut gate = ActionGate::new();
    let hold = gate.acquire(t0).unwrap();
    for item in 0..QUEUE_MAX_ITEMS {
        gate.submit(item, 1, t0).unwrap();
    }
    assert_eq!(
        gate.submit(QUEUE_MAX_ITEMS, 1, t0),
        Err(HoldError::QueueFull {
            items: QUEUE_MAX_ITEMS,
            bytes: QUEUE_MAX_ITEMS
        })
    );
    assert_eq!(gate.queued(), QUEUE_MAX_ITEMS);
    assert_eq!(gate.next(t0), None);
    gate.release(hold, t0).unwrap();
    assert_eq!(gate.acquire(t0), Err(HoldError::Busy));
    let mut flushed = Vec::new();
    while let Some(admitted) = gate.next(t0) {
        assert_eq!(gate.next(t0), None);
        flushed.push(admitted.op);
        gate.finish(admitted.effect, t0).unwrap();
    }
    assert_eq!(flushed, (0..QUEUE_MAX_ITEMS).collect::<Vec<_>>());

    let mut gate = ActionGate::new();
    let _hold = gate.acquire(t0).unwrap();
    for item in 0..4 {
        gate.submit(item, QUEUE_MAX_BYTES / 4, t0).unwrap();
    }
    assert_eq!(
        gate.submit(4, 1, t0),
        Err(HoldError::QueueFull {
            items: 4,
            bytes: QUEUE_MAX_BYTES
        })
    );
    assert_eq!(gate.queued(), 4);

    let mut gate = ActionGate::new();
    let hold = gate.acquire(t0).unwrap();
    gate.submit("oldest", 1, t0).unwrap();
    for renew in [9, 18, 27] {
        let at = t0 + Duration::from_secs(renew);
        let step = gate.begin(hold, at).unwrap();
        gate.finish(step, at).unwrap();
    }
    gate.submit("young", 1, t0 + QUEUE_MAX_AGE - Duration::from_secs(1))
        .unwrap();
    assert_eq!(
        gate.submit("late", 1, t0 + QUEUE_MAX_AGE),
        Err(HoldError::QueueStale {
            waited: QUEUE_MAX_AGE
        })
    );
    assert_eq!(gate.queued(), 2);
    gate.release(hold, t0 + QUEUE_MAX_AGE).unwrap();
    let oldest = gate.next(t0 + QUEUE_MAX_AGE).unwrap();
    assert_eq!(oldest.op, "oldest");
    gate.finish(oldest.effect, t0 + QUEUE_MAX_AGE).unwrap();
    assert_eq!(gate.next(t0 + QUEUE_MAX_AGE).map(|a| a.op), Some("young"));

    let mut gate = ActionGate::new();
    let old = gate.acquire(t0).unwrap();
    let old_step = gate.begin(old, t0).unwrap();
    gate.finish(old_step, t0).unwrap();
    gate.release(old, t0).unwrap();
    let newer = gate.acquire(t0).unwrap();
    let newer_step = gate.begin(newer, t0).unwrap();
    assert_eq!(gate.finish(old_step, t0), Err(HoldError::StaleEffect));
    assert_eq!(gate.release(old, t0), Err(HoldError::SupersededToken));
    assert_eq!(gate.begin(old, t0), Err(HoldError::SupersededToken));
    gate.submit("unheld", 1, t0).unwrap();
    assert_eq!(gate.next(t0), None);
    gate.finish(newer_step, t0).unwrap();
    gate.release(newer, t0).unwrap();
    assert_eq!(gate.next(t0).map(|a| a.op), Some("unheld"));
}

#[test]
fn uncertain_hold_admits_nothing_before_proof() {
    let t0 = Instant::now();
    let later = t0 + QUEUE_MAX_AGE * 2;

    let mut gate = ActionGate::new();
    let first = gate.acquire(t0).unwrap();
    let write = gate.begin(first, t0).unwrap();
    gate.submit("queued", 1, t0).unwrap();
    assert_eq!(gate.next(t0 + HOLD_TTL), None);
    assert_eq!(
        gate.uncertainty(),
        Some((write, UncertainCause::TtlExpired))
    );
    assert_eq!(
        gate.begin(first, t0 + HOLD_TTL),
        Err(HoldError::SupersededToken)
    );
    assert_eq!(gate.acquire(t0 + HOLD_TTL), Err(HoldError::Busy));
    assert_eq!(gate.next(later), None);
    assert_eq!(
        gate.prove(Proof::HostEpochChanged),
        Some(Resolution {
            effect: write,
            proof: Proof::HostEpochChanged
        })
    );
    assert_eq!(gate.uncertainty(), None);
    let flushed = gate.next(later).unwrap();
    assert_eq!(flushed.op, "queued");
    assert_ne!(flushed.effect, write);
    assert_eq!(gate.finish(write, later), Err(HoldError::StaleEffect));
    gate.finish(flushed.effect, later).unwrap();
    let newer = gate.acquire(later).unwrap();
    assert_eq!(gate.finish(write, later), Err(HoldError::StaleEffect));
    assert_eq!(gate.release(first, later), Err(HoldError::SupersededToken));
    let newer_step = gate.begin(newer, later).unwrap();
    gate.finish(newer_step, later).unwrap();
    assert_eq!(gate.next(later), None);
    assert_eq!(gate.queued(), 0);

    for (cause, proof) in [
        (UncertainCause::Timeout, Proof::PaneDestroyed),
        (UncertainCause::Disconnected, Proof::TerminalDestroyed),
        (UncertainCause::Timeout, Proof::HostExited),
        (UncertainCause::Disconnected, Proof::OperatorClose),
        (UncertainCause::Disconnected, Proof::OperatorKill),
    ] {
        let mut gate = ActionGate::new();
        let hold = gate.acquire(t0).unwrap();
        let write = gate.begin(hold, t0).unwrap();
        gate.submit("next", 1, t0).unwrap();
        gate.uncertain(write, cause).unwrap();
        assert_eq!(gate.uncertainty(), Some((write, cause)));
        assert_eq!(gate.next(later), None);
        assert_eq!(gate.prove(proof).map(|r| r.effect), Some(write));
        assert_eq!(gate.next(later).map(|a| a.op), Some("next"));
    }

    let mut gate = ActionGate::new();
    let hold = gate.acquire(t0).unwrap();
    let write = gate.begin(hold, t0).unwrap();
    gate.submit("after completion", 1, t0).unwrap();
    gate.uncertain(write, UncertainCause::Timeout).unwrap();
    assert_eq!(gate.next(t0), None);
    gate.finish(write, t0).unwrap();
    assert_eq!(gate.uncertainty(), None);
    assert_eq!(gate.next(t0).map(|a| a.op), Some("after completion"));
}
