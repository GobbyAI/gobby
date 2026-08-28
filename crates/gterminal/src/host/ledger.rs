//! Per-connection monotonic `operation_seq` ledger.

use std::collections::VecDeque;

use serde_json::Value;

use crate::protocol::OPERATION_LEDGER_SIZE;

#[derive(Debug)]
pub enum LedgerDecision {
    Execute,
    Replay(Value),
    Gap,
    Expired,
    FingerprintMismatch,
}

#[derive(Debug, Default)]
pub struct OperationLedger {
    high_seq: u64,
    evicted_below: u64,
    entries: VecDeque<Entry>,
}

#[derive(Debug, Clone)]
struct Entry {
    seq: u64,
    fingerprint: u64,
    outcome: Value,
}

impl OperationLedger {
    pub fn decide(&self, seq: u64, fingerprint: u64) -> LedgerDecision {
        if seq <= self.evicted_below {
            return LedgerDecision::Expired;
        }
        if let Some(entry) = self.entries.iter().find(|entry| entry.seq == seq) {
            if entry.fingerprint != fingerprint {
                return LedgerDecision::FingerprintMismatch;
            }
            return LedgerDecision::Replay(entry.outcome.clone());
        }
        if seq == self.high_seq + 1 {
            LedgerDecision::Execute
        } else if seq > self.high_seq + 1 {
            LedgerDecision::Gap
        } else {
            LedgerDecision::Expired
        }
    }

    pub fn record(&mut self, seq: u64, fingerprint: u64, outcome: Value) {
        self.high_seq = self.high_seq.max(seq);
        self.entries.push_back(Entry {
            seq,
            fingerprint,
            outcome,
        });
        while self.entries.len() > OPERATION_LEDGER_SIZE {
            if let Some(old) = self.entries.pop_front() {
                self.evicted_below = self.evicted_below.max(old.seq);
            }
        }
    }
}

pub fn fingerprint_json(method: &str, extra: &Value) -> u64 {
    use std::hash::{Hash, Hasher};
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    method.hash(&mut hasher);
    extra.to_string().hash(&mut hasher);
    hasher.finish()
}
