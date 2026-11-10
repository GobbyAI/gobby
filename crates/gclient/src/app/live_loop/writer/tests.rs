use super::*;
use crate::daemon::{DaemonError, REQUEST_DEADLINE};
use serde_json::json;
use tokio::time::{advance, timeout, Duration, Instant};

#[tokio::test(start_paused = true)]
async fn timed_out_reply_keeps_queued_keys_in_the_serving_writer() {
    let generation = Generation(1);
    let pane = PaneId(41);
    let (tx, mut queued) = mpsc::unbounded_channel();
    let writer = PaneWriter {
        tx,
        generation,
        backlog: Arc::new(Backlog::default()),
    };
    let body = |data| {
        json!({
            "type": "terminal_input",
            "terminal_id": "terminal-race",
            "attachment_id": "attachment-race",
            "data": data,
        })
    };
    assert!(writer.enqueue(generation, 1, body("y"), 1).unwrap());
    let QueuedWrite::Write {
        client_write_seq,
        generation: sent_generation,
        body: sent_body,
        bytes,
    } = queued.try_recv().expect("first write leaves the queue")
    else {
        panic!("expected the first input write");
    };

    // The first frame has left the queue, but its reply never arrives.
    // Arm its real timeout before admitting the next key, then expire it
    // before the worker examines that queued key.
    let (armed_tx, armed_rx) = tokio::sync::oneshot::channel();
    let reply = tokio::spawn(async move {
        let deadline = timeout(REQUEST_DEADLINE, std::future::pending::<Value>());
        armed_tx.send(()).expect("report the armed reply deadline");
        deadline.await.map_err(|_| DaemonError::Timeout {
            request: "terminal_input".into(),
        })
    });
    armed_rx.await.expect("reply deadline armed");
    let mut inflight = VecDeque::from([InFlight {
        client_write_seq,
        generation: sent_generation,
        attachment: sent_body["attachment_id"].as_str().unwrap().into(),
        bytes,
        reply,
    }]);
    let queued_at = Instant::now();
    assert!(writer.enqueue(generation, 2, body("k"), 1).unwrap());
    assert_eq!(queued.len(), 1, "the next key precedes timeout settlement");
    advance(REQUEST_DEADLINE + Duration::from_millis(1)).await;
    let settled = settle_front(&mut inflight, pane).await;
    assert_eq!(settled.outcome.tag.id, 1);
    assert!(Instant::now().duration_since(queued_at) >= REQUEST_DEADLINE);
    let (outcomes, mut outcome_rx) = mpsc::unbounded_channel();
    assert!(deliver(settled, &writer.backlog, &outcomes));
    assert!(matches!(
        outcome_rx.try_recv().unwrap().result,
        JobResult::WriteUnconfirmed { .. }
    ));
    assert!(
        writer.serves(generation),
        "a lost reply must not retire the writer holding an already queued key"
    );
    let QueuedWrite::Write {
        client_write_seq,
        body,
        ..
    } = queued.try_recv().expect("the queued key remains available")
    else {
        panic!("expected the next input write");
    };
    assert_eq!(client_write_seq, 2);
    assert_eq!(body["data"], "k");
    assert!(!writer.serves(Generation(2)));
    writer.retire();
    assert!(
        !writer.serves(generation),
        "explicit retirement still stops it"
    );
}
