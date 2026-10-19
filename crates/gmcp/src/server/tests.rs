use super::*;
use std::cell::RefCell;

use anyhow::{Context, Result};

/// A readiness failure with the given wrapper code.
fn failure(code: &str) -> ReadyFailure {
    ReadyFailure {
        code: code.to_owned(),
        message: format!("{code} message"),
    }
}

/// Runs `report_ready` over scripted answers, returning its attempt count and the log lines.
async fn ready_with(answers: Vec<Result<(), ReadyFailure>>) -> (usize, Vec<String>) {
    let mut answers = answers.into_iter();
    let logs = RefCell::new(Vec::new());
    let attempts = report_ready(
        &[Duration::ZERO; 4],
        || {
            std::future::ready(
                answers
                    .next()
                    .unwrap_or_else(|| Err(failure("DAEMON_UNAVAILABLE"))),
            )
        },
        |line| logs.borrow_mut().push(line.to_owned()),
    )
    .await;
    (attempts, logs.into_inner())
}

#[tokio::test]
async fn posts_once_after_the_first_success() {
    let (attempts, logs) = ready_with(vec![Ok(())]).await;
    assert_eq!(attempts, 1);
    assert!(logs.is_empty());
}

#[tokio::test]
async fn stops_on_a_failure_outside_the_retry_set() {
    let (attempts, logs) = ready_with(vec![Err(failure("PERMISSION_DENIED"))]).await;
    assert_eq!(attempts, 1);
    assert_eq!(
        logs,
        ["gmcp: bridge readiness report failed: PERMISSION_DENIED message"]
    );
}

#[tokio::test]
async fn retries_every_wrapper_code_along_the_schedule() {
    for code in RETRYABLE_ERROR_CODES {
        let (attempts, _) = ready_with(vec![Err(failure(code)); 4]).await;
        assert_eq!(attempts, 4, "{code}");
    }
}

#[tokio::test]
async fn retried_codes_log_nothing_before_success() {
    let (attempts, logs) = ready_with(vec![
        Err(failure("DAEMON_UNAVAILABLE")),
        Err(failure("REQUEST_TIMEOUT")),
        Ok(()),
    ])
    .await;
    assert_eq!(attempts, 3);
    assert!(logs.is_empty());
}

#[tokio::test]
async fn session_required_give_up_stays_silent() {
    let (attempts, logs) = ready_with(vec![Err(failure("SESSION_REQUIRED")); 4]).await;
    assert_eq!(attempts, 4);
    assert!(logs.is_empty());
}

#[tokio::test]
async fn other_give_up_logs_once_with_the_last_message() {
    let (attempts, logs) = ready_with(vec![Err(failure("DAEMON_UNAVAILABLE")); 4]).await;
    assert_eq!(attempts, 4);
    assert_eq!(
        logs,
        ["gmcp: bridge readiness report gave up after 3 retries: DAEMON_UNAVAILABLE message"]
    );
}

#[test]
fn schedule_and_codes_match_the_wire_fixture() -> Result<()> {
    let wire: serde_json::Value =
        serde_json::from_str(include_str!("../../contracts/wire_cases.v1.json"))?;
    let bridge = &wire["bridge_ready"];

    let scheduled: Vec<f64> = READY_DELAYS.iter().map(Duration::as_secs_f64).collect();
    let fixture_delays: Vec<f64> = bridge["delays_seconds"]
        .as_array()
        .context("delays_seconds")?
        .iter()
        .map(|delay| delay.as_f64().context("delay seconds"))
        .collect::<Result<_>>()?;
    assert_eq!(scheduled, fixture_delays);

    let mut fixture_codes: Vec<&str> = bridge["retry_error_codes"]
        .as_array()
        .context("retry_error_codes")?
        .iter()
        .map(|code| code.as_str().context("retry error code"))
        .collect::<Result<_>>()?;
    fixture_codes.sort_unstable();
    let mut codes = RETRYABLE_ERROR_CODES.to_vec();
    codes.sort_unstable();
    assert_eq!(codes, fixture_codes);
    Ok(())
}
