# Select providers and models

Load when choosing execution settings or diagnosing model/provider rejection.
Inspect the installed definition and spawning session first. Resolve provider
from an explicit spawn argument, then the definition, then the spawning session;
absence of a concrete supported provider is an error. When the spawn `model`
argument is supplied, `provider` must also be supplied explicitly — definition
and session defaults cannot fill it in. Do not infer provider from a model name.
Inspect returned effective settings rather than assuming inheritance.

Fetch the launch schema for model, reasoning effort, and reasoning-required
options. Explicit provider selection affects fallback eligibility. Definition
fallback chains are bounded and inspect prior task/provider failures; do not
assume every failure automatically rotates providers. `spawn_agent` and
`evaluate_spawn` validate the provider/model pair against the provider-scoped
capability matrix (`CapabilityResolver.find_model` on collector snapshots)
before allocating a terminal or worktree; `evaluate_spawn` accepts `model` so
the same check is a dry run. Include known, unknown, and unsupported reasoning
states. Strict reasoning requests can fail; inspect the reported effective
effort and warnings.

Operator/client discovery uses `GET /api/providers` for readiness and
`GET /api/providers/models` for capabilities and refresh provenance. Last-good
model rows can remain after a collector failure; availability and freshness are
separate. `gobby status` diagnoses local installation. Feature candidates use
explicit provider/model routing. Endpoint groups follow their own protocol and
health gates; never pass an unresolved `auto` model sentinel to a provider.

If readiness, auth, or endpoint activation fails, use the provider guide's
operator diagnostics and repair the configured dependency. Do not expose tokens,
guess model capabilities, or silently replace the requested provider. Persona
switching cannot change these execution settings. Speed is not a spawn parameter.

Guide: [Providers and models](../../../../../../../../docs/guides/providers-and-models.md).

_Last verified: 2026-09-12_
