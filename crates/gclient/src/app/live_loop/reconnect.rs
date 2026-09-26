//! Reconnect scheduling, outcomes, and outage notifications.

use super::*;

pub(super) async fn recv_daemon_event(
    events: &mut Option<EventReceiver>,
) -> Result<DaemonEvent, tokio::sync::broadcast::error::RecvError> {
    match events {
        Some(events) => events.recv().await,
        None => std::future::pending().await,
    }
}

pub(super) async fn await_reconnect_job(
    job: &mut Option<ReconnectFuture>,
) -> Result<Generation, DaemonError> {
    match job {
        Some(job) => job.as_mut().await,
        None => std::future::pending().await,
    }
}

pub(super) async fn wait_for_reconnect(ready_at: Option<Instant>) {
    match ready_at {
        Some(ready_at) => tokio::time::sleep_until(ready_at).await,
        None => std::future::pending().await,
    }
}

pub(super) fn begin_reconnect(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    supervisor: &mut ReconnectSupervisor,
    daemon: &LiveDaemon,
    error: DaemonError,
) {
    if workspace.daemon_ready
        && matches!(
            &error,
            DaemonError::Unavailable { .. } | DaemonError::GoingAway | DaemonError::Timeout { .. }
        )
    {
        chrome.notify(Toast::error(format!(
            "Daemon unreachable at {}: {error}",
            chrome.connection.url
        )));
    }
    let generation = daemon.generation();
    drop(supervisor.request(generation));
    workspace.observe_daemon_disconnect(generation, error);
    chrome.connection.retry_at = supervisor.next_attempt_at().map(Instant::into_std);
}

pub(super) async fn handle_live_event(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    supervisor: &mut ReconnectSupervisor,
    event: DaemonEvent,
) -> Result<(), DaemonError> {
    if let DaemonEvent::Disconnected { generation, error } = &event {
        if workspace.daemon_ready {
            chrome.notify(Toast::error(format!(
                "Daemon unreachable at {}: {error}",
                chrome.connection.url
            )));
        }
        drop(supervisor.request(*generation));
        chrome.connection.retry_at = supervisor.next_attempt_at().map(Instant::into_std);
    }
    if let DaemonEvent::Message(message) = &event {
        apply_live_write_outcome(workspace, message);
    }
    workspace.apply_live_event(event).await?;
    Ok(())
}

pub(super) fn handle_reconnect_outcome(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    events: &mut Option<EventReceiver>,
    supervisor: &mut ReconnectSupervisor,
    outcome: ReconnectAttempt,
) -> Option<(Generation, EventReceiver)> {
    match outcome {
        ReconnectAttempt::Reconnected(generation) => {
            // Subscribe before refetching so events that arrive during the
            // workspace and roster requests remain buffered in this receiver.
            let (status, receiver) = Daemon::subscribe(workspace.daemon());
            *events = None;
            if !status.ready {
                let error = status
                    .last_error
                    .unwrap_or(DaemonError::Unavailable { retry_after: None });
                workspace.observe_daemon_disconnect(generation, error.clone());
                supervisor.handshake_failed(error);
                chrome.connection.retry_at = supervisor.next_attempt_at().map(Instant::into_std);
                return None;
            }
            Some((generation, receiver))
        }
        ReconnectAttempt::RetryScheduled { .. } | ReconnectAttempt::Idle => None,
    }
}

/// One toast per sidebar refetch outage: the first failure raises it, later
/// ones stay quiet, and a success re-arms it.
pub(super) fn settle_sidebar_banner(
    chrome: &mut Chrome,
    shown: &mut bool,
    error: Option<&DaemonError>,
) {
    match error {
        Some(error) if !*shown => {
            chrome.notify(Toast::error(error.to_string()));
            *shown = true;
        }
        Some(_) => {}
        None => *shown = false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sidebar_banner_raises_one_toast_per_outage() {
        let mut chrome = Chrome::dark();
        let mut shown = false;
        let timeout = DaemonError::timeout("GET /api/terminals");
        settle_sidebar_banner(&mut chrome, &mut shown, Some(&timeout));
        settle_sidebar_banner(&mut chrome, &mut shown, Some(&timeout));
        assert_eq!(chrome.toasts.len(), 1);
        assert_eq!(
            chrome.toasts[0].toast.title,
            "Daemon did not answer GET /api/terminals in time."
        );
        assert!(shown);
        settle_sidebar_banner(&mut chrome, &mut shown, None);
        assert!(!shown);
        settle_sidebar_banner(&mut chrome, &mut shown, Some(&timeout));
        assert_eq!(chrome.alert_log.len(), 2, "the next outage raises again");
    }

    #[test]
    fn a_dropped_daemon_toasts_the_url_once_per_outage() {
        let daemon = LiveDaemon::unconnected("http://127.0.0.1:60887", "local-token")
            .expect("unconnected daemon");
        let mut workspace = Workspace::live(daemon.clone());
        workspace.daemon_ready = true;
        let mut chrome = Chrome::dark();
        chrome.connection.url = "http://127.0.0.1:60887".into();
        let mut supervisor = ReconnectSupervisor::new();

        for _ in 0..2 {
            begin_reconnect(
                &mut workspace,
                &mut chrome,
                &mut supervisor,
                &daemon,
                DaemonError::Unavailable { retry_after: None },
            );
        }
        assert_eq!(chrome.alert_log.len(), 1, "one toast for the first outage");
        assert_eq!(
            chrome.alert_log[0].title,
            "Daemon unreachable at http://127.0.0.1:60887: Daemon unavailable."
        );
        assert!(chrome.connection.retry_at.is_some(), "retry is visible");

        workspace.daemon_ready = true;
        begin_reconnect(
            &mut workspace,
            &mut chrome,
            &mut supervisor,
            &daemon,
            DaemonError::Unavailable { retry_after: None },
        );
        assert_eq!(chrome.alert_log.len(), 2, "the next outage toasts again");
    }

    #[tokio::test]
    async fn a_disconnected_event_toasts_the_error_once_per_outage() {
        let daemon = LiveDaemon::unconnected("http://127.0.0.1:60887", "local-token")
            .expect("unconnected daemon");
        let mut workspace = Workspace::live(daemon.clone());
        workspace.daemon_ready = true;
        let mut chrome = Chrome::dark();
        chrome.connection.url = "http://127.0.0.1:60887".into();
        let mut supervisor = ReconnectSupervisor::new();
        let disconnected = || DaemonEvent::Disconnected {
            generation: daemon.generation(),
            error: DaemonError::Unavailable { retry_after: None },
        };

        handle_live_event(&mut workspace, &mut chrome, &mut supervisor, disconnected())
            .await
            .expect("first disconnect");
        handle_live_event(&mut workspace, &mut chrome, &mut supervisor, disconnected())
            .await
            .expect("duplicate disconnect");
        assert_eq!(chrome.alert_log.len(), 1, "one toast for the outage");
        assert_eq!(
            chrome.alert_log[0].title,
            "Daemon unreachable at http://127.0.0.1:60887: Daemon unavailable."
        );

        workspace.daemon_ready = true;
        handle_live_event(&mut workspace, &mut chrome, &mut supervisor, disconnected())
            .await
            .expect("next disconnect");
        assert_eq!(chrome.alert_log.len(), 2, "the next outage toasts again");
    }
}
