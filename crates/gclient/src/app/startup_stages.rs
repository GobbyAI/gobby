use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StartupStage {
    DaemonHealth,
    WorkspaceAttach,
    Roster,
    FirstFrame,
}

impl StartupStage {
    pub fn label(self) -> &'static str {
        match self {
            Self::DaemonHealth => "daemon health",
            Self::WorkspaceAttach => "workspace attach",
            Self::Roster => "roster",
            Self::FirstFrame => "first frame",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StageState {
    Pending,
    Running { since: Instant },
    Done { took: Duration },
}

#[derive(Debug, Clone)]
pub struct StartupStages {
    states: [StageState; 4],
    pub now: Instant,
    started: Instant,
}

#[derive(Debug, Clone)]
pub struct ConnectionView {
    pub url: String,
    pub machine: String,
    pub daemon_version: Option<String>,
    pub launch_project: Option<String>,
    pub stages: Option<StartupStages>,
    pub retry_at: Option<Instant>,
    pub now: Instant,
}

impl Default for ConnectionView {
    fn default() -> Self {
        Self {
            url: String::new(),
            machine: String::new(),
            daemon_version: None,
            launch_project: None,
            stages: None,
            retry_at: None,
            now: Instant::now(),
        }
    }
}

impl StartupStages {
    pub fn begin(now: Instant) -> Self {
        Self {
            states: [StageState::Pending; 4],
            now,
            started: now,
        }
    }

    pub fn mark_running(&mut self, stage: StartupStage, now: Instant) {
        self.now = now;
        self.states[stage as usize] = StageState::Running { since: now };
    }

    pub fn mark_done(&mut self, stage: StartupStage, now: Instant) {
        self.now = now;
        if let StageState::Running { since } = self.states[stage as usize] {
            self.states[stage as usize] = StageState::Done {
                took: now.saturating_duration_since(since),
            };
        }
    }

    pub fn running(&self) -> Option<StartupStage> {
        StartupStage::ALL
            .into_iter()
            .find(|stage| matches!(self.states[*stage as usize], StageState::Running { .. }))
    }

    pub fn finished(&self) -> bool {
        self.states
            .iter()
            .all(|state| matches!(state, StageState::Done { .. }))
    }

    pub fn elapsed(&self, stage: StartupStage) -> Option<Duration> {
        match self.states[stage as usize] {
            StageState::Pending => None,
            StageState::Running { since } => Some(self.now.saturating_duration_since(since)),
            StageState::Done { took } => Some(took),
        }
    }

    pub fn state(&self, stage: StartupStage) -> StageState {
        self.states[stage as usize]
    }

    pub fn for_test(states: [StageState; 4], now: Instant) -> Self {
        Self {
            states,
            now,
            started: now,
        }
    }

    pub fn summary(&self) -> String {
        let seconds = |stage| self.elapsed(stage).unwrap_or_default().as_secs_f64();
        format!(
            "health={:.2}s attach={:.2}s roster={:.2}s first_frame={:.2}s total={:.2}s",
            seconds(StartupStage::DaemonHealth),
            seconds(StartupStage::WorkspaceAttach),
            seconds(StartupStage::Roster),
            seconds(StartupStage::FirstFrame),
            self.now
                .saturating_duration_since(self.started)
                .as_secs_f64(),
        )
    }
}

impl StartupStage {
    pub const ALL: [Self; 4] = [
        Self::DaemonHealth,
        Self::WorkspaceAttach,
        Self::Roster,
        Self::FirstFrame,
    ];
}
