//! Per-terminal action hold: the write route's admission rule.
//!
//! The gate admits one host effect at a time. A hold reserves the terminal for
//! its holder's steps; operations without the hold queue in arrival order and
//! are admitted one by one once no hold or effect stands in the way. An effect
//! that becomes uncertain (TTL expiry, timeout, disconnect) keeps blocking
//! admission until it completes or a [`Proof`] shows it can produce nothing
//! further. The gate never re-dispatches an admitted op.

use std::collections::VecDeque;

use tokio::time::{Duration, Instant};

/// A hold lapses this long after its last acquire, step start, or step end.
pub const HOLD_TTL: Duration = Duration::from_secs(10);
pub const QUEUE_MAX_ITEMS: usize = 128;
pub const QUEUE_MAX_BYTES: usize = 256 * 1024;
/// New input is refused once the oldest queued item has waited this long.
pub const QUEUE_MAX_AGE: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct HoldToken(u64);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct EffectId(u64);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UncertainCause {
    TtlExpired,
    Timeout,
    Disconnected,
    Cancelled,
    ChildStopped,
}

/// Evidence that an admitted effect can produce no further effects.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Proof {
    PaneDestroyed,
    TerminalDestroyed,
    HostEpochChanged,
    HostExited,
    OperatorClose,
    OperatorKill,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Resolution {
    pub effect: EffectId,
    pub proof: Proof,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum HoldError {
    #[error("terminal action queue is full ({items} items, {bytes} bytes)")]
    QueueFull { items: usize, bytes: usize },
    #[error("terminal action queue's oldest item has waited {waited:?}")]
    QueueStale { waited: Duration },
    #[error("another action holds or is admitted on this terminal")]
    Busy,
    #[error("the hold token is not the terminal's current hold")]
    SupersededToken,
    #[error("an effect is already admitted on this terminal")]
    EffectInFlight,
    #[error("the effect is not the terminal's admitted effect")]
    StaleEffect,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Admitted<T> {
    pub effect: EffectId,
    pub op: T,
}

struct Hold {
    token: HoldToken,
    expires: Instant,
}

struct InFlight {
    effect: EffectId,
    hold: Option<HoldToken>,
    uncertain: Option<UncertainCause>,
}

struct Queued<T> {
    op: T,
    bytes: usize,
    at: Instant,
}

pub struct ActionGate<T> {
    seq: u64,
    hold: Option<Hold>,
    in_flight: Option<InFlight>,
    queue: VecDeque<Queued<T>>,
    queued_bytes: usize,
}

impl<T> Default for ActionGate<T> {
    fn default() -> Self {
        Self::new()
    }
}

impl<T> ActionGate<T> {
    pub fn new() -> Self {
        Self {
            seq: 0,
            hold: None,
            in_flight: None,
            queue: VecDeque::new(),
            queued_bytes: 0,
        }
    }

    /// Takes the hold. Earlier queued arrivals and an admitted effect go first.
    pub fn acquire(&mut self, now: Instant) -> Result<HoldToken, HoldError> {
        self.lapse(now);
        if self.hold.is_some() || self.in_flight.is_some() || !self.queue.is_empty() {
            return Err(HoldError::Busy);
        }
        let token = HoldToken(self.bump());
        self.hold = Some(Hold {
            token,
            expires: now + HOLD_TTL,
        });
        Ok(token)
    }

    /// Queues an operation without the hold; [`Self::next`] admits it in turn.
    pub fn submit(&mut self, op: T, bytes: usize, now: Instant) -> Result<(), HoldError> {
        self.lapse(now);
        if let Some(oldest) = self.queue.front() {
            let waited = now.saturating_duration_since(oldest.at);
            if waited >= QUEUE_MAX_AGE {
                return Err(HoldError::QueueStale { waited });
            }
        }
        if self.queue.len() >= QUEUE_MAX_ITEMS || self.queued_bytes + bytes > QUEUE_MAX_BYTES {
            return Err(HoldError::QueueFull {
                items: self.queue.len(),
                bytes: self.queued_bytes,
            });
        }
        self.queued_bytes += bytes;
        self.queue.push_back(Queued { op, bytes, at: now });
        Ok(())
    }

    /// Admits the oldest queued operation when no hold or effect blocks it.
    pub fn next(&mut self, now: Instant) -> Option<Admitted<T>> {
        self.lapse(now);
        if self.hold.is_some() || self.in_flight.is_some() {
            return None;
        }
        let queued = self.queue.pop_front()?;
        self.queued_bytes -= queued.bytes;
        let effect = EffectId(self.bump());
        self.in_flight = Some(InFlight {
            effect,
            hold: None,
            uncertain: None,
        });
        Some(Admitted {
            effect,
            op: queued.op,
        })
    }

    /// Admits one step under the hold and renews its TTL.
    pub fn begin(&mut self, token: HoldToken, now: Instant) -> Result<EffectId, HoldError> {
        self.lapse(now);
        self.current(token)?;
        if self.in_flight.is_some() {
            return Err(HoldError::EffectInFlight);
        }
        if let Some(hold) = self.hold.as_mut() {
            hold.expires = now + HOLD_TTL;
        }
        let effect = EffectId(self.bump());
        self.in_flight = Some(InFlight {
            effect,
            hold: Some(token),
            uncertain: None,
        });
        Ok(effect)
    }

    /// Completes the admitted effect, certain or not. A reply for any other
    /// effect changes nothing.
    pub fn finish(&mut self, effect: EffectId, now: Instant) -> Result<(), HoldError> {
        let in_flight = self.in_flight.take_if(|f| f.effect == effect);
        let Some(in_flight) = in_flight else {
            return Err(HoldError::StaleEffect);
        };
        if let Some(hold) = self.hold.as_mut()
            && Some(hold.token) == in_flight.hold
            && now < hold.expires
        {
            hold.expires = now + HOLD_TTL;
        }
        self.lapse(now);
        Ok(())
    }

    /// Ends the hold. An admitted step keeps running; nothing queued is admitted
    /// before it finishes.
    pub fn release(&mut self, token: HoldToken, now: Instant) -> Result<(), HoldError> {
        self.lapse(now);
        self.current(token)?;
        self.hold = None;
        Ok(())
    }

    /// Records that the admitted effect's outcome is unknown. The hold ends;
    /// admission stays blocked until completion or proof.
    pub fn uncertain(&mut self, effect: EffectId, cause: UncertainCause) -> Result<(), HoldError> {
        let in_flight = self
            .in_flight
            .as_mut()
            .filter(|f| f.effect == effect)
            .ok_or(HoldError::StaleEffect)?;
        in_flight.uncertain.get_or_insert(cause);
        self.hold = None;
        Ok(())
    }

    /// Clears the admitted effect on proof that it can do nothing further. A
    /// proof for any other effect changes nothing.
    pub fn prove(&mut self, effect: EffectId, proof: Proof) -> Result<Resolution, HoldError> {
        self.in_flight
            .take_if(|f| f.effect == effect)
            .map(|f| Resolution {
                effect: f.effect,
                proof,
            })
            .ok_or(HoldError::StaleEffect)
    }

    pub fn uncertainty(&self) -> Option<(EffectId, UncertainCause)> {
        self.in_flight
            .as_ref()
            .and_then(|f| f.uncertain.map(|cause| (f.effect, cause)))
    }

    pub fn queued(&self) -> usize {
        self.queue.len()
    }

    fn current(&self, token: HoldToken) -> Result<(), HoldError> {
        match &self.hold {
            Some(hold) if hold.token == token => Ok(()),
            _ => Err(HoldError::SupersededToken),
        }
    }

    /// Drops an expired hold; its admitted step, if any, becomes uncertain.
    fn lapse(&mut self, now: Instant) {
        let Some(hold) = self.hold.take_if(|h| now >= h.expires) else {
            return;
        };
        if let Some(in_flight) = self.in_flight.as_mut()
            && in_flight.hold == Some(hold.token)
        {
            in_flight
                .uncertain
                .get_or_insert(UncertainCause::TtlExpired);
        }
    }

    fn bump(&mut self) -> u64 {
        self.seq += 1;
        self.seq
    }
}
