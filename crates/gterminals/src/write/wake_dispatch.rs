//! Host submission for one native wake batch: each target's prelude, then its
//! Enter after the pace. Each host request first refuses the targets whose token
//! has retired since admission, so a retired token never reaches the host.

use std::collections::{HashMap, HashSet};
use std::time::Duration;

use super::{
    BatchRuntime, BatchTarget, Delivery, Kind, Phased, Prepared, Stage, WakeStep, WriteFailure,
};
use crate::write_api::Fence;

/// Result ids whose token has retired since their request was admitted.
fn retired_ids(prepared: &[Prepared]) -> HashSet<String> {
    prepared
        .iter()
        .filter(|item| !item.fence.as_ref().is_none_or(Fence::holds))
        .map(|item| item.target.result_id.clone())
        .collect()
}

/// The refusal for a target whose token retired before its next host request.
/// Only `Partial` leaves its text in the composer, so only it keeps the latch.
fn retired_failure(stage: Stage) -> Result<Delivery, WriteFailure> {
    let detail = match stage {
        Stage::None => "the write token was retired before the effect was dispatched",
        Stage::Partial => "the write token was retired before the Enter was dispatched",
    };
    Err(WriteFailure {
        stage,
        code: "retired_token".into(),
        detail: detail.into(),
    })
}

/// Drops the targets whose token retired, recording each refusal at `step`.
fn refuse_retired(
    prepared: &[Prepared],
    targets: &mut Vec<BatchTarget>,
    step: WakeStep,
    by_id: &mut HashMap<String, Phased>,
) {
    let dead = retired_ids(prepared);
    targets.retain(|target| {
        if !dead.contains(&target.result_id) {
            return true;
        }
        by_id.insert(
            target.result_id.clone(),
            (step, retired_failure(Stage::None)),
        );
        false
    });
}

/// Two host requests: every prelude, then the Enter of each target whose
/// prelude was delivered. A transport failure leaves every target uncertain.
/// Each request refuses the targets whose token has retired before it is
/// sent, so a retired token never reaches the host.
pub(super) async fn dispatch_wake(batch: &dyn BatchRuntime, prepared: &[Prepared]) -> Vec<Phased> {
    let mut prelude = Vec::new();
    let mut enter = Vec::new();
    let mut has_prelude = HashSet::new();
    for item in prepared {
        let target = &item.target;
        match target.operations.split_last() {
            Some((last, rest)) if last.kind == Kind::Key && last.payload == "enter" => {
                enter.push(BatchTarget {
                    operations: vec![last.clone()],
                    ..target.clone()
                });
                if !rest.is_empty() {
                    has_prelude.insert(target.result_id.clone());
                    prelude.push(BatchTarget {
                        operations: rest.to_vec(),
                        ..target.clone()
                    });
                }
            }
            _ => prelude.push(target.clone()),
        }
    }
    let mut by_id: HashMap<String, Phased> = HashMap::new();
    let mut entered = HashSet::new();
    // The step a target had reached when its outcome went unknown.
    let unknown = |entered: &HashSet<String>, item: &Prepared, detail: &str| -> Phased {
        let step = if entered.contains(&item.target.result_id) {
            WakeStep::Enter
        } else {
            WakeStep::Prelude
        };
        (
            step,
            Ok(Delivery::Uncertain {
                detail: detail.into(),
            }),
        )
    };
    let uncertain = |entered: &HashSet<String>, detail: &str| {
        prepared
            .iter()
            .map(|item| unknown(entered, item, detail))
            .collect()
    };
    refuse_retired(prepared, &mut prelude, WakeStep::Preflight, &mut by_id);
    refuse_retired(prepared, &mut enter, WakeStep::Preflight, &mut by_id);
    if !prelude.is_empty() {
        match batch.write_batch(prelude).await {
            Ok(results) => {
                for result in results {
                    by_id.insert(result.result_id, (WakeStep::Prelude, result.outcome));
                }
            }
            Err(detail) => return uncertain(&entered, &detail),
        }
    }
    let mut ready: Vec<BatchTarget> = enter
        .into_iter()
        .filter(|target| {
            !has_prelude.contains(&target.result_id)
                || matches!(
                    by_id.get(&target.result_id),
                    Some((_, Ok(Delivery::Delivered)))
                )
        })
        .collect();
    if let Some(pace) = ready.iter().map(|t| t.operations[0].delay_ms).max() {
        // Pace before submitting, so a hook read during the text/Enter gap
        // cannot acknowledge the new prompt.
        tokio::time::sleep(Duration::from_millis(pace)).await;
        // A token retired during the pace sends no Enter. Text that already
        // reached the composer keeps the wake partial, so its latch stays.
        let dead = retired_ids(prepared);
        ready.retain(|target| {
            if !dead.contains(&target.result_id) {
                return true;
            }
            let refusal = if has_prelude.contains(&target.result_id) {
                (WakeStep::Enter, retired_failure(Stage::Partial))
            } else {
                (WakeStep::Preflight, retired_failure(Stage::None))
            };
            by_id.insert(target.result_id.clone(), refusal);
            false
        });
        for target in &mut ready {
            target.operations[0].delay_ms = 0;
            entered.insert(target.result_id.clone());
        }
        if !ready.is_empty() {
            match batch.write_batch(ready).await {
                Ok(results) => {
                    for mut result in results {
                        // A refused Enter is still a partial wake once its text
                        // reached the composer, so the latch stays.
                        if has_prelude.contains(&result.result_id)
                            && let Err(failure) = &mut result.outcome
                            && failure.stage == Stage::None
                        {
                            failure.stage = Stage::Partial;
                        }
                        by_id.insert(result.result_id, (WakeStep::Enter, result.outcome));
                    }
                }
                Err(detail) => return uncertain(&entered, &detail),
            }
        }
    }
    prepared
        .iter()
        .map(|item| {
            by_id.remove(&item.target.result_id).unwrap_or_else(|| {
                unknown(
                    &entered,
                    item,
                    "native host returned no result for this target",
                )
            })
        })
        .collect()
}
