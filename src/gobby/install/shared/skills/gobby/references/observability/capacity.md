# Provider capacity

Load when deciding whether a provider has usable quota or diagnosing stale
capacity. Fetch `gobby-metrics:get_provider_capacity` and call it with the
provider name. It does not start an agent turn, but a stale or missing snapshot
can trigger a provider usage refresh and persist the resulting observation.

1. Read `state`, `supported`, `observed_at`, `windows`, and `reason` together.
2. `available` means the returned windows were not exhausted at observation.
   It does not reserve quota or guarantee the next request will succeed.
3. `exhausted` means at least one reported window is exhausted. Inspect that
   window and its reset information before choosing another provider or waiting.
4. `stale` preserves a previous observation after a transient refresh failure.
   Its windows are historical evidence, not a fresh availability decision.
5. `unknown` means usable capacity evidence is absent, including an unavailable
   service, missing reporter, or unsupported provider version. It does not mean
   unlimited capacity or exhaustion.

The default service registers AGY. A provider can be supported for agent work
without having a capacity reporter. Snapshots are scoped by machine/provider;
the service normally reuses observations for 60 seconds and joins concurrent
refreshes. Repeated calls inside that interval need not contact the provider.

For failures, inspect the returned reason and provider/version support first.
Use provider setup and authentication guidance for the underlying repair; do
not launch a billable agent turn merely to test the quota report. After repair,
allow the normal refresh path to produce a new observation. Configuration or
provider switching still requires the authority of the user's task.

See the [observability guide](../../../../../../../../docs/guides/observability.md).
