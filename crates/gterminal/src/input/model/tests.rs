use super::*;

#[test]
fn native_source_exposes_typed_identity_without_changing_semantics() {
    let record = WindowsKeyRecord {
        key_down: true,
        repeat_count: 1,
        virtual_key_code: 27,
        virtual_scan_code: 1,
        unicode: 27,
        control_key_state: 0,
    };
    let key = TerminalKey::new(KeyCode::Esc, KeyModifiers::empty()).with_windows_record(record);
    let enhanced = TerminalKey::new(KeyCode::Esc, KeyModifiers::empty()).with_windows_record(
        WindowsKeyRecord {
            control_key_state: 0x0100,
            ..record
        },
    );

    assert!(matches!(key.identity(), KeyIdentity::Physical(_)));
    assert_ne!(key.identity(), enhanced.identity());
    assert_eq!(key.code, KeyCode::Esc);
}

#[test]
fn semantic_source_uses_semantic_identity() {
    let key = TerminalKey::new(KeyCode::Char('x'), KeyModifiers::CONTROL);

    assert_eq!(key.identity(), KeyIdentity::Semantic(KeyCode::Char('x')));
    assert!(!key.has_physical_identity());
    assert_eq!(key.windows_record(), None);
}

#[test]
fn native_source_remains_immutable_when_canonical_phase_changes() {
    let key = TerminalKey::new(KeyCode::Esc, KeyModifiers::empty())
        .with_windows_record(WindowsKeyRecord {
            key_down: true,
            repeat_count: 1,
            virtual_key_code: 27,
            virtual_scan_code: 1,
            unicode: 27,
            control_key_state: 0,
        })
        .with_kind(crossterm::event::KeyEventKind::Release);

    assert_eq!(key.kind, crossterm::event::KeyEventKind::Release);
    assert_eq!(
        key.windows_record().map(|record| record.key_down),
        Some(true)
    );
    assert_eq!(key.repeat_count, 1);
}

#[test]
fn release_clears_generated_text_and_grouped_repeat_count() {
    let release = TerminalKey::new(KeyCode::Char('a'), KeyModifiers::empty())
        .with_generated_text(Some("a".to_owned()))
        .with_repeat_count(4)
        .with_kind(crossterm::event::KeyEventKind::Release);
    let regrouped_release = release
        .clone()
        .with_repeat_count(4)
        .with_generated_text(Some("ignored".to_owned()));

    assert_eq!(release.generated_text, None);
    assert_eq!(release.repeat_count, 1);
    assert_eq!(regrouped_release.generated_text, None);
    assert_eq!(regrouped_release.repeat_count, 1);
}

#[test]
fn non_ascii_uppercase_with_shift_is_committed_text() {
    let key = TerminalKey::new(KeyCode::Char('É'), KeyModifiers::SHIFT).with_text_commit();

    assert_eq!(key.generated_text.as_deref(), Some("É"));
}

#[test]
fn protocol_from_zero_flags_is_legacy() {
    assert_eq!(
        KeyboardProtocol::from_kitty_flags(0),
        KeyboardProtocol::Legacy
    );
}

#[test]
fn protocol_from_nonzero_flags_is_kitty() {
    assert_eq!(
        KeyboardProtocol::from_kitty_flags(7),
        KeyboardProtocol::Kitty { flags: 7 }
    );
}

#[cfg(not(windows))]
#[test]
fn keyboard_enhancement_flags_stay_ime_compatible() {
    let flags = ime_compatible_keyboard_enhancement_flags();

    assert!(flags.contains(KeyboardEnhancementFlags::DISAMBIGUATE_ESCAPE_CODES));
    assert!(flags.contains(KeyboardEnhancementFlags::REPORT_EVENT_TYPES));
    assert!(flags.contains(KeyboardEnhancementFlags::REPORT_ALTERNATE_KEYS));
    assert!(!flags.contains(KeyboardEnhancementFlags::REPORT_ALL_KEYS_AS_ESCAPE_CODES));
}

#[test]
fn modify_other_keys_mode_is_enabled_for_tmux() {
    assert_eq!(
        host_modify_other_keys_mode_for_env(true, Some("WezTerm"), true, true),
        Some(ModifyOtherKeysMode::Mode2)
    );
}

#[test]
fn modify_other_keys_mode_is_enabled_for_wezterm_hosts() {
    assert_eq!(
        host_modify_other_keys_mode_for_env(false, Some("WezTerm"), false, false),
        Some(ModifyOtherKeysMode::Mode1)
    );
    assert_eq!(
        host_modify_other_keys_mode_for_env(false, None, true, false),
        Some(ModifyOtherKeysMode::Mode1)
    );
}

#[test]
fn modify_other_keys_mode_is_enabled_for_alacritty_hosts() {
    assert_eq!(
        host_modify_other_keys_mode_for_env(false, None, false, true),
        Some(ModifyOtherKeysMode::Mode1)
    );
}

#[test]
fn modify_other_keys_mode_is_not_enabled_for_unknown_hosts() {
    assert_eq!(
        host_modify_other_keys_mode_for_env(false, Some("ghostty"), false, false),
        None
    );
    assert_eq!(
        host_modify_other_keys_mode_for_env(false, None, false, false),
        None
    );
}
