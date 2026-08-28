//! 3.3.9 Gobby token map survives monochrome.

use gobby_client::theme::{Theme, ThemeKind};

#[test]
fn tokens_match_design_contract_and_survive_monochrome() {
    for kind in [ThemeKind::Dark, ThemeKind::Light] {
        let theme = Theme::new(kind);
        assert_eq!(theme.accent.hue, 125);
        assert_eq!(theme.info.hue, 250);
        assert_eq!(theme.warning.hue, 75);
        assert_eq!(theme.destructive.hue, 350);
        assert_eq!(theme.success.hue, 125);
        assert!(
            theme.success.lightness != theme.accent.lightness
                || theme.success.chroma < theme.accent.chroma,
            "success is lightness/chroma differentiated brand hue"
        );
        for state in theme.states() {
            assert!(
                !state.icon.is_empty() || state.position_cue.is_some(),
                "{} needs icon or position cue",
                state.name
            );
        }
        let mono = theme.monochrome_ranks();
        assert!(
            mono.windows(2).all(|w| w[0] != w[1]),
            "state lightness ranks must be distinct in {kind:?}: {mono:?}"
        );
        let ansi = theme.ansi256_ranks();
        assert_eq!(ansi, {
            let mut sorted = mono.clone();
            sorted.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());
            sorted
        });
        let rgb = theme.accent.rgb();
        assert!(rgb.0 > 0 || rgb.1 > 0 || rgb.2 > 0);
        assert_ne!(theme.info.rgb(), theme.warning.rgb());
        assert_ne!(theme.warning.rgb(), theme.destructive.rgb());
    }
}
