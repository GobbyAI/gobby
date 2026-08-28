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
