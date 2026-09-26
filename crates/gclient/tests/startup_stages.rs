use std::time::{Duration, Instant};

use gobby_client::app::startup_stages::{StageState, StartupStage, StartupStages};

#[test]
fn stages_advance_in_order_and_report_took_and_waiting() {
    use StartupStage::{DaemonHealth, FirstFrame, Roster, WorkspaceAttach};

    let start = Instant::now();
    let mut stages = StartupStages::begin(start);
    assert_eq!(stages.running(), None);
    assert!(!stages.finished());

    stages.mark_running(DaemonHealth, start);
    stages.now = start + Duration::from_millis(310);
    assert_eq!(stages.running(), Some(DaemonHealth));
    assert_eq!(
        stages.elapsed(DaemonHealth),
        Some(Duration::from_millis(310))
    );
    stages.mark_done(DaemonHealth, stages.now);

    let attach_start = stages.now;
    stages.mark_running(WorkspaceAttach, attach_start);
    stages.mark_done(WorkspaceAttach, attach_start + Duration::from_millis(2100));
    let roster_start = attach_start + Duration::from_millis(2100);
    stages.mark_running(Roster, roster_start);
    stages.mark_done(Roster, roster_start + Duration::from_millis(420));
    let frame_start = roster_start + Duration::from_millis(420);
    stages.mark_running(FirstFrame, frame_start);
    stages.mark_done(FirstFrame, frame_start + Duration::from_millis(80));

    assert!(stages.finished());
    assert_eq!(stages.running(), None);
    assert_eq!(stages.elapsed(FirstFrame), Some(Duration::from_millis(80)));
    assert_eq!(
        stages.summary(),
        "health=0.31s attach=2.10s roster=0.42s first_frame=0.08s total=2.91s"
    );

    let waiting = StartupStages::for_test(
        [
            StageState::Done {
                took: Duration::from_millis(310),
            },
            StageState::Running { since: start },
            StageState::Pending,
            StageState::Pending,
        ],
        start + Duration::from_secs(9),
    );
    assert_eq!(waiting.running(), Some(WorkspaceAttach));
    assert_eq!(
        waiting.elapsed(WorkspaceAttach),
        Some(Duration::from_secs(9))
    );
    assert!(!waiting.finished());
    assert_eq!(WorkspaceAttach.label(), "workspace attach");
}
