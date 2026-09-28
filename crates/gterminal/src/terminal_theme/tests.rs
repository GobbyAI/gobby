use super::*;

#[test]
fn parses_st_terminated_rgb_response() {
    let parsed = parse_default_color_response("\x1b]10;rgb:cccc/dddd/eeee\x1b\\");
    assert_eq!(
        parsed,
        Some((
            DefaultColorKind::Foreground,
            RgbColor {
                r: 0xcc,
                g: 0xdd,
                b: 0xee,
            },
        ))
    );
}

#[test]
fn parses_bel_terminated_hash_response() {
    let parsed = parse_default_color_response("\x1b]11;#123456\u{7}");
    assert_eq!(
        parsed,
        Some((
            DefaultColorKind::Background,
            RgbColor {
                r: 0x12,
                g: 0x34,
                b: 0x56,
            },
        ))
    );
}

#[test]
fn parses_palette_responses_and_builds_full_query() {
    assert_eq!(
        parse_palette_color_response("\x1b]4;255;rgb:1111/2222/3333\x1b\\"),
        Some((
            255,
            RgbColor {
                r: 0x11,
                g: 0x22,
                b: 0x33,
            }
        ))
    );

    let query = host_terminal_theme_query_sequence();
    assert!(query.starts_with(HOST_COLOR_QUERY_SEQUENCE));
    assert!(query.contains("\x1b]4;0;?\x1b\\"));
    assert!(query.ends_with("\x1b]4;255;?\x1b\\"));
    assert_eq!(query.matches("\x1b]4;").count(), 256);
}

#[test]
fn default_color_reset_sequences_use_xterm_osc_numbers() {
    assert_eq!(
        osc_reset_default_color_sequence(DefaultColorKind::Foreground),
        "\x1b]110\x1b\\"
    );
    assert_eq!(
        osc_reset_default_color_sequence(DefaultColorKind::Background),
        "\x1b]111\x1b\\"
    );
}

#[test]
fn scales_short_hex_components() {
    assert_eq!(parse_hex_component("f"), Some(255));
    assert_eq!(parse_hex_component("80"), Some(128));
    assert_eq!(parse_hex_component("800"), Some(128));
    assert_eq!(parse_hex_component("8000"), Some(128));
}

#[test]
fn theme_declaration_round_trips_a_sparse_terminal_theme() {
    let rgb = |r, g, b| RgbColor { r, g, b };
    let mut theme = TerminalTheme {
        foreground: Some(rgb(0x20, 0x21, 0x22)),
        background: Some(rgb(0xfa, 0xfb, 0xfc)),
        ..TerminalTheme::default()
    };
    theme.palette[1] = Some(rgb(0xc0, 0x10, 0x20));
    theme.palette[255] = Some(rgb(1, 2, 3));

    let declared = ThemeDeclaration::from(&theme);
    assert_eq!(
        declared.palette,
        vec![(1, rgb(0xc0, 0x10, 0x20)), (255, rgb(1, 2, 3))]
    );
    assert_eq!(declared.terminal_theme(), theme);
}

#[test]
fn theme_declaration_appearance_follows_the_background() {
    let with_background = |background| ThemeDeclaration {
        background,
        ..ThemeDeclaration::default()
    };
    assert_eq!(
        with_background(Some(RgbColor {
            r: 0xfa,
            g: 0xfb,
            b: 0xfc
        }))
        .appearance(),
        Some(HostAppearance::Light)
    );
    assert_eq!(
        with_background(Some(RgbColor {
            r: 0x10,
            g: 0x11,
            b: 0x12
        }))
        .appearance(),
        Some(HostAppearance::Dark)
    );
    assert_eq!(with_background(None).appearance(), None);
}
