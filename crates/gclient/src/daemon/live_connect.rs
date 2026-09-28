//! Connection lifecycle and daemon transport implementation.

use super::*;

impl LiveDaemon {
    pub async fn config_version(&self) -> Result<Option<String>, DaemonError> {
        self.inner.rest.config_version().await
    }

    pub async fn new(
        base_url: impl AsRef<str>,
        token: impl Into<String>,
    ) -> Result<Self, DaemonError> {
        let daemon = Self::unconnected(base_url, token)?;
        daemon.open_connection(Generation(0)).await?;
        Ok(daemon)
    }

    /// The daemon handle before its first connection.
    pub fn unconnected(
        base_url: impl AsRef<str>,
        token: impl Into<String>,
    ) -> Result<Self, DaemonError> {
        let base_url = Url::parse(base_url.as_ref()).map_err(|error| DaemonError::Protocol {
            detail: error.to_string(),
        })?;
        let token = token.into();
        let rest = RestClient::new(base_url.clone(), token.clone())?;
        let (events, _) = broadcast::channel(BROADCAST_CAPACITY);
        let (closed_tx, _) = watch::channel(false);
        Ok(Self {
            inner: Arc::new(LiveInner {
                rest,
                base_url,
                token,
                state: Mutex::new(LiveState::default()),
                events,
                reader: Mutex::new(None),
                closed_tx,
                reader_active: AtomicBool::new(false),
                sink_active: AtomicBool::new(false),
                reconnect_active: AtomicBool::new(false),
                close_stall: Mutex::new(None),
                owners: AtomicUsize::new(1),
            }),
        })
    }

    pub async fn connect(
        base_url: impl AsRef<str>,
        token: impl Into<String>,
    ) -> Result<Self, DaemonError> {
        Self::new(base_url, token).await
    }

    /// [`Self::connect`] for a launch that waits: a daemon that is down,
    /// away or silent yields a daemon that is not ready and carries the
    /// connect error as `last_error`, the same shape a lost connection
    /// leaves behind, so the live loop's supervisor brings it up. A bad URL,
    /// a refused token or a protocol fault still fail here.
    pub async fn connect_or_wait(
        base_url: impl AsRef<str>,
        token: impl Into<String>,
    ) -> Result<Self, DaemonError> {
        let daemon = Self::unconnected(base_url, token)?;
        match daemon.open_connection(Generation(0)).await {
            Ok(_) => {}
            Err(
                error @ (DaemonError::Unavailable { .. }
                | DaemonError::GoingAway
                | DaemonError::Timeout { .. }),
            ) => {
                let mut state = daemon.inner.state();
                state.ready = false;
                state.last_error = Some(error);
            }
            Err(error) => return Err(error),
        }
        Ok(daemon)
    }

    async fn open_connection(&self, observed: Generation) -> Result<Generation, DaemonError> {
        let socket = connect_socket(
            &self.inner.base_url,
            &self.inner.token,
            self.inner.closed_tx.subscribe(),
        )
        .await?;
        // The reader guard lives in this block: it must not be held across
        // the subscribe await below.
        let generation = {
            let mut reader = self
                .inner
                .reader
                .lock()
                .expect("live daemon reader mutex poisoned");
            let (outbound, receiver) = mpsc::channel(256);
            let generation = {
                let mut state = self.inner.state();
                if state.closed {
                    return Err(DaemonError::Unavailable { retry_after: None });
                }
                if state.generation != observed {
                    return Ok(state.generation);
                }
                state.generation.0 += 1;
                state.ready = true;
                state.last_error = None;
                state.outbound = Some(outbound);
                state.generation
            };
            let inner = Arc::clone(&self.inner);
            let handle = tokio::spawn(async move {
                run_connection(inner, generation, socket, receiver).await;
            });
            // Replacing the slot is enough to retire the reader in it, and
            // that matters when a handshake fails after this point — the
            // subscribe below timing out under `connect_or_wait` leaves the
            // previous socket up, because nothing closed it. The generation
            // above already swapped `state.outbound`, so the old reader's
            // command channel has no senders left: its `recv` resolves to
            // `None` and it disconnects itself. One reader feeds the broadcast
            // channel; `a_timed_out_subscribe_leaves_no_second_reader` pins it.
            *reader = Some(handle);
            generation
        };
        self.subscribe_events().await?;
        Ok(generation)
    }

    /// Ask for the gated event kinds and wait for the daemon's confirmation.
    /// The daemon delivers those kinds only to a socket that subscribed, so
    /// a connection that skipped this would reduce over silence; waiting for
    /// `subscribe_success` keeps the subscription ahead of the first fetch.
    async fn subscribe_events(&self) -> Result<(), DaemonError> {
        let mut events = self.inner.events.subscribe();
        self.notification(json!({"type": "subscribe", "events": SUBSCRIBED_EVENTS}))
            .await?;
        let deadline = Instant::now() + REQUEST_DEADLINE;
        loop {
            let event = timeout_at(deadline, events.recv())
                .await
                .map_err(|_| request_timed_out("subscribe"))?;
            match event {
                Ok(DaemonEvent::Message(message))
                    if crate::daemon::message_kind(&message) == Some("subscribe_success") =>
                {
                    return Ok(());
                }
                Ok(DaemonEvent::Disconnected { error, .. }) => return Err(error),
                Ok(_) | Err(broadcast::error::RecvError::Lagged(_)) => continue,
                Err(broadcast::error::RecvError::Closed) => {
                    return Err(DaemonError::Unavailable { retry_after: None });
                }
            }
        }
    }
}

impl Daemon for LiveDaemon {
    async fn list_terminals(
        &self,
        project: &str,
        cursor: Option<&str>,
    ) -> Result<Page<TerminalRow>, DaemonError> {
        self.inner.rest.list_terminals(project, cursor).await
    }

    async fn inventory_page(
        &self,
        states: &[&str],
        cursor: Option<&str>,
    ) -> Result<Page<TerminalRow>, DaemonError> {
        let reply = self
            .request(json!({
                "type": "terminal_list",
                "request_id": Uuid::new_v4().to_string(),
                "states": states,
                "limit": INVENTORY_PAGE_SIZE,
                "cursor": cursor,
            }))
            .await?;
        serde_json::from_value(reply).map_err(|error| DaemonError::Protocol {
            detail: error.to_string(),
        })
    }

    async fn roster(&self) -> Result<Vec<RosterEntry>, DaemonError> {
        self.inner.rest.roster().await
    }

    async fn projects(&self) -> Result<Vec<ProjectRow>, DaemonError> {
        self.inner.rest.projects().await
    }

    async fn source_status(&self, project: &str) -> Result<SourceStatus, DaemonError> {
        self.inner.rest.source_status(project).await
    }

    async fn worktrees(&self, project: &str) -> Result<Vec<WorktreeRow>, DaemonError> {
        self.inner.rest.worktrees(project).await
    }

    async fn init_project(&self, path: &str) -> Result<ProjectRow, DaemonError> {
        self.inner.rest.init_project(path).await
    }

    async fn create_worktree(
        &self,
        project: &str,
        branch: &str,
        base: Option<&str>,
    ) -> Result<WorktreeRow, DaemonError> {
        self.inner.rest.create_worktree(project, branch, base).await
    }

    async fn delete_worktree(&self, worktree_id: &str) -> Result<(), DaemonError> {
        self.inner.rest.delete_worktree(worktree_id).await
    }

    async fn sessions(&self, project: &str) -> Result<Vec<SessionRow>, DaemonError> {
        self.inner.rest.sessions(project).await
    }

    async fn agent_runs(&self, project: &str) -> Result<Vec<RunRow>, DaemonError> {
        self.inner.rest.agent_runs(project).await
    }

    async fn respond(
        &self,
        entry: &str,
        attention_id: &str,
        answer: &Answer,
    ) -> Result<(), DaemonError> {
        self.inner.rest.respond(entry, attention_id, answer).await
    }

    async fn mark_seen(&self, entry: &str, attention_id: &str) -> Result<(), DaemonError> {
        self.inner.rest.mark_seen(entry, attention_id).await
    }

    async fn spawn(&self, request: SpawnRequest) -> Result<SpawnOutcome, DaemonError> {
        let mut message = json!({
            "type": "terminal_create",
            "request_id": Uuid::new_v4().to_string(),
            "rows": request.rows,
            "cols": request.cols,
            "command": request.command,
        });
        if let Some(cwd) = request.cwd {
            message["cwd"] = Value::String(cwd);
        }
        if let Some(project_id) = request.project_id {
            message["project_id"] = Value::String(project_id);
        }
        self.theme_request(&mut message);
        let reply = self.request(message).await?;
        if reply.get("success").and_then(Value::as_bool) != Some(true) {
            return Ok(SpawnOutcome::Refused {
                reason: reply
                    .get("reason")
                    .and_then(Value::as_str)
                    .unwrap_or("refused")
                    .to_string(),
            });
        }
        Ok(SpawnOutcome::Created {
            terminal_id: required_string(&reply, "terminal_id")?,
            backend: required_string(&reply, "backend")?,
        })
    }

    async fn terminate(&self, terminal_id: &str) -> Result<KillOutcome, DaemonError> {
        let reply = self
            .request(json!({
                "type": "terminal_kill",
                "request_id": Uuid::new_v4().to_string(),
                "terminal_id": terminal_id,
            }))
            .await?;
        if reply.get("success").and_then(Value::as_bool) == Some(true) {
            Ok(KillOutcome::Killed {
                terminal_id: terminal_id.to_string(),
            })
        } else {
            Ok(KillOutcome::Refused {
                terminal_id: terminal_id.to_string(),
            })
        }
    }

    fn subscribe(&self) -> (SubscribeSnapshot, EventReceiver) {
        let receiver = self.inner.events.subscribe().into();
        let state = self.inner.state();
        (
            SubscribeSnapshot {
                generation: state.generation,
                ready: state.ready,
                last_error: state.last_error.clone(),
            },
            receiver,
        )
    }

    async fn send(&self, message: Value) -> Result<Value, DaemonError> {
        self.request(message).await
    }

    async fn notify(&self, message: Value) -> Result<(), DaemonError> {
        self.notification(message).await
    }

    async fn reconnect(&self, observed: Generation) -> Result<Generation, DaemonError> {
        let (mut joiner, owner, observed) = {
            let mut state = self.inner.state();
            if state.closed {
                return Err(DaemonError::Unavailable { retry_after: None });
            }
            if state.generation != observed && state.ready {
                return Ok(state.generation);
            }
            // A stale caller with no ready connection and no flight still needs
            // a connection: a handshake that failed after rolling the generation
            // forward would otherwise strand every later attempt on the cached
            // error (#22002).
            let observed = state.generation;
            if let Some(flight) = &state.reconnect {
                (Some(flight.result_rx.clone()), None, observed)
            } else {
                let (result_tx, result_rx) = watch::channel(None);
                let done = Arc::new(Notify::new());
                let (abort, registration) = AbortHandle::new_pair();
                state.reconnect = Some(ReconnectFlight {
                    observed,
                    result_tx: result_tx.clone(),
                    result_rx,
                    done: Arc::clone(&done),
                    abort,
                });
                (None, Some((result_tx, done, registration)), observed)
            }
        };
        if let Some(receiver) = joiner.as_ref() {
            if let Some(result) = receiver.borrow().clone() {
                return result;
            }
        }
        if let Some(receiver) = joiner.as_mut() {
            receiver
                .changed()
                .await
                .map_err(|_| DaemonError::Unavailable { retry_after: None })?;
            return receiver
                .borrow()
                .clone()
                .unwrap_or(Err(DaemonError::Unavailable { retry_after: None }));
        }
        let (result_tx, done, registration) = owner.expect("reconnect owner or joiner");
        self.inner.reconnect_active.store(true, Ordering::Release);
        let mut owner_guard = ReconnectOwnerGuard {
            daemon: self,
            observed,
            result_tx: result_tx.clone(),
            done: Arc::clone(&done),
            armed: true,
        };
        let mut result = Abortable::new(self.open_connection(observed), registration)
            .await
            .unwrap_or(Err(DaemonError::Unavailable { retry_after: None }));
        {
            let mut state = self.inner.state();
            if state.closed {
                result = Err(DaemonError::Unavailable { retry_after: None });
            }
            if result.is_err() {
                state.ready = false;
                state.last_error = result.as_ref().err().cloned();
            }
            result_tx.send_replace(Some(result.clone()));
            if state
                .reconnect
                .as_ref()
                .is_some_and(|flight| flight.observed == observed)
            {
                state.reconnect = None;
            }
        }
        done.notify_one();
        self.inner.reconnect_active.store(false, Ordering::Release);
        owner_guard.armed = false;
        result
    }

    async fn close(&self, deadline: Instant) -> Result<(), DaemonError> {
        let (outbound, reconnect, reader) = {
            let mut reader_slot = self
                .inner
                .reader
                .lock()
                .expect("live daemon reader mutex poisoned");
            let mut state = self.inner.state();
            if state.closed {
                return Ok(());
            }
            state.closed = true;
            state.ready = false;
            let reconnect = state.reconnect.as_ref().map(|flight| {
                flight
                    .result_tx
                    .send_replace(Some(Err(DaemonError::Unavailable { retry_after: None })));
                (Arc::clone(&flight.done), flight.abort.clone())
            });
            let reader = reader_slot.take();
            (state.outbound.take(), reconnect, reader)
        };
        let mut resources = CloseResources {
            inner: Arc::clone(&self.inner),
            reader,
            reconnect_abort: reconnect.as_ref().map(|(_, abort)| abort.clone()),
            armed: true,
        };
        self.inner
            .fail_waiters(DaemonError::Unavailable { retry_after: None });
        if deadline <= Instant::now() {
            return Err(DaemonError::timeout("close"));
        }
        let close_result = if let Some(outbound) = outbound {
            let (done, done_rx) = oneshot::channel();
            match timeout_at(deadline, outbound.send(Outbound::Close { done })).await {
                Err(_) => Err(DaemonError::timeout("close")),
                Ok(Err(_)) => Ok(()),
                Ok(Ok(())) => match timeout_at(deadline, done_rx).await {
                    Err(_) => Err(DaemonError::timeout("close")),
                    Ok(_) => Ok(()),
                },
            }
        } else {
            Ok(())
        };
        close_result?;
        if let Some(handle) = resources.reader.as_mut() {
            if timeout_at(deadline, &mut *handle).await.is_err() {
                return Err(DaemonError::timeout("close"));
            }
        }
        if let Some((done, abort)) = reconnect {
            self.inner
                .stall_close_stage(CloseStage::ReconnectJoin)
                .await;
            abort.abort();
            timeout_at(deadline, done.notified())
                .await
                .map_err(|_| DaemonError::timeout("close"))?;
        }
        self.inner.closed_tx.send_replace(true);
        resources.armed = false;
        Ok(())
    }

    async fn attach_workspace(
        &self,
        node: Option<&str>,
        workspace: Option<&str>,
        project_id: Option<&str>,
    ) -> Result<WorkspaceSnapshot, DaemonError> {
        self.attach_workspace_live(node, workspace, project_id)
            .await
    }

    async fn workspace_op(&self, op: WorkspaceOp) -> Result<WorkspaceReply, DaemonError> {
        self.workspace_op_live(op).await
    }
}

struct CloseResources {
    inner: Arc<LiveInner>,
    reader: Option<JoinHandle<()>>,
    reconnect_abort: Option<AbortHandle>,
    armed: bool,
}

impl Drop for CloseResources {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        self.inner.closed_tx.send_replace(true);
        if let Some(abort) = self.reconnect_abort.take() {
            abort.abort();
        }
        if let Some(reader) = self.reader.take() {
            reader.abort();
        }
    }
}

struct ReconnectOwnerGuard<'a> {
    daemon: &'a LiveDaemon,
    observed: Generation,
    result_tx: watch::Sender<Option<Result<Generation, DaemonError>>>,
    done: Arc<Notify>,
    armed: bool,
}

impl Drop for ReconnectOwnerGuard<'_> {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        self.daemon
            .inner
            .reconnect_active
            .store(false, Ordering::Release);
        let error = DaemonError::Unavailable { retry_after: None };
        {
            let mut state = self.daemon.inner.state();
            state.ready = false;
            state.last_error = Some(error.clone());
            if state
                .reconnect
                .as_ref()
                .is_some_and(|flight| flight.observed == self.observed)
            {
                state.reconnect = None;
            }
        }
        self.result_tx.send_replace(Some(Err(error)));
        self.done.notify_one();
    }
}
