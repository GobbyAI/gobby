//! 3.1.6 / 3.1.7: herdr defaults, client-local overrides, collisions.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use gobby_client::ui::keymap::{Action, Keymap, KeymapError, Trigger, BINDINGS};
use std::collections::HashSet;
use std::fs;

fn key(code: KeyCode, modifiers: KeyModifiers) -> KeyEvent {
    KeyEvent::new(code, modifiers)
}

fn ch(c: char) -> KeyEvent {
    key(KeyCode::Char(c), KeyModifiers::NONE)
}

fn shift(c: char) -> KeyEvent {
    key(KeyCode::Char(c), KeyModifiers::SHIFT)
}

fn chords_by_name(keymap: &Keymap) -> Vec<(&'static str, Vec<Trigger>)> {
    keymap
        .bindings
        .iter()
        .map(|b| (b.name, b.chords.iter().map(|c| c.trigger).collect()))
        .collect()
}

#[test]
fn overrides_preserve_defaults_and_cannot_activate_deferred_actions() {
    let defaults = Keymap::defaults();

    // herdr v0.8.0 map, spot-checked against config/model.rs KeysConfig::default().
    assert!(defaults.is_prefix(&key(KeyCode::Char('b'), KeyModifiers::CONTROL)));
    assert!(!defaults.is_prefix(&key(KeyCode::Char('a'), KeyModifiers::CONTROL)));
    let prefix_map = [
        (ch('?'), Action::Help),
        (ch('s'), Action::Settings),
        (shift('N'), Action::NewTerminal),
        (shift('W'), Action::RenameTerminal),
        (shift('D'), Action::CloseTerminal),
        (ch('w'), Action::TerminalPicker),
        (ch('g'), Action::Goto),
        (ch('q'), Action::Detach),
        (shift('R'), Action::ReloadConfig),
        (ch('o'), Action::OpenNotificationTarget),
        (ch('c'), Action::NewTab),
        (shift('T'), Action::RenameTab),
        (ch('p'), Action::PreviousTab),
        (ch('n'), Action::NextTab),
        (ch('1'), Action::SwitchTab(1)),
        (ch('9'), Action::SwitchTab(9)),
        (shift('X'), Action::CloseTab),
        (shift('P'), Action::RenamePane),
        (ch('e'), Action::EditScrollback),
        (ch('['), Action::CopyMode),
        (ch('h'), Action::FocusPaneLeft),
        (ch('j'), Action::FocusPaneDown),
        (ch('k'), Action::FocusPaneUp),
        (ch('l'), Action::FocusPaneRight),
        (shift('H'), Action::SwapPaneLeft),
        (shift('J'), Action::SwapPaneDown),
        (shift('K'), Action::SwapPaneUp),
        (shift('L'), Action::SwapPaneRight),
        (key(KeyCode::Tab, KeyModifiers::NONE), Action::CyclePaneNext),
        (
            key(KeyCode::BackTab, KeyModifiers::SHIFT),
            Action::CyclePanePrevious,
        ),
        (ch('v'), Action::SplitVertical),
        (ch('-'), Action::SplitHorizontal),
        (ch('x'), Action::ClosePane),
        (ch('z'), Action::Zoom),
        (ch('r'), Action::ResizeMode),
        (ch('b'), Action::ToggleSidebar),
        (ch('t'), Action::TakeControl),
        (ch('u'), Action::ReleaseControl),
        (shift('A'), Action::TakeBack),
        (ch('a'), Action::Respond),
        (shift('Q'), Action::Quit),
    ];
    // Direct (no prefix) chords: sidebar navigation reuses h/j/k/l, so those
    // events resolve differently with and without the prefix.
    let direct_map = [
        (key(KeyCode::Up, KeyModifiers::NONE), Action::NavigateUp),
        (key(KeyCode::Down, KeyModifiers::NONE), Action::NavigateDown),
        (ch('h'), Action::NavigatePaneLeft),
        (ch('j'), Action::NavigatePaneDown),
        (ch('k'), Action::NavigatePaneUp),
        (ch('l'), Action::NavigatePaneRight),
    ];
    for (event, action) in prefix_map {
        assert_eq!(defaults.lookup_prefix(&event), Some(action), "{event:?}");
        if !direct_map.iter().any(|(direct, _)| *direct == event) {
            assert_eq!(
                defaults.lookup_direct(&event),
                None,
                "{event:?} is prefix-only"
            );
        }
    }
    for (event, action) in direct_map {
        assert_eq!(defaults.lookup_direct(&event), Some(action), "{event:?}");
    }
    for name in [
        "previous_terminal",
        "next_terminal",
        "previous_attention",
        "next_attention",
        "focus_attention",
        "switch_terminal",
        "last_pane",
    ] {
        assert!(
            defaults.binding(name).unwrap().chords.is_empty(),
            "{name} is unset"
        );
    }
    assert_eq!(defaults.binding("switch_tab").unwrap().chords.len(), 9);
    for spec in BINDINGS {
        let binding = defaults.binding(spec.name).unwrap();
        assert_eq!(binding.reserved, spec.reserved, "{}", spec.name);
    }
    assert_eq!(defaults.bindings.len(), BINDINGS.len());

    // Repository-checkout and mobile actions do not exist here.
    for name in [
        "new_worktree",
        "open_worktree",
        "remove_worktree",
        "mobile_menu",
    ] {
        assert!(defaults.binding(name).is_none(), "{name}");
        let err =
            Keymap::from_toml(&format!("[bindings]\n{name} = \"prefix+shift+g\"\n")).unwrap_err();
        assert_eq!(err, KeymapError::UnknownAction(name.to_string()));
    }

    // A client-local override replaces exactly one action by name.
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("keymap.toml");
    fs::write(&path, "[bindings]\nhelp = \"prefix+f1\"\n").unwrap();
    let merged = Keymap::load_overrides(&path).unwrap();
    assert_eq!(
        merged.lookup_prefix(&key(KeyCode::F(1), KeyModifiers::NONE)),
        Some(Action::Help)
    );
    assert_eq!(
        merged.lookup_prefix(&ch('?')),
        None,
        "replaced chord is released"
    );
    let before = chords_by_name(&defaults);
    let after = chords_by_name(&merged);
    for ((name, old), (_, new)) in before.iter().zip(after.iter()) {
        if *name == "help" {
            assert_ne!(old, new);
        } else {
            assert_eq!(old, new, "{name} changed by an unrelated override");
        }
    }
    assert!(merged.is_prefix(&key(KeyCode::Char('b'), KeyModifiers::CONTROL)));

    // Multiple chords, a prefix override, and a missing file.
    fs::write(
        &path,
        "prefix = \"ctrl+a\"\n[bindings]\nnext_tab = [\"prefix+n\", \"ctrl+alt+]\"]\n",
    )
    .unwrap();
    let merged = Keymap::load_overrides(&path).unwrap();
    assert!(merged.is_prefix(&key(KeyCode::Char('a'), KeyModifiers::CONTROL)));
    assert!(!merged.is_prefix(&key(KeyCode::Char('b'), KeyModifiers::CONTROL)));
    assert_eq!(merged.lookup_prefix(&ch('n')), Some(Action::NextTab));
    assert_eq!(
        merged.lookup_direct(&key(
            KeyCode::Char(']'),
            KeyModifiers::CONTROL | KeyModifiers::ALT
        )),
        Some(Action::NextTab)
    );
    let missing = Keymap::load_overrides(&dir.path().join("absent.toml")).unwrap();
    assert_eq!(chords_by_name(&missing), chords_by_name(&defaults));

    // Reserved actions stay non-dispatchable and hidden even when named.
    let err = Keymap::from_toml("[bindings]\ncustom_command = \"prefix+shift+m\"\n").unwrap_err();
    assert_eq!(
        err,
        KeymapError::ReservedAction("custom_command".to_string())
    );
    assert_eq!(
        defaults.lookup_prefix(&ch('m')),
        None,
        "reserved chord never dispatches"
    );
    let help = defaults.help_entries();
    assert!(help.iter().any(|e| e.name == "split_vertical"));
    assert!(help.iter().all(|e| e.name != "custom_command"));
    assert!(help.iter().all(|e| !e.keys.contains("prefix+m")));

    // Malformed input is a parse error, not a panic.
    assert!(matches!(
        Keymap::from_toml("[bindings\nhelp = 1"),
        Err(KeymapError::Parse(_))
    ));
    assert_eq!(
        Keymap::from_toml("[bindings]\nhelp = \"prefix+\"\n").unwrap_err(),
        KeymapError::InvalidChord {
            action: "help".to_string(),
            chord: "prefix+".to_string()
        }
    );
}

#[test]
fn colliding_override_is_rejected_and_defaults_survive() {
    let err = Keymap::from_toml("[bindings]\nsplit_vertical = \"prefix+x\"\n").unwrap_err();
    assert_eq!(
        err,
        KeymapError::Collision {
            chord: "prefix+x".to_string(),
            action: "split_vertical".to_string(),
            displaced: "close_pane".to_string(),
        }
    );
    let message = err.to_string();
    for needle in ["prefix+x", "split_vertical", "close_pane"] {
        assert!(message.contains(needle), "{message}");
    }
    let defaults = Keymap::defaults();
    assert_eq!(defaults.lookup_prefix(&ch('x')), Some(Action::ClosePane));
    assert_eq!(
        defaults.lookup_prefix(&ch('v')),
        Some(Action::SplitVertical)
    );

    // Two overrides in one file colliding with each other.
    let err = Keymap::from_toml("[bindings]\nzoom = \"prefix+f2\"\nresize_mode = \"prefix+f2\"\n")
        .unwrap_err();
    assert!(
        matches!(&err, KeymapError::Collision { chord, .. } if chord == "prefix+f2"),
        "{err:?}"
    );
    // A direct chord collides too.
    let err = Keymap::from_toml("[bindings]\nnavigate_down = \"up\"\n").unwrap_err();
    assert_eq!(
        err,
        KeymapError::Collision {
            chord: "up".to_string(),
            action: "navigate_down".to_string(),
            displaced: "navigate_up".to_string(),
        }
    );

    // Moving the displaced action in the same file is accepted.
    let ok = Keymap::from_toml(
        "[bindings]\nsplit_vertical = \"prefix+x\"\nclose_pane = \"prefix+f3\"\n",
    )
    .unwrap();
    assert_eq!(ok.lookup_prefix(&ch('x')), Some(Action::SplitVertical));
    assert_eq!(
        ok.lookup_prefix(&key(KeyCode::F(3), KeyModifiers::NONE)),
        Some(Action::ClosePane)
    );
    assert_eq!(ok.lookup_prefix(&ch('v')), None);

    // A chord owned only by a reserved action is free to take.
    let ok = Keymap::from_toml("[bindings]\nzoom = \"prefix+m\"\n").unwrap();
    assert_eq!(ok.lookup_prefix(&ch('m')), Some(Action::Zoom));
    assert_eq!(ok.lookup_prefix(&ch('z')), None);

    // After any accepted merge every active chord dispatches to exactly one action.
    for keymap in [&defaults, &ok] {
        let chords = keymap.active_chords();
        let unique: HashSet<Trigger> = chords.iter().map(|(t, _)| *t).collect();
        assert_eq!(unique.len(), chords.len(), "duplicate active chord");
        assert!(chords.iter().all(|(_, action)| !action.is_reserved()));
    }
}
