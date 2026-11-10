use super::*;
use crate::frame_source::claim_writer_lease;

#[tokio::test(start_paused = true)]
async fn a_claim_timeout_queues_detach_on_the_retained_connection() {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token").expect("daemon");
    let (outbound, mut receiver) = mpsc::channel(4);
    daemon.inner.state().outbound = Some(outbound);
    let claim = async { claim_writer_lease(&daemon, "terminal", "attachment").await };
    let writer = async {
        let Some(Outbound::Message { raw, written, .. }) = receiver.recv().await else {
            panic!("claim must enter the connection's write queue");
        };
        let message: Value = serde_json::from_str(&raw).expect("claim JSON");
        assert_eq!(message["type"], "terminal_take_control");
        // Retain the write acknowledgement through the deadline; the connection stays open.
        tokio::time::advance(REQUEST_DEADLINE + Duration::from_millis(1)).await;
        let Some(Outbound::Message {
            raw,
            written: detached,
            ..
        }) = receiver.recv().await
        else {
            panic!("failed claim must queue a best-effort detach");
        };
        let message: Value = serde_json::from_str(&raw).expect("detach JSON");
        assert_eq!(message["type"], "terminal_detach");
        assert_eq!(message["terminal_id"], "terminal");
        assert_eq!(message["attachment_id"], "attachment");
        detached.send(()).expect("detach acknowledged");
        drop(written);
    };
    let (result, ()) = tokio::join!(claim, writer);
    assert!(
        matches!(result, Err(crate::frame_source::FrameError::Other(message))
        if message == "Daemon did not answer terminal_take_control in time.")
    );
    assert!(
        daemon.inner.state().outbound.is_some(),
        "shared connection retained"
    );
}
