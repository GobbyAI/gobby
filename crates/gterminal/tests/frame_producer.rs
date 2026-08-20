//! Drive a live PTY through the frame-producer API with no ratatui::Frame.

use std::path::Path;
use std::time::Duration;

use bytes::Bytes;
use gobby_terminal::pane::{PaneShellConfig, ShellMode};
use gobby_terminal::protocol::FrameData;
use gobby_terminal::runtime::TerminalRuntime;
use gobby_terminal::terminal_theme::TerminalTheme;

fn frame_text(frame: &FrameData) -> String {
    frame
        .cells
        .chunks(frame.width as usize)
        .map(|row| {
            row.iter()
                .map(|cell| cell.symbol.as_str())
                .collect::<String>()
                .trim_end()
                .to_string()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[tokio::test]
async fn end_to_end_without_ratatui_frame() {
    let cwd = std::env::temp_dir();
    assert!(Path::new(&cwd).is_dir());

    let runtime = TerminalRuntime::spawn(
        24,
        80,
        cwd,
        64 * 1024,
        TerminalTheme::default(),
        None,
        PaneShellConfig::new("", ShellMode::NonLogin),
    )
    .expect("spawn interactive shell");

    runtime
        .send_bytes(Bytes::from_static(b"printf 'hello-from-gterm\\n'\n"))
        .await
        .expect("write to pty");

    let marker = "hello-from-gterm";
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    let mut frame = runtime.frame_data(80, 24);
    while !frame_text(&frame).contains(marker) {
        assert!(
            tokio::time::Instant::now() < deadline,
            "timed out waiting for PTY output in FrameData; got:\n{}",
            frame_text(&frame)
        );
        tokio::time::sleep(Duration::from_millis(50)).await;
        frame = runtime.frame_data(80, 24);
    }

    let _ = runtime.dirty_patch();
    let _ = runtime.osc_title();
    let _ = runtime.osc_progress();

    runtime.resize(30, 100, 0, 0);
    let resized = runtime.frame_data(100, 30);
    assert_eq!(resized.width, 100);
    assert_eq!(resized.height, 30);
    assert_eq!(resized.cells.len(), 100 * 30);

    runtime.shutdown();
}
