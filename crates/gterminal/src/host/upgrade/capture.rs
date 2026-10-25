//! Step 5's capture: the handover state built from the live host, with every
//! pane's snapshot and wrapper state and a duplicate of its master fd.

use std::os::fd::{FromRawFd, OwnedFd};
use std::time::Duration;

use super::super::handover::{
    encode_snapshot, CarriedIdentity, CarriedObserverBind, CarriedPane, CarriedReservation,
    HandoverState, UpgradeAttempt, UpgradeReason, FORMAT_VERSION,
};
use super::super::state::{HostState, ObserverBind};
use super::{until_soft_cutoff, HANDOFF_ROLLBACK};

pub(super) struct CaptureSpec {
    pub(super) attempt: UpgradeAttempt,
    pub(super) deadline_ns: u64,
    pub(super) encode_error: Option<String>,
    pub(super) soft_deadline_in_capture: bool,
    pub(super) capture_cutoff_delay: Duration,
}

/// Step 5 on a blocking thread, under the `inner` lock then the events lock.
/// Every master duplicate lands in `masters`, which closes it on rollback or
/// when a panic unwinds.
pub(super) fn capture(
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
        std::thread::sleep(
            until_soft_cutoff(spec.deadline_ns)
                + spec.capture_cutoff_delay.max(HANDOFF_ROLLBACK / 2),
        );
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
        next_attachment: inner.next_attachment,
        events: state.events.carried(),
        panes,
    })
}
