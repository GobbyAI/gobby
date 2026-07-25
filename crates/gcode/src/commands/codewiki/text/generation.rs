mod aggregate;
mod one_shot;
mod outcome;
mod routing;
mod tool_loop;

// Preserve every pre-refactor `generation::...` crate path, including symbols
// currently consumed only by tests or as inferred return types.
#[allow(unused_imports)]
pub(crate) use aggregate::{
    AggregateGeneration, LANE_ONE_SHOT, LANE_TOOL_LOOP, generate_aggregate,
};
#[allow(unused_imports)]
pub(crate) use one_shot::{
    ResolvedTextGenerator, generate_with_bounded_retry, maybe_generate, resolve_text_generator,
    resolve_text_verifier,
};
#[allow(unused_imports)]
pub(crate) use outcome::{
    GRAPH_UNAVAILABLE, GenerationContent, GenerationFailureCause, GenerationObservability,
    GenerationOutcome, GenerationStatus, is_ai_generation_failure_code,
};
pub(crate) use routing::direct_route_candidate_error;
#[allow(unused_imports)]
pub(crate) use tool_loop::{
    ResolvedToolLoopGenerator, ToolLoopGenerator, ToolLoopResult, maybe_generate_tool_loop,
    resolve_tool_loop_generator,
};
