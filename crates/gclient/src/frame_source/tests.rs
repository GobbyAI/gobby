//! Cancellation behavior of the unix-socket frame source.

use super::*;
use std::sync::atomic::Ordering;

use gobby_terminal::protocol::write_message;
use tokio::io::AsyncReadExt;

async fn wait_for_progress(progress: &std::sync::atomic::AtomicUsize, expected: usize) {
    timeout(Duration::from_secs(1), async {
        loop {
            if progress.load(Ordering::SeqCst) >= expected {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("frame task made partial progress");
}

async fn assert_peer_eof(mut peer: UnixStream, expected: &[u8]) {
    let mut received = Vec::new();
    timeout(Duration::from_secs(1), peer.read_to_end(&mut received))
        .await
        .expect("frame source closed its socket")
        .expect("peer read succeeds");
    assert_eq!(received, expected);
}

#[tokio::test]
async fn reader_cancellation_after_partial_frame_retires_whole_source() {
    for read_limit in [2, 6] {
        let (stream, mut peer) = UnixStream::pair().expect("socket pair");
        let mut source = UnixSocketFrameSource::from_stream(stream, VecDeque::new());
        let progress = Arc::clone(&source.read_progress);
        let message = ServerMessage::Welcome {
            host_epoch: "epoch-cancel".into(),
        };
        let mut encoded = Vec::new();
        write_message(&mut encoded, &message).expect("encode server message");

        peer.write_all(&encoded[..read_limit])
            .await
            .expect("write partial server frame");
        wait_for_progress(&progress, read_limit).await;
        source.cancel_reader_task();

        assert!(matches!(source.recv().await, Err(FrameError::Cancelled)));
        assert!(matches!(
            source
                .send(&ClientMessage::SetScrollOffset {
                    rows_from_live_edge: 1,
                })
                .await,
            Err(FrameError::Cancelled)
        ));
        assert_peer_eof(peer, &[]).await;
    }
}

#[tokio::test]
async fn writer_cancellation_after_partial_frame_retires_whole_source() {
    for write_limit in [2, 6] {
        let (stream, peer) = UnixStream::pair().expect("socket pair");
        let mut source = UnixSocketFrameSource::from_stream(stream, VecDeque::new());
        source
            .write_pause_after
            .store(write_limit, Ordering::SeqCst);
        let progress = Arc::clone(&source.write_progress);
        let retired = Arc::clone(&source.retired);
        let shutdown = source.shutdown.clone();
        let message = ClientMessage::SetScrollOffset {
            rows_from_live_edge: u32::MAX,
        };
        let mut encoded = Vec::new();
        write_message(&mut encoded, &message).expect("encode client message");
        assert!(encoded.len() > write_limit);

        let cancel = tokio::spawn(async move {
            wait_for_progress(&progress, write_limit).await;
            set_retired(&retired, RetireReason::Cancelled);
            let _ = shutdown.send(true);
        });

        assert!(matches!(
            source.send(&message).await,
            Err(FrameError::Cancelled)
        ));
        cancel.await.expect("cancellation task completed");
        assert!(matches!(
            source.send(&message).await,
            Err(FrameError::Cancelled)
        ));
        assert!(matches!(source.recv().await, Err(FrameError::Cancelled)));
        assert_peer_eof(peer, &encoded[..write_limit]).await;
    }
}
