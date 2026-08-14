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
        tool_loop_limits: ToolLoopLimits::default(),
        project_id: None,
        grant: None,
    }
}

fn pinned_candidates() -> Vec<FeatureCandidate> {
    vec![FeatureCandidate {
        candidate: "claude/sonnet".to_string(),
        reasoning_effort: Some("xhigh".to_string()),
    }]
}

#[test]
fn pinned_one_shot_rejects_off_route() {
    let error = generate_one_shot_pinned(
        &pinned_context(),
        AiRouting::Off,
        GenerationTier::Aggregate,
        &pinned_candidates(),
        "prompt",
        None,
        None,
    )
    .expect_err("off route rejects pinned generation");

    assert!(matches!(error, AiError::NotConfigured { .. }), "{error}");
    assert!(error.to_string().contains("route is off"), "{error}");
}

#[test]
fn pinned_one_shot_requires_at_least_one_candidate() {
    let error = generate_one_shot_pinned(
        &pinned_context(),
        AiRouting::Daemon,
        GenerationTier::Aggregate,
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
