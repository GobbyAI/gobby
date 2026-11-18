use super::*;
use std::sync::atomic::AtomicBool;
use std::sync::Arc;

fn activity() -> InputActivity {
    InputActivity {
        effect: InputEffect::Edit,
        terminal_id: "t".into(),
        host_terminal_id: "h".into(),
        attachment_id: "a".into(),
        kind: "input",
        bytes: 1,
        interrupt: None,
        submit: false,
        edits: super::super::edits::summarize(b"x"),
    }
}

#[tokio::test]
async fn cancelled_input_never_enqueues_without_an_event_slot() {
    let events = Arc::new(HostEvents::new("e".into(), 64 * 1024));
    let enqueued = Arc::new(AtomicBool::new(false));
    let held = events.state.lock().await;
    let (started, ready) = tokio::sync::oneshot::channel();
    let task = {
        let events = events.clone();
        let enqueued = enqueued.clone();
        tokio::spawn(async move {
            started.send(()).unwrap();
            events
                .emit_input_activity(activity(), || {
                    enqueued.store(true, Ordering::SeqCst);
                    Ok(())
                })
                .await
        })
    };
    ready.await.unwrap();
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    drop(held);
    assert!(!enqueued.load(Ordering::SeqCst));
    assert_eq!(events.cursor().await, ("e".into(), 0));
}

#[tokio::test]
async fn rejected_input_publishes_no_activity() {
    let events = HostEvents::new("e".into(), 64 * 1024);
    let result = events
        .emit_input_activity(activity(), || Err("pty_busy"))
        .await;
    assert_eq!(result, Err("pty_busy"));
    assert_eq!(events.cursor().await, ("e".into(), 0));
    events
        .emit_input_activity(activity(), || Ok(()))
        .await
        .unwrap();
    assert_eq!(events.cursor().await, ("e".into(), 1));
}
