//! `host_upgrade` (Decisions 9, 10, 13): one bounded attempt that pins and
//! probes a candidate image, takes the mutation gate's write side, quiesces
//! and captures every pane, and execs the candidate on the same pid. A failure
//! before exec rolls back in process; a rollback that cannot resume every pane
//! ends the host through SIGALRM.

use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex as StdMutex, OnceLock, PoisonError};
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};
use tokio::sync::{oneshot, Mutex, MutexGuard, OwnedMutexGuard, OwnedRwLockWriteGuard};

use super::handover::{
    encode_snapshot, monotonic_now_ns, write_state, CarriedIdentity, CarriedObserverBind,
    CarriedPane, CarriedReservation, HandoverState, UpgradeAttempt, UpgradeOutcome, UpgradeReason,
    UpgradeRecord, FORMAT_VERSION, STATE_FILE,
};
use super::helpers::{err, s};
use super::image::{self, PinnedImage};
use super::sigterm;
use super::state::{CommitState, HostState, ObserverBind};
use crate::pty::actor::{HandoffAckFault, PtyIoActorHandle};

/// Decision 10's budget, fixed when the attempt takes `upgrade_lock`.
const BUDGET: Duration = Duration::from_secs(15);
/// The soft cutoff sits this far before the deadline: two 1 s rollback
/// acknowledgements and cleanup.
const ROLLBACK_RESERVE: Duration = Duration::from_secs(3);
const PROBE_LIMIT: Duration = Duration::from_secs(5);
const POLL: Duration = Duration::from_millis(10);
const GATE_RETRY: Duration = Duration::from_millis(500);
/// `begin_handoff` spends up to this long rolling back when its wait times out.
const HANDOFF_ROLLBACK: Duration = Duration::from_secs(1);
/// How long an accepted attempt waits for its reply to be queued.
const REPLY_WAIT: Duration = Duration::from_secs(1);
/// Upper bounds on test-fault holds, so no fault is unbounded.
const FAULT_DELAY_CAP: Duration = Duration::from_secs(5);
const FAULT_STALL_CAP: Duration = Duration::from_secs(60);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Phase {
    Probing,
    Quiescing,
    Capturing,
    Exec,
    RollingBack,
}

impl Phase {
    fn as_str(self) -> &'static str {
        match self {
            Phase::Probing => "probing",
            Phase::Quiescing => "quiescing",
            Phase::Capturing => "capturing",
            Phase::Exec => "exec",
            Phase::RollingBack => "rolling_back",
        }
    }
}

/// The attempt in progress, reported by `ping` (Decision 13).
struct LiveAttempt {
    attempt_id: String,
    phase: Phase,
    candidate_sha256: Option<String>,
    deadline_ns: u64,
}

/// What `run()` knows that an attempt needs: the argv to exec with and the
/// listeners it carries.
pub(crate) struct UpgradeContext {
    argv: Vec<String>,
    socket_dir: PathBuf,
    control_fd: RawFd,
    frames_fd: RawFd,
}

impl UpgradeContext {
    /// The running argv without the resume flags a restored image ran with.
    pub(crate) fn new(socket_dir: PathBuf, control_fd: RawFd, frames_fd: RawFd) -> Self {
        let mut argv = Vec::new();
        let mut args = std::env::args();
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--resume-state" => {
                    args.next();
                }
                "--resume-fallback" => {}
                _ => argv.push(arg),
            }
        }
        Self {
            argv,
            socket_dir,
            control_fd,
            frames_fd,
        }
    }
}

/// `upgrade_lock` (Decision 9) and the attempt in progress.
#[derive(Default)]
pub(crate) struct Attempts {
    lock: Arc<Mutex<()>>,
    live: StdMutex<Option<LiveAttempt>>,
    context: OnceLock<UpgradeContext>,
}

impl Attempts {
    pub(crate) fn install(&self, context: UpgradeContext) {
        let _ = self.context.set(context);
    }

    /// Waits for an in-flight attempt to settle, then holds `upgrade_lock` so
    /// no attempt starts while the host tears down. An attempt ends within
    /// its own budget or through SIGALRM, so this adds no bound of its own.
    pub(crate) async fn settled(&self) -> MutexGuard<'_, ()> {
        self.lock.lock().await
    }

    fn update(&self, change: impl FnOnce(&mut LiveAttempt)) {
        if let Some(live) = self
            .live
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .as_mut()
        {
            change(live);
        }
    }
}

impl HostState {
    /// Adds `generation` and `upgrade` to `ping` (Decision 13), with explicit
    /// nulls when idle.
    pub(super) fn with_upgrade(&self, mut ping: Value) -> Value {
        ping["generation"] = json!(self.generation);
        ping["upgrade"] = upgrade_record(self);
        ping
    }
}

fn upgrade_record(state: &HostState) -> Value {
    let last = state
        .upgrade
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .as_ref()
        .and_then(outcome_json);
    let live = state
        .attempts
        .live
        .lock()
        .unwrap_or_else(PoisonError::into_inner);
    match live.as_ref() {
        Some(live) => json!({
            "attempt_id": live.attempt_id,
            "phase": live.phase.as_str(),
            "candidate_sha256": live.candidate_sha256,
            "remaining_ms": remaining(live.deadline_ns).as_millis() as u64,
            "last_outcome": last,
        }),
        None => json!({
            "attempt_id": null,
            "phase": "idle",
            "candidate_sha256": null,
            "remaining_ms": null,
            "last_outcome": last,
        }),
    }
}

fn outcome_json(record: &UpgradeRecord) -> Option<Value> {
    let (outcome, reason, errno) = match record.outcome? {
        UpgradeOutcome::Succeeded => ("succeeded", None, None),
        UpgradeOutcome::Fallback => ("fallback", None, None),
        UpgradeOutcome::Refused => ("refused", None, None),
        UpgradeOutcome::Deferred(reason) => ("deferred", Some(reason.as_str()), None),
        UpgradeOutcome::Aborted(reason) => ("aborted", Some(reason.as_str()), None),
        UpgradeOutcome::RolledBack { errno } => ("rolled_back", Some("exec_failed"), Some(errno)),
    };
    let sha = &record.attempt.candidate_sha256;
    let mut value = json!({
        "attempt_id": record.attempt.attempt_id,
        "outcome": outcome,
        "candidate_sha256": (!sha.is_empty()).then_some(sha),
        "reason": reason,
    });
    if let Some(errno) = errno {
        value["errno"] = json!(errno);
    }
    Some(value)
}

fn remaining(deadline_ns: u64) -> Duration {
    Duration::from_nanos(deadline_ns.saturating_sub(monotonic_now_ns()))
}

/// Time left before the soft cutoff, which keeps `ROLLBACK_RESERVE` for a
/// rollback; exec must not start once it reaches zero.
fn until_soft_cutoff(deadline_ns: u64) -> Duration {
    remaining(deadline_ns).saturating_sub(ROLLBACK_RESERVE)
}

/// Admission (Decision 9): a host that is not draining, holds no unconsumed
/// reservation, and whose every prepared reservation names a committed pane.
async fn admission(state: &HostState) -> Result<(), UpgradeReason> {
    if state.draining.load(Ordering::SeqCst) {
        return Err(UpgradeReason::HostDraining);
    }
    let inner = state.inner.lock().await;
    let pending = inner.reservations.values().any(|reservation| {
        !reservation.prepared
            || reservation.identity.as_ref().is_none_or(|identity| {
                inner
                    .terminals
                    .get(identity)
                    .is_none_or(|slot| slot.commit_state != CommitState::Committed)
            })
    });
    if pending {
        return Err(UpgradeReason::HostBusy);
    }
    Ok(())
}

/// Decision 10's hard bound: the default SIGALRM action ends a wedged attempt.
fn arm_alarm() {
    // SAFETY: restoring SIGALRM's default action and scheduling it touch only
    // this process's signal state.
    unsafe {
        libc::signal(libc::SIGALRM, libc::SIG_DFL);
        libc::alarm(BUDGET.as_secs() as libc::c_uint);
    }
}

fn disarm_alarm() {
    // SAFETY: alarm(0) only cancels a pending alarm.
    unsafe { libc::alarm(0) };
}

/// Test-only faults, honored only under `GTERM_TEST_HELPER=1` and scoped to
/// this attempt.
#[derive(Default)]
struct Faults {
    hold_accepted: Duration,
    refuse_probe: bool,
    quiesce_ack: Option<(String, AckFault)>,
    encode_error: Option<String>,
    soft_deadline_in_capture: bool,
    soft_deadline_in_exec: bool,
    exec_error: bool,
    rollback_fail: Option<String>,
    stall_cleanup: bool,
    hold_after_guard_release: Duration,
    hold_before_exec: Duration,
    hold_in_exec: Duration,
}

enum AckFault {
    AfterBudget(Duration),
    Withhold,
}

impl Faults {
    fn parse(extra: &Map<String, Value>) -> Self {
        let helper = std::env::var_os("GTERM_TEST_HELPER").is_some_and(|value| value == "1");
        let Some(fault) = extra
            .get("test_fault")
            .filter(|_| helper)
            .and_then(Value::as_object)
        else {
            return Self::default();
        };
        let ms = |key: &str, cap: Duration| {
            Duration::from_millis(fault.get(key).and_then(Value::as_u64).unwrap_or(0)).min(cap)
        };
        let flag = |key: &str| fault.get(key).and_then(Value::as_bool).unwrap_or(false);
        let pane = |key: &str| fault.get(key).and_then(Value::as_str).map(str::to_owned);
        let quiesce_ack = fault
            .get("quiesce_ack")
            .and_then(Value::as_object)
            .and_then(|ack| {
                let pane = ack.get("host_terminal_id")?.as_str()?.to_owned();
                let fault = if ack.get("hold").and_then(Value::as_bool) == Some(true) {
                    AckFault::Withhold
                } else {
                    let after = ack.get("after_budget_ms")?.as_u64()?;
                    AckFault::AfterBudget(Duration::from_millis(after).min(FAULT_DELAY_CAP))
                };
                Some((pane, fault))
            });
        Self {
            hold_accepted: ms("hold_accepted_ms", FAULT_DELAY_CAP),
            refuse_probe: flag("refuse_probe"),
            quiesce_ack,
            encode_error: pane("encode_error"),
            soft_deadline_in_capture: flag("soft_deadline_in_capture"),
            soft_deadline_in_exec: flag("soft_deadline_in_exec"),
            exec_error: flag("exec_error"),
            rollback_fail: pane("rollback_fail"),
            stall_cleanup: flag("stall_cleanup"),
            hold_after_guard_release: ms("hold_after_guard_release_ms", FAULT_STALL_CAP),
            hold_before_exec: ms("hold_before_exec_ms", FAULT_DELAY_CAP),
            hold_in_exec: ms("hold_in_exec_ms", FAULT_DELAY_CAP),
        }
    }
}

/// The `host_upgrade` verb (1.3 steps 1-3). The attempt runs on its own task,
/// so a closed connection cannot strand it between `upgrade_lock` and its
/// outcome. The returned sender fires once the reply is queued; the attempt
/// waits up to `REPLY_WAIT` for it before quiescing, so the reply is queued,
/// though not necessarily read, before the exec. A client that misses it
/// reads `last_outcome` from `ping`.
pub(crate) async fn host_upgrade(
    state: &Arc<HostState>,
    extra: &Map<String, Value>,
) -> (Value, Option<oneshot::Sender<()>>) {
    let exe = s(extra, "exe");
    let attempt_id = s(extra, "attempt_id");
    if exe.is_empty() || attempt_id.is_empty() {
        return (err("invalid_request"), None);
    }
    if state.attempts.context.get().is_none() {
        return (err("upgrade_unavailable"), None);
    }
    let Ok(lock) = state.attempts.lock.clone().try_lock_owned() else {
        // Teardown holds the lock once a drain ends, and a draining host
        // answers with the drain whoever holds it.
        let error = if state.draining.load(Ordering::SeqCst) {
            UpgradeReason::HostDraining.as_str()
        } else {
            "upgrade_in_progress"
        };
        return (err(error), None);
    };
    if let Err(reason) = admission(state).await {
        return (err(reason.as_str()), None);
    }
    let deadline_ns = monotonic_now_ns() + BUDGET.as_nanos() as u64;
    arm_alarm();
    *state
        .attempts
        .live
        .lock()
        .unwrap_or_else(PoisonError::into_inner) = Some(LiveAttempt {
        attempt_id: attempt_id.clone(),
        phase: Phase::Probing,
        candidate_sha256: None,
        deadline_ns,
    });
    let attempt = Attempt {
        state: Arc::clone(state),
        lock,
        attempt_id,
        exe: PathBuf::from(exe),
        deadline_ns,
        faults: Faults::parse(extra),
    };
    let (reply_tx, reply_rx) = oneshot::channel();
    let (replied_tx, replied_rx) = oneshot::channel();
    tokio::spawn(attempt.run(reply_tx, replied_rx));
    match reply_rx.await {
        Ok(reply) => (reply, Some(replied_tx)),
        Err(_) => (err("upgrade_failed"), None),
    }
}

struct Attempt {
    state: Arc<HostState>,
    lock: OwnedMutexGuard<()>,
    attempt_id: String,
    exe: PathBuf,
    deadline_ns: u64,
    faults: Faults,
}

/// What the attempt holds after acceptance and must undo on rollback.
#[derive(Default)]
struct Window {
    panes: Arc<Vec<(String, PtyIoActorHandle)>>,
    masters: Vec<OwnedFd>,
}

impl Attempt {
    fn context(&self) -> &UpgradeContext {
        // Checked before the attempt started; `install` runs once, in `run()`.
        self.state
            .attempts
            .context
            .get()
            .expect("upgrade context installed")
    }

    /// Why the attempt must stop short of exec now, if it must.
    fn cutoff(&self) -> Option<UpgradeReason> {
        if self.state.draining.load(Ordering::SeqCst) {
            Some(UpgradeReason::HostDraining)
        } else if until_soft_cutoff(self.deadline_ns).is_zero() {
            Some(UpgradeReason::SoftDeadline)
        } else {
            None
        }
    }

    fn phase(&self, phase: Phase) {
        self.state.attempts.update(|live| live.phase = phase);
    }

    fn images_dir(&self) -> PathBuf {
        self.context().socket_dir.join(image::IMAGES_DIR)
    }

    /// Records the terminal outcome, which returns `ping` to idle.
    fn record(&self, outcome: UpgradeOutcome, candidate_sha256: String) {
        *self
            .state
            .upgrade
            .lock()
            .unwrap_or_else(PoisonError::into_inner) = Some(UpgradeRecord {
            attempt: UpgradeAttempt {
                attempt_id: self.attempt_id.clone(),
                candidate_sha256,
                previous_sha256: self.state.image.sha256.clone(),
            },
            outcome: Some(outcome),
        });
        *self
            .state
            .attempts
            .live
            .lock()
            .unwrap_or_else(PoisonError::into_inner) = None;
    }

    /// Best effort: a candidate pin left behind is removed by the
    /// `prune_images` pass at the next host start.
    fn drop_candidate(&self, pin: &PinnedImage) {
        let _ = image::remove_candidate(pin, &self.state.image);
    }

    /// A return to idle before acceptance: record, clear the alarm while
    /// `upgrade_lock` is held, release it last, then answer.
    fn settle_before_accept(
        self,
        outcome: UpgradeOutcome,
        pin: Option<&PinnedImage>,
        reply: oneshot::Sender<Value>,
        response: Value,
    ) {
        if let Some(pin) = pin {
            self.drop_candidate(pin);
        }
        self.record(
            outcome,
            pin.map(|pin| pin.sha256.clone()).unwrap_or_default(),
        );
        disarm_alarm();
        // Reply before releasing the lock: a shutdown waiting in
        // `Attempts::settled` must not tear down ahead of this answer.
        let _ = reply.send(response);
        drop(self.lock);
    }

    async fn run(self, reply: oneshot::Sender<Value>, replied: oneshot::Receiver<()>) {
        let pin = match self.probe().await {
            Ok(pin) => pin,
            Err((detail, pin)) => {
                let response = json!({"ok": false, "error": "upgrade_refused", "detail": detail});
                return self.settle_before_accept(
                    UpgradeOutcome::Refused,
                    pin.as_ref(),
                    reply,
                    response,
                );
            }
        };
        let write = match self.take_gate().await {
            Ok(write) => write,
            Err(reason) => {
                let response = err(reason.as_str());
                return self.settle_before_accept(
                    UpgradeOutcome::Deferred(reason),
                    Some(&pin),
                    reply,
                    response,
                );
            }
        };
        let _ = reply.send(json!({
            "ok": true,
            "accepted": true,
            "attempt_id": self.attempt_id,
            "candidate_sha256": pin.sha256,
            "remaining_ms": remaining(self.deadline_ns).as_millis() as u64,
            "generation": self.state.generation,
        }));
        let _ = tokio::time::timeout(REPLY_WAIT, replied).await;
        let mut window = Window::default();
        let outcome = self.window(&pin, &mut window).await;
        self.roll_back(outcome, window, pin, write).await;
    }

    /// Step 2: pin the candidate and run its resume probe in its own process
    /// group, bounded by 5 s and the soft cutoff.
    async fn probe(&self) -> Result<PinnedImage, (String, Option<PinnedImage>)> {
        let (images_dir, exe) = (self.images_dir(), self.exe.clone());
        let pin = tokio::task::spawn_blocking(move || image::pin_image(&images_dir, &exe))
            .await
            .map_err(|join| (format!("pin: {join}"), None))?
            .map_err(|err| (format!("pin {}: {err}", self.exe.display()), None))?;
        self.state
            .attempts
            .update(|live| live.candidate_sha256 = Some(pin.sha256.clone()));
        let limit = PROBE_LIMIT.min(until_soft_cutoff(self.deadline_ns));
        let version = if self.faults.refuse_probe {
            0
        } else {
            FORMAT_VERSION
        };
        let spawned = Command::new(&pin.path)
            .args(["host", "--probe-resume", &version.to_string()])
            .process_group(0)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn();
        let mut child = match spawned {
            Ok(child) => child,
            Err(err) => return Err((format!("probe spawn: {err}"), Some(pin))),
        };
        let give_up = Instant::now() + limit;
        loop {
            match child.try_wait() {
                Ok(Some(status)) if status.success() => return Ok(pin),
                Ok(Some(status)) => return Err((format!("probe {status}"), Some(pin))),
                Ok(None) if Instant::now() < give_up => tokio::time::sleep(POLL).await,
                waited => {
                    // SAFETY: the probe leads its own process group; this
                    // signals only that group.
                    unsafe { libc::kill(-(child.id() as libc::pid_t), libc::SIGKILL) };
                    let _ = child.wait();
                    let detail = match waited {
                        Err(err) => format!("probe wait: {err}"),
                        _ => format!("probe timed out after {} ms", limit.as_millis()),
                    };
                    return Err((detail, Some(pin)));
                }
            }
        }
    }

    /// Step 3: the write guard, taken without queueing, then the recheck.
    async fn take_gate(&self) -> Result<OwnedRwLockWriteGuard<()>, UpgradeReason> {
        let give_up = Instant::now() + GATE_RETRY;
        let write = loop {
            if let Ok(write) = self.state.mutation_gate.clone().try_write_owned() {
                break write;
            }
            if Instant::now() >= give_up {
                return Err(UpgradeReason::HostBusy);
            }
            tokio::time::sleep(POLL).await;
        };
        admission(&self.state).await?;
        Ok(write)
    }

    /// Steps 4-6. Returns only on failure: a successful exec never returns.
    async fn window(&self, pin: &PinnedImage, window: &mut Window) -> UpgradeOutcome {
        self.phase(Phase::Quiescing);
        tokio::time::sleep(self.faults.hold_accepted).await;
        window.panes = Arc::new(self.freeze().await);
        let budget = until_soft_cutoff(self.deadline_ns).saturating_sub(HANDOFF_ROLLBACK);
        if budget.is_zero() {
            return UpgradeOutcome::Aborted(UpgradeReason::QuiesceTimeout);
        }
        if let Some((host_terminal_id, fault)) = &self.faults.quiesce_ack {
            let fault = match fault {
                AckFault::AfterBudget(after) => {
                    HandoffAckFault::DelayUntil(Instant::now() + budget + *after)
                }
                AckFault::Withhold => HandoffAckFault::Withhold,
            };
            for (_, actor) in window.panes.iter().filter(|(id, _)| id == host_terminal_id) {
                let _ = actor.arm_handoff_ack_fault(fault);
            }
        }
        let panes = Arc::clone(&window.panes);
        let failed = tokio::task::spawn_blocking(move || quiesce(&panes, budget))
            .await
            .unwrap_or_else(|_| vec!["<quiesce panicked>".into()]);
        if !failed.is_empty() {
            return UpgradeOutcome::Aborted(UpgradeReason::QuiesceTimeout);
        }

        self.phase(Phase::Capturing);
        let spec = CaptureSpec {
            attempt: UpgradeAttempt {
                attempt_id: self.attempt_id.clone(),
                candidate_sha256: pin.sha256.clone(),
                previous_sha256: self.state.image.sha256.clone(),
            },
            deadline_ns: self.deadline_ns,
            encode_error: self.faults.encode_error.clone(),
            soft_deadline_in_capture: self.faults.soft_deadline_in_capture,
        };
        let state = Arc::clone(&self.state);
        let mut capture_job = tokio::task::spawn_blocking(move || {
            let mut masters = Vec::new();
            let captured = capture(&state, spec, &mut masters);
            (captured, masters)
        });
        let captured =
            match tokio::time::timeout(until_soft_cutoff(self.deadline_ns), &mut capture_job).await
            {
                Ok(captured) => captured,
                Err(_) => {
                    // Abort prevents a queued job starting. A running blocking
                    // job must finish before rollback can resume its panes;
                    // its private masters are dropped with its result.
                    capture_job.abort();
                    if tokio::time::timeout(remaining(self.deadline_ns), capture_job)
                        .await
                        .is_err()
                    {
                        // SAFETY: SIGALRM has its default fatal action. Never
                        // reopen the gate over a capture that cannot finish.
                        unsafe { libc::raise(libc::SIGALRM) };
                        std::future::pending::<()>().await;
                    }
                    return UpgradeOutcome::Aborted(UpgradeReason::SoftDeadline);
                }
            };
        let handover = match captured {
            Ok((captured, masters)) => {
                window.masters = masters;
                match captured {
                    Ok(handover) => handover,
                    Err(reason) => return UpgradeOutcome::Aborted(reason),
                }
            }
            Err(_) => return UpgradeOutcome::Aborted(UpgradeReason::CaptureFailed),
        };
        if let Some(reason) = self.cutoff() {
            return UpgradeOutcome::Aborted(reason);
        }
        let path = self.context().socket_dir.join(STATE_FILE);
        let target = path.clone();
        let written = tokio::task::spawn_blocking(move || write_state(&target, &handover)).await;
        if !matches!(written, Ok(Ok(()))) {
            return UpgradeOutcome::Aborted(UpgradeReason::StateWriteFailed);
        }
        // 1.4: exec only the bytes the probe accepted.
        let verified = {
            let pin = pin.clone();
            tokio::task::spawn_blocking(move || pin.verify()).await
        };
        if !matches!(verified, Ok(Ok(()))) {
            return UpgradeOutcome::Aborted(UpgradeReason::PinMismatch);
        }
        if let Some(reason) = self.cutoff() {
            return UpgradeOutcome::Aborted(reason);
        }
        tokio::time::sleep(self.faults.hold_before_exec).await;

        let program = if self.faults.exec_error {
            pin.path.with_file_name("gterm-missing-image")
        } else {
            pin.path.clone()
        };
        let context = self.context();
        let mut fds: Vec<RawFd> = window.masters.iter().map(AsRawFd::as_raw_fd).collect();
        fds.extend([context.control_fd, context.frames_fd]);
        let argv = context.argv.clone();
        let state = Arc::clone(&self.state);
        let hold = self.faults.hold_in_exec;
        let soft_deadline_in_exec = self.faults.soft_deadline_in_exec;
        let deadline_ns = self.deadline_ns;
        let exec = move || {
            // A SIGTERM before the mask set `draining`; one after it stays
            // pending into the new image.
            if state.draining.load(Ordering::SeqCst) {
                return Err(UpgradeReason::HostDraining);
            }
            state.attempts.update(|live| live.phase = Phase::Exec);
            std::thread::sleep(hold);
            if soft_deadline_in_exec {
                std::thread::sleep(until_soft_cutoff(deadline_ns));
            }
            // Every await and queue is behind us: the soft cutoff is checked
            // last, at the commit.
            sigterm::exec(&program, &argv, &path, &fds, || {
                !until_soft_cutoff(deadline_ns).is_zero()
            })
            .ok_or(UpgradeReason::SoftDeadline)
        };
        match sigterm::blocked(exec).await {
            Some(Ok(errno)) => UpgradeOutcome::RolledBack { errno },
            Some(Err(reason)) => UpgradeOutcome::Aborted(reason),
            None => UpgradeOutcome::Aborted(UpgradeReason::HostDraining),
        }
    }

    /// Freezes reaping on every carried pane and takes a thread-safe handle
    /// to its actor.
    async fn freeze(&self) -> Vec<(String, PtyIoActorHandle)> {
        let inner = self.state.inner.lock().await;
        let mut panes = Vec::new();
        for slot in inner.terminals.values() {
            let Some(child) = &slot.child else {
                continue;
            };
            child.runtime.freeze_reaping();
            if let Some(actor) = child.runtime.handoff_actor() {
                panes.push((slot.host_terminal_id.clone(), actor));
            }
        }
        panes
    }

    /// Step 7, under the alarm and the write guard.
    async fn roll_back(
        self,
        outcome: UpgradeOutcome,
        window: Window,
        pin: PinnedImage,
        write: OwnedRwLockWriteGuard<()>,
    ) {
        self.phase(Phase::RollingBack);
        drop(window.masters);
        let fail = self.faults.rollback_fail.clone();
        let panes = Arc::clone(&window.panes);
        let stuck = tokio::task::spawn_blocking(move || resume(&panes, fail.as_deref()))
            .await
            .unwrap_or_else(|_| vec!["<rollback panicked>".into()]);
        drop(window.panes);
        {
            let inner = self.state.inner.lock().await;
            for child in inner
                .terminals
                .values()
                .filter_map(|slot| slot.child.as_ref())
            {
                child.runtime.unfreeze_reaping();
            }
        }
        if !stuck.is_empty() {
            // SAFETY: SIGALRM's action is the default, which ends the process.
            unsafe { libc::raise(libc::SIGALRM) };
            // Never reopen the gate over a pane that refuses input.
            std::future::pending::<()>().await;
        }
        if self.faults.stall_cleanup {
            tokio::time::sleep(FAULT_STALL_CAP).await;
        }
        let _ = std::fs::remove_file(self.context().socket_dir.join(STATE_FILE));
        self.drop_candidate(&pin);
        self.record(outcome, pin.sha256.clone());
        let reason = match outcome {
            UpgradeOutcome::Aborted(reason) => reason.as_str(),
            _ => "exec_failed",
        };
        self.state
            .events
            .emit_host_upgrade_failed(self.attempt_id.clone(), reason)
            .await;
        drop(write);
        tokio::time::sleep(self.faults.hold_after_guard_release).await;
        disarm_alarm();
        drop(self.lock);
    }
}

/// One thread per pane: quiesce within `budget`. Returns the panes that failed.
fn quiesce(panes: &[(String, PtyIoActorHandle)], budget: Duration) -> Vec<String> {
    std::thread::scope(|scope| {
        let joins: Vec<_> = panes
            .iter()
            .map(|(id, actor)| (id, scope.spawn(move || actor.begin_handoff(budget))))
            .collect();
        joins
            .into_iter()
            .filter_map(|(id, join)| match join.join() {
                Ok(Ok(())) => None,
                Ok(Err(_)) => Some(id.clone()),
                Err(_) => Some(id.clone()),
            })
            .collect()
    })
}

/// One thread per pane: resume, retrying once. Returns the panes that
/// refused both times.
fn resume(panes: &[(String, PtyIoActorHandle)], fail: Option<&str>) -> Vec<String> {
    std::thread::scope(|scope| {
        let joins: Vec<_> = panes
            .iter()
            .map(|(id, actor)| {
                let forced = fail == Some(id.as_str());
                (
                    id,
                    scope
                        .spawn(move || (0..2).any(|_| !forced && actor.rollback_handoff().is_ok())),
                )
            })
            .collect();
        joins
            .into_iter()
            .filter_map(|(id, join)| (!matches!(join.join(), Ok(true))).then(|| id.clone()))
            .collect()
    })
}

struct CaptureSpec {
    attempt: UpgradeAttempt,
    deadline_ns: u64,
    encode_error: Option<String>,
    soft_deadline_in_capture: bool,
}

/// Step 5 on a blocking thread, under the `inner` lock then the events lock.
/// Every master duplicate lands in `masters`, which closes it on rollback or
/// when a panic unwinds.
fn capture(
    state: &HostState,
    spec: CaptureSpec,
    masters: &mut Vec<OwnedFd>,
) -> Result<HandoverState, UpgradeReason> {
    let context = state
        .attempts
        .context
        .get()
        .ok_or(UpgradeReason::CaptureFailed)?;
    let inner = state.inner.blocking_lock();
    if spec.soft_deadline_in_capture {
        // Exercise a worker still running when the window's soft wait expires.
        std::thread::sleep(until_soft_cutoff(spec.deadline_ns) + HANDOFF_ROLLBACK);
    }
    let mut slots: Vec<_> = inner
        .terminals
        .values()
        .filter_map(|slot| slot.child.as_ref().map(|child| (slot, child)))
        .collect();
    slots.sort_by(|(a, _), (b, _)| a.host_terminal_id.cmp(&b.host_terminal_id));
    let failed = |_what: &str, _id: &str, _err: std::io::Error| UpgradeReason::CaptureFailed;
    let mut panes = Vec::with_capacity(slots.len());
    for (slot, child) in slots {
        if until_soft_cutoff(spec.deadline_ns).is_zero() {
            return Err(UpgradeReason::SoftDeadline);
        }
        let id = slot.host_terminal_id.as_str();
        if spec.encode_error.as_deref() == Some(id) {
            return Err(failed(
                "encode",
                id,
                std::io::Error::other("injected encode error"),
            ));
        }
        let (snapshot, core) = child
            .runtime
            .encode_handover()
            .map_err(|err| failed("encode", id, err))?;
        if until_soft_cutoff(spec.deadline_ns).is_zero() {
            return Err(UpgradeReason::SoftDeadline);
        }
        let master_fd = child
            .runtime
            .duplicate_handoff_fd()
            .map_err(|err| failed("master duplicate", id, err))?;
        // SAFETY: the duplicate is new, and nothing else owns it.
        masters.push(unsafe { OwnedFd::from_raw_fd(master_fd) });
        let (_, _, pixel_width, pixel_height) = child.runtime.handover_size();
        panes.push(CarriedPane {
            host_terminal_id: slot.host_terminal_id.clone(),
            terminal_id: slot.identity.terminal_id.clone(),
            spawn_key: slot.identity.spawn_key.clone(),
            master_fd,
            pid: child.pid,
            pgid: slot.pgid,
            start_time: child.start_time,
            title: slot.title.clone(),
            rows: slot.rows,
            cols: slot.cols,
            pixel_width,
            pixel_height,
            last_seq: slot.last_seq,
            fingerprint: slot.fingerprint,
            observation_state: slot.observation_state,
            observation_generation: slot.observation_generation,
            observer_generation: slot.observer_generation,
            written_bytes: slot.written_bytes,
            dropped_bytes: slot.dropped_bytes,
            total_bytes: slot.total_bytes,
            truncated: slot.truncated,
            input_grant: slot.input_grant.clone(),
            locator: slot.locator.clone(),
            reported_cwd: child.runtime.reported_cwd(),
            reservation_id: slot.reservation_id.clone(),
            reserve_key: slot.reserve_key.clone(),
            reserve_generation: slot.reserve_generation,
            // Attachments close at exec, so a bound observer is carried as
            // entitled.
            observer_bind: match &slot.observer_bind {
                ObserverBind::None => CarriedObserverBind::None,
                ObserverBind::Reserved {
                    reservation_id,
                    generation,
                } => CarriedObserverBind::Reserved {
                    reservation_id: reservation_id.clone(),
                    generation: *generation,
                },
                ObserverBind::Bound {
                    reservation_id,
                    generation,
                    ..
                }
                | ObserverBind::Entitled {
                    reservation_id,
                    generation,
                } => CarriedObserverBind::Entitled {
                    reservation_id: reservation_id.clone(),
                    generation: *generation,
                },
            },
            reservation: inner
                .reservations
                .get(&slot.reservation_id)
                .map(|reservation| CarriedReservation {
                    id: reservation.id.clone(),
                    key: reservation.key.clone(),
                    generation: reservation.generation,
                    terminal_id: reservation.terminal_id.clone(),
                    identity: reservation
                        .identity
                        .as_ref()
                        .map(|identity| CarriedIdentity {
                            terminal_id: identity.terminal_id.clone(),
                            spawn_key: identity.spawn_key.clone(),
                        }),
                }),
            exit: child.runtime.child_exit(),
            snapshot_b64: encode_snapshot(&snapshot),
            core,
        });
    }
    Ok(HandoverState {
        format_version: FORMAT_VERSION,
        host_epoch: state.host_epoch.clone(),
        generation: state.generation + 1,
        host_pid: state.host_pid,
        deadline_monotonic_ns: spec.deadline_ns,
        attempt: spec.attempt,
        previous_image: state.image.path.clone(),
        argv: context.argv.clone(),
        control_listener_fd: context.control_fd,
        frames_listener_fd: context.frames_fd,
        next_host_id: inner.next_host_id,
        events: state.events.carried(),
        panes,
    })
}
