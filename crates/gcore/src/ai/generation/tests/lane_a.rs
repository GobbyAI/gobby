use super::common::*;

fn pinned_context() -> AiContext {
    AiContext {
        bindings: AiBindings {
            embed: blank_binding(),
            audio_transcribe: blank_binding(),
            audio_translate: blank_binding(),
            vision_extract: blank_binding(),
            text_generate: blank_binding(),
        },
        tuning: AiTuning {
            max_concurrency: 1,
            keep_alive: None,
        },
        limiter: AiLimiter::new(1),
        project_id: None,
    }
}

fn pinned_candidates() -> Vec<FeatureCandidate> {
    vec![FeatureCandidate {
        candidate: "claude/sonnet".to_string(),
        reasoning_effort: Some("xhigh".to_string()),
    }]
}

#[test]
fn pinned_one_shot_rejects_direct_route_with_clear_error() {
    // The Direct route resolves a single profile target and cannot honor an
    // explicit candidate chain; it must fail loudly, never generate unpinned.
    let error = generate_one_shot_pinned(
        &pinned_context(),
        AiRouting::Direct,
        &pinned_candidates(),
        "prompt",
        None,
        None,
    )
    .expect_err("direct route rejects explicit candidates");

    assert!(matches!(error, AiError::NotConfigured { .. }), "{error}");
    assert!(
        error
            .to_string()
            .contains("unsupported on the Direct route"),
        "{error}"
    );
}

#[test]
fn pinned_one_shot_requires_at_least_one_candidate() {
    let error = generate_one_shot_pinned(
        &pinned_context(),
        AiRouting::Daemon,
        &[],
        "prompt",
        None,
        None,
    )
    .expect_err("empty candidate chain rejected");

    assert!(matches!(error, AiError::NotConfigured { .. }), "{error}");
    assert!(
        error.to_string().contains("at least one candidate"),
        "{error}"
    );
}

#[test]
fn pinned_one_shot_rejects_off_and_auto_routes() {
    for route in [AiRouting::Off, AiRouting::Auto] {
        let error = generate_one_shot_pinned(
            &pinned_context(),
            route,
            &pinned_candidates(),
            "prompt",
            None,
            None,
        )
        .expect_err("unresolved route rejected");
        assert!(matches!(error, AiError::NotConfigured { .. }), "{error}");
    }
}
