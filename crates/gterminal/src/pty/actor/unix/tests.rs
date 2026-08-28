use super::*;
use std::{
    io::{Read, Write},
    os::fd::{AsRawFd, FromRawFd, IntoRawFd},
    os::unix::net::UnixStream,
    sync::atomic::{AtomicBool, Ordering},
};

fn test_wake_pair() -> (fd::WakeWriter, OwnedFd) {
    let pipe = fd::create_wake_pipe().expect("wake pipe");
    (pipe.writer, pipe.read_fd)
}

fn actor_with_socket_pair(
    initially_quiesced: bool,
) -> (PtyIoActorHandle, UnixStream, std_mpsc::Receiver<Bytes>) {
    actor_with_socket_pair_and_poll_observer(initially_quiesced, None)
}

fn actor_with_socket_pair_and_poll_observer(
    initially_quiesced: bool,
    poll_observer: Option<std_mpsc::Sender<()>>,
) -> (PtyIoActorHandle, UnixStream, std_mpsc::Receiver<Bytes>) {
    let (actor_socket, peer) = UnixStream::pair().expect("socket pair");
    actor_socket
        .set_nonblocking(true)
        .expect("actor socket nonblocking");
    peer.set_read_timeout(Some(Duration::from_secs(1)))
        .expect("peer timeout");
    let owned = unsafe { OwnedFd::from_raw_fd(actor_socket.into_raw_fd()) };
    let (read_tx, read_rx) = std_mpsc::channel();
    let config = PtyIoActorConfig {
        pane_id: 1,
        master_fd: owned,
        initially_quiesced,
        on_read: Box::new(move |bytes| {
            read_tx
                .send(Bytes::copy_from_slice(bytes))
                .expect("read callback receiver alive");
            PtyReadResult::empty()
        }),
        on_reader_exit: None,
    };
    let handle = if let Some(poll_observer) = poll_observer {
        PtyIoActor::spawn_with_poll_observer(config, poll_observer)
    } else {
        PtyIoActor::spawn(config)
    }
    .expect("actor spawn");
    (handle, peer, read_rx)
}

fn actor_runner_for_unit_test() -> (PtyIoActorRunner, UnixStream) {
    let (actor_socket, peer) = UnixStream::pair().expect("socket pair");
    actor_socket
        .set_nonblocking(true)
        .expect("actor socket nonblocking");
    let owned = unsafe { OwnedFd::from_raw_fd(actor_socket.into_raw_fd()) };
    let (_data_tx, data_rx) = mpsc::channel(ACTOR_COMMAND_BUFFER);
    let (_control_tx, control_rx) = std_mpsc::channel();
    let wake_pipe = fd::create_wake_pipe().expect("wake pipe");
    let runner = PtyIoActorRunner {
        pane_id: 1,
        file: std::fs::File::from(owned),
        data_rx,
        control_rx,
        state: ActorState::Running,
        pending_writes: VecDeque::new(),
        current_write_offset: 0,
        wake_read_fd: wake_pipe.read_fd,
        controls: Arc::new(Mutex::new(SharedPtyControls::default())),
        response_order: Arc::new(Mutex::new(())),
        on_read: Box::new(|_| PtyReadResult::empty()),
        on_reader_exit: None,
        poll_observer: None,
    };
    (runner, peer)
}

#[test]
fn actor_ignores_empty_user_input_write() {
    let (mut runner, _peer) = actor_runner_for_unit_test();

    assert!(!runner.handle_data_command(PtyIoDataCommand::WriteUserInput(Bytes::new())));

    assert!(runner.pending_writes.is_empty());
}

#[test]
fn actor_writes_user_input_to_owned_fd() {
    let (handle, mut peer, _read_rx) = actor_with_socket_pair(false);

    handle
        .try_write_user_input(Bytes::from_static(b"hello"))
        .expect("write command accepted");

    let mut buf = [0u8; 5];
    peer.read_exact(&mut buf).expect("peer receives write");
    assert_eq!(&buf, b"hello");
    handle.shutdown();
}

#[test]
fn actor_wakes_idle_poll_for_user_input() {
    let (poll_tx, poll_rx) = std_mpsc::channel();
    let (handle, mut peer, _read_rx) =
        actor_with_socket_pair_and_poll_observer(false, Some(poll_tx));
    peer.set_read_timeout(Some(Duration::from_millis(500)))
        .expect("peer timeout");
    poll_rx
        .recv_timeout(Duration::from_secs(1))
        .expect("actor entered idle poll");

    let start = Instant::now();
    handle
        .try_write_user_input(Bytes::from_static(b"x"))
        .expect("write command accepted");

    let mut buf = [0u8; 1];
    peer.read_exact(&mut buf)
        .expect("peer receives write without waiting for actor poll timeout");
    assert_eq!(&buf, b"x");
    assert!(
        start.elapsed() < Duration::from_millis(500),
        "actor write should be driven by wake fd, not the idle poll timeout"
    );
    handle.shutdown();
}

#[test]
fn actor_reads_output_while_input_is_backpressured() {
    let (mut actor_socket, mut peer) = UnixStream::pair().expect("socket pair");
    actor_socket
        .set_nonblocking(true)
        .expect("actor socket nonblocking");
    peer.set_read_timeout(Some(Duration::from_secs(1)))
        .expect("peer timeout");

    let fill = [0xAA; 8192];
    let mut prefilled = 0;
    loop {
        match actor_socket.write(&fill) {
            Ok(written) => prefilled += written,
            Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => break,
            Err(err) => panic!("failed to fill actor write buffer: {err}"),
        }
    }
    assert!(prefilled > 0, "actor write buffer should accept some bytes");

    let owned = unsafe { OwnedFd::from_raw_fd(actor_socket.into_raw_fd()) };
    let (read_tx, read_rx) = std_mpsc::channel();
    let handle = PtyIoActor::spawn(PtyIoActorConfig {
        pane_id: 1,
        master_fd: owned,
        initially_quiesced: false,
        on_read: Box::new(move |bytes| {
            read_tx
                .send(Bytes::copy_from_slice(bytes))
                .expect("read callback receiver alive");
            PtyReadResult::empty()
        }),
        on_reader_exit: None,
    })
    .expect("actor spawn");

    let marker = Bytes::from_static(b"queued-input");
    handle
        .try_write_user_input(marker.clone())
        .expect("write command accepted");

    const OUTPUT_LEN: usize = 128 * 1024;
    let mut peer_writer = peer.try_clone().expect("clone peer writer");
    let output_writer = std::thread::spawn(move || {
        peer_writer
            .write_all(&vec![0xBB; OUTPUT_LEN])
            .expect("peer writes sustained output");
    });
    let deadline = Instant::now() + Duration::from_millis(500);
    let mut output_len = 0;
    while output_len < OUTPUT_LEN {
        let remaining = deadline.saturating_duration_since(Instant::now());
        assert!(
            !remaining.is_zero(),
            "actor did not keep reading blocked peer output"
        );
        let output = read_rx
            .recv_timeout(remaining)
            .expect("actor keeps reading while input remains blocked");
        assert!(output.iter().all(|byte| *byte == 0xBB));
        output_len += output.len();
    }
    assert_eq!(output_len, OUTPUT_LEN);
    output_writer.join().expect("output writer joins");

    let mut received_input = vec![0; prefilled + marker.len()];
    peer.read_exact(&mut received_input)
        .expect("peer receives prefill and queued input");
    assert!(received_input[..prefilled].iter().all(|byte| *byte == 0xAA));
    assert_eq!(&received_input[prefilled..], marker.as_ref());
    handle.shutdown();
}

#[test]
fn actor_wakes_idle_poll_for_handoff_control() {
    let (poll_tx, poll_rx) = std_mpsc::channel();
    let (handle, _peer, _read_rx) = actor_with_socket_pair_and_poll_observer(false, Some(poll_tx));
    poll_rx
        .recv_timeout(Duration::from_secs(1))
        .expect("actor entered idle poll");

    let start = Instant::now();
    let handoff_handle = handle.clone();
    let handoff = std::thread::spawn(move || handoff_handle.begin_handoff(Duration::from_secs(1)));

    handoff
        .join()
        .expect("handoff thread joins")
        .expect("handoff control should wake idle actor");
    assert!(
        start.elapsed() < Duration::from_millis(500),
        "handoff control should be driven by wake fd, not the idle poll timeout"
    );
    handle.shutdown();
}

#[test]
fn poll_ignores_pty_hup_without_pty_interest() {
    let (actor_socket, peer) = UnixStream::pair().expect("socket pair");
    actor_socket
        .set_nonblocking(true)
        .expect("actor socket nonblocking");
    drop(peer);
    let wake_pipe = fd::create_wake_pipe().expect("wake pipe");

    let readiness = fd::poll_pty_and_wake(
        actor_socket.as_raw_fd(),
        wake_pipe.read_fd.as_raw_fd(),
        false,
        false,
        10,
    )
    .expect("poll succeeds");

    assert!(!readiness.pty_read_ready);
    assert!(!readiness.pty_write_ready);
    assert!(!readiness.wake_ready);
}

#[test]
fn actor_delivers_fd_reads_to_callback() {
    let (handle, mut peer, read_rx) = actor_with_socket_pair(false);

    peer.write_all(b"from-peer").expect("peer write");

    let read = read_rx
        .recv_timeout(Duration::from_secs(1))
        .expect("actor read callback");
    assert_eq!(read, Bytes::from_static(b"from-peer"));
    handle.shutdown();
}

#[test]
fn begin_handoff_stops_reads_and_rejects_user_writes_until_rollback() {
    let (handle, mut peer, read_rx) = actor_with_socket_pair(false);

    handle
        .begin_handoff(Duration::from_secs(1))
        .expect("handoff quiesced");
    assert!(handle
        .try_write_user_input(Bytes::from_static(b"blocked"))
        .is_err());

    peer.write_all(b"held").expect("peer write during quiesce");
    assert!(
        read_rx.recv_timeout(Duration::from_millis(150)).is_err(),
        "actor must not read while quiesced"
    );

    handle.rollback_handoff().expect("rollback resumes actor");
    let read = read_rx
        .recv_timeout(Duration::from_secs(1))
        .expect("actor reads held bytes after rollback");
    assert_eq!(read, Bytes::from_static(b"held"));

    handle
        .try_write_user_input(Bytes::from_static(b"after"))
        .expect("write accepted after rollback");
    let mut buf = [0u8; 5];
    peer.read_exact(&mut buf).expect("peer receives after");
    assert_eq!(&buf, b"after");
    handle.shutdown();
}

#[test]
fn duplicate_for_handoff_requires_quiesced_actor() {
    let (handle, mut peer, read_rx) = actor_with_socket_pair(false);

    assert!(handle.duplicate_for_handoff().is_err());
    handle
        .begin_handoff(Duration::from_secs(1))
        .expect("handoff quiesced");
    let duplicate = handle
        .duplicate_for_handoff()
        .expect("handoff duplicate created");
    assert!(duplicate >= 0);
    unsafe {
        libc::close(duplicate);
    }
    handle.rollback_handoff().expect("rollback resumes actor");

    peer.write_all(b"still-live").expect("peer write");
    let read = read_rx
        .recv_timeout(Duration::from_secs(1))
        .expect("actor still reads after duplicate closes");
    assert_eq!(read, Bytes::from_static(b"still-live"));
    handle.shutdown();
}

#[test]
fn resize_and_nudge_keep_latest_request_when_command_queue_is_full() {
    let (data_tx, _data_rx) = mpsc::channel(1);
    let (control_tx, _control_rx) = std_mpsc::channel();
    data_tx
        .try_send(PtyIoDataCommand::WriteUserInput(Bytes::from_static(
            b"fill",
        )))
        .expect("fill command queue");
    let controls = Arc::new(Mutex::new(SharedPtyControls::default()));
    let (wake, _wake_read_fd) = test_wake_pair();
    let handle = PtyIoActorHandle {
        data_tx,
        control_tx,
        wake,
        user_writes: Arc::new(Mutex::new(UserWriteGate { accepting: true })),
        controls: Arc::clone(&controls),
        response_order: Arc::new(Mutex::new(())),
    };

    handle.resize(20, 80, 8, 16, vec![Bytes::from_static(b"old")]);
    handle.resize(40, 120, 9, 18, vec![Bytes::from_static(b"new")]);
    handle.nudge_child_redraw_after_handoff(41, 121, 10, 20);
    handle.write_terminal_response(|| Some(Bytes::from_static(b"response")));

    let controls = controls.lock().expect("controls lock");
    assert_eq!(
        controls.resize,
        Some(PtyResizeRequest {
            resize: PtyResize {
                rows: 40,
                cols: 120,
                cell_width_px: 9,
                cell_height_px: 18,
            },
            terminal_responses: vec![Bytes::from_static(b"new")],
        })
    );
    assert_eq!(
        controls.nudge,
        Some(PtyResize {
            rows: 41,
            cols: 121,
            cell_width_px: 10,
            cell_height_px: 20,
        })
    );
    assert_eq!(
        controls.terminal_responses,
        vec![Bytes::from_static(b"response")]
    );
}

#[test]
fn appearance_transition_report_precedes_query_of_new_scheme() {
    let (actor_socket, mut peer) = UnixStream::pair().expect("socket pair");
    actor_socket
        .set_nonblocking(true)
        .expect("actor socket nonblocking");
    let owned = unsafe { OwnedFd::from_raw_fd(actor_socket.into_raw_fd()) };
    let (data_tx, data_rx) = mpsc::channel(ACTOR_COMMAND_BUFFER);
    let (control_tx, control_rx) = std_mpsc::channel();
    let wake_pipe = fd::create_wake_pipe().expect("wake pipe");
    let controls = Arc::new(Mutex::new(SharedPtyControls::default()));
    let response_order = Arc::new(Mutex::new(()));
    let light = Arc::new(AtomicBool::new(false));
    let query_light = Arc::clone(&light);
    let runner = PtyIoActorRunner {
        pane_id: 1,
        file: std::fs::File::from(owned),
        data_rx,
        control_rx,
        state: ActorState::Running,
        pending_writes: VecDeque::new(),
        current_write_offset: 0,
        wake_read_fd: wake_pipe.read_fd,
        controls: Arc::clone(&controls),
        response_order: Arc::clone(&response_order),
        on_read: Box::new(move |_| PtyReadResult {
            terminal_responses: vec![if query_light.load(Ordering::Acquire) {
                Bytes::from_static(b"query-light")
            } else {
                Bytes::from_static(b"query-dark")
            }],
        }),
        on_reader_exit: None,
        poll_observer: None,
    };
    let handle = PtyIoActorHandle {
        data_tx,
        control_tx,
        wake: wake_pipe.writer,
        user_writes: Arc::new(Mutex::new(UserWriteGate { accepting: true })),
        controls,
        response_order,
    };
    let (changed_tx, changed_rx) = std_mpsc::channel();
    let (continue_tx, continue_rx) = std_mpsc::channel();

    let appearance = std::thread::spawn(move || {
        handle.write_terminal_response(|| {
            light.store(true, Ordering::Release);
            changed_tx.send(()).expect("notify appearance change");
            continue_rx.recv().expect("continue appearance report");
            Some(Bytes::from_static(b"live-light"))
        });
    });
    changed_rx.recv().expect("appearance changed");
    peer.write_all(b"query").expect("write query");
    let reader = std::thread::spawn(move || {
        let mut runner = runner;
        assert!(runner.read_once());
        runner
    });
    continue_tx.send(()).expect("release appearance report");
    appearance.join().expect("appearance thread joins");
    let runner = reader.join().expect("reader thread joins");

    assert_eq!(
        runner.pending_writes,
        VecDeque::from([
            Bytes::from_static(b"live-light"),
            Bytes::from_static(b"query-light"),
        ])
    );
}

#[test]
fn resize_writes_terminal_responses_after_applying_resize() {
    let (handle, mut peer, _read_rx) = actor_with_socket_pair(false);
    let response = Bytes::from_static(b"\x1B[48;40;100;720;900t");

    handle.resize(40, 100, 9, 18, vec![response.clone()]);

    let mut buf = vec![0; response.len()];
    peer.read_exact(&mut buf)
        .expect("peer receives resize response");
    assert_eq!(Bytes::from(buf), response);
    handle.shutdown();
}

#[tokio::test]
async fn async_user_input_waits_for_queue_capacity() {
    let (data_tx, mut data_rx) = mpsc::channel(1);
    let (control_tx, _control_rx) = std_mpsc::channel();
    data_tx
        .try_send(PtyIoDataCommand::WriteUserInput(Bytes::from_static(
            b"fill",
        )))
        .expect("fill data queue");
    let (wake, _wake_read_fd) = test_wake_pair();
    let handle = PtyIoActorHandle {
        data_tx,
        control_tx,
        wake,
        user_writes: Arc::new(Mutex::new(UserWriteGate { accepting: true })),
        controls: Arc::new(Mutex::new(SharedPtyControls::default())),
        response_order: Arc::new(Mutex::new(())),
    };

    let write = tokio::spawn(async move {
        handle
            .write_user_input(Bytes::from_static(b"wait-for-capacity"))
            .await
    });
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(
        !write.is_finished(),
        "async input should wait for queue capacity"
    );

    assert!(matches!(
        data_rx.recv().await,
        Some(PtyIoDataCommand::WriteUserInput(_))
    ));
    write
        .await
        .expect("write task joins")
        .expect("write succeeds after capacity opens");
    match data_rx.recv().await {
        Some(PtyIoDataCommand::WriteUserInput(bytes)) => {
            assert_eq!(bytes, Bytes::from_static(b"wait-for-capacity"));
        }
        _ => panic!("expected queued user input"),
    }
}

#[tokio::test]
async fn async_user_input_waiting_for_capacity_is_rejected_after_handoff_begins() {
    let (data_tx, mut data_rx) = mpsc::channel(1);
    let (control_tx, control_rx) = std_mpsc::channel();
    data_tx
        .try_send(PtyIoDataCommand::WriteUserInput(Bytes::from_static(
            b"fill",
        )))
        .expect("fill data queue");
    let (wake, _wake_read_fd) = test_wake_pair();
    let handle = PtyIoActorHandle {
        data_tx,
        control_tx,
        wake,
        user_writes: Arc::new(Mutex::new(UserWriteGate { accepting: true })),
        controls: Arc::new(Mutex::new(SharedPtyControls::default())),
        response_order: Arc::new(Mutex::new(())),
    };
    let write_handle = handle.clone();
    let write = tokio::spawn(async move {
        write_handle
            .write_user_input(Bytes::from_static(b"after-handoff-start"))
            .await
    });
    tokio::time::sleep(Duration::from_millis(50)).await;

    let handoff = std::thread::spawn(move || handle.begin_handoff(Duration::from_secs(1)));
    match control_rx
        .recv_timeout(Duration::from_secs(1))
        .expect("handoff control command")
    {
        PtyIoControlCommand::BeginHandoff(reply) => {
            reply.send(Ok(())).expect("handoff waiter alive");
        }
        _ => panic!("expected begin handoff command"),
    }
    handoff
        .join()
        .expect("handoff thread joins")
        .expect("handoff succeeds");
    assert!(matches!(
        data_rx.recv().await,
        Some(PtyIoDataCommand::WriteUserInput(_))
    ));

    let err = write.await.expect("write task joins").expect_err(
        "write waiting for capacity must be rejected after handoff closes the input gate",
    );
    assert_eq!(err.0, Bytes::from_static(b"after-handoff-start"));
    match tokio::time::timeout(Duration::from_millis(50), data_rx.recv()).await {
        Err(_) | Ok(None) => {}
        Ok(Some(_)) => panic!("rejected write must not be queued"),
    }
}

#[test]
fn handoff_control_is_not_blocked_by_full_data_queue() {
    let (data_tx, _data_rx) = mpsc::channel(1);
    let (control_tx, control_rx) = std_mpsc::channel();
    data_tx
        .try_send(PtyIoDataCommand::WriteUserInput(Bytes::from_static(
            b"fill",
        )))
        .expect("fill data queue");
    let (wake, _wake_read_fd) = test_wake_pair();
    let handle = PtyIoActorHandle {
        data_tx,
        control_tx,
        wake,
        user_writes: Arc::new(Mutex::new(UserWriteGate { accepting: true })),
        controls: Arc::new(Mutex::new(SharedPtyControls::default())),
        response_order: Arc::new(Mutex::new(())),
    };

    let handoff = std::thread::spawn(move || handle.begin_handoff(Duration::from_secs(1)));
    match control_rx
        .recv_timeout(Duration::from_secs(1))
        .expect("handoff control command")
    {
        PtyIoControlCommand::BeginHandoff(reply) => {
            reply.send(Ok(())).expect("handoff waiter alive");
        }
        _ => panic!("expected begin handoff command"),
    }

    handoff
        .join()
        .expect("handoff thread joins")
        .expect("handoff succeeds despite full data queue");
}

#[test]
fn begin_handoff_drains_user_writes_already_in_command_queue() {
    let (actor_socket, mut peer) = UnixStream::pair().expect("socket pair");
    actor_socket
        .set_nonblocking(true)
        .expect("actor socket nonblocking");
    peer.set_read_timeout(Some(Duration::from_secs(1)))
        .expect("peer timeout");
    let (data_tx, data_rx) = mpsc::channel(ACTOR_COMMAND_BUFFER);
    let (_control_tx, control_rx) = std_mpsc::channel();
    data_tx
        .try_send(PtyIoDataCommand::WriteUserInput(Bytes::from_static(
            b"queued-before-ack",
        )))
        .expect("queued write");
    let mut runner = PtyIoActorRunner {
        pane_id: 1,
        file: std::fs::File::from(unsafe { OwnedFd::from_raw_fd(actor_socket.into_raw_fd()) }),
        data_rx,
        control_rx,
        state: ActorState::Running,
        pending_writes: VecDeque::new(),
        current_write_offset: 0,
        wake_read_fd: fd::create_wake_pipe().expect("wake pipe").read_fd,
        controls: Arc::new(Mutex::new(SharedPtyControls::default())),
        response_order: Arc::new(Mutex::new(())),
        on_read: Box::new(|_| PtyReadResult::empty()),
        on_reader_exit: None,
        poll_observer: None,
    };

    runner.begin_handoff().expect("handoff drains queued write");

    let mut buf = [0u8; 17];
    peer.read_exact(&mut buf)
        .expect("queued write reaches peer before quiesce ack");
    assert_eq!(&buf, b"queued-before-ack");
    assert_eq!(runner.state, ActorState::Quiesced);
}

#[test]
fn release_after_commit_prevents_further_io() {
    let (handle, mut peer, read_rx) = actor_with_socket_pair(false);

    handle.release_after_commit().expect("actor released");
    assert!(handle
        .try_write_user_input(Bytes::from_static(b"blocked"))
        .is_err());

    let _ = peer.write_all(b"ignored");
    assert!(read_rx.recv_timeout(Duration::from_millis(150)).is_err());
}
