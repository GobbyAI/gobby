# Code index

Load when locating implementation, retrieving source, assessing change impact,
or diagnosing index freshness. Use the native `gcode` CLI through the shell.
Ordinary index navigation uses native commands. The `gobby-ask` MCP service
handles direct interactive evidence, durable source-bound questions, and assigned worker evidence;
the daemon's HTTP routes also serve UI and integration clients.

Start with the query shape, then retrieve the smallest useful source body.
Direct `gcode` navigation does not require loading this reference. Reference
loads still use the normal skill-file schema gate and complete pagination.
Loading an overview or menu never loads another topic.

The first raw repository search, navigation, or source read requests this reference.
After loading, reconsider the blocked command using the guidance. Use `gcode` for
supported search, navigation, and source retrieval. Use raw tools when `gcode`
cannot adequately serve the operation, and state the reason before falling back.
Ordinary turns do not repeat the warning; clear or compact resets the teaching gate.

| Topic | Load when |
| --- | --- |
| [search](search.md) | Choosing search lanes or interpreting ranked matches |
| [retrieval](retrieval.md) | Reading source by file location or stored symbol ID |
| [ask](ask.md) | Running durable questions or handling immutable evidence |
| [navigation](navigation.md) | Exploring structure, paths, identities, or pages |
| [impact](impact.md) | Finding callers, dependencies, and change impact |
| [graphs](graphs.md) | Inspecting graph views, reports, or projections |
| [recovery](recovery.md) | Indexing, repairing, or diagnosing unavailable services |

Discover syntax with `gcode --help` and the relevant subcommand's `--help`.
The current checkout's CLI and tests own supported commands; an installed binary
from another branch can expose a different surface. Compare `gcode contract`
and `gcode schema-identity --json` when diagnosing that mismatch.

Navigation defaults to compact text. Run printed continuation commands exactly
when more relevant results remain. Exit 0 includes an empty result; do not
repeat a successful query merely to confirm emptiness. Switch search lanes
when results are irrelevant. On `payload_skew` or `api_contract_mismatch`, stop
retrying, report the recovery directive, and use fallback tools.

Mutating index, projection, and cleanup commands need task-relevant authority;
this reference grants none. Global pruning and exact content retirement are
operator maintenance. Test mutations against isolated fixtures.

Guide: [Code index](../../../../../../../../docs/guides/code-index.md).

_Last verified: 2026-09-12_
