use super::*;
use std::cell::Cell;

#[tokio::test]
async fn posts_once_after_the_first_success() {
    let calls = Cell::new(0);
    let attempts = report_ready(&[Duration::ZERO; 4], || {
        calls.set(calls.get() + 1);
        std::future::ready(None)
    })
    .await;
    assert_eq!(attempts, 1);
    assert_eq!(calls.get(), 1);
}

#[tokio::test]
async fn stops_on_a_failure_outside_the_retry_set() {
    let attempts = report_ready(&[Duration::ZERO; 4], || {
        std::future::ready(Some("PERMISSION_DENIED".to_owned()))
    })
    .await;
    assert_eq!(attempts, 1);
}

#[tokio::test]
async fn retries_every_wrapper_code_along_the_schedule() {
    for code in RETRYABLE_ERROR_CODES {
        let attempts = report_ready(&[Duration::ZERO; 3], || {
            std::future::ready(Some(code.to_owned()))
        })
        .await;
        assert_eq!(attempts, 3, "{code}");
    }
}
