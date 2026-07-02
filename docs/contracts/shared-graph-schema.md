# Shared Code Graph Schema

This contract defines the live FalkorDB code graph produced by `gcode` and consumed by `gwiki`.

## Graph

The shared code graph is the FalkorDB graph named `gobby_code`. Graph name
selection remains consumer-owned; `gcore` only carries FalkorDB connection
configuration.

Every node that belongs to a Gobby project carries a `project` property. Consumers
must include `project` in reads, and producers must include `project` in node
identity writes.

## Labels

`CodeFile`
: Source file indexed by gcode. Identity is `(project, path)`.

`CodeModule`
: Imported module or package. Identity is `(project, name)`.

`CodeSymbol`
: Definition or resolvable call target. Identity is `(project, id)`.

`ExternalSymbol`
: Resolved external call target outside the indexed project. Identity is
`(project, id)`.

`UnresolvedCallee`
: Call target that gcode could not resolve to an indexed or external symbol.
Identity is `(project, id)`.

## Relationships

`IMPORTS`
: `CodeFile -> CodeModule`. The relationship stores source import text, source
file path, optional line, provenance, source system, and sync token metadata.

`DEFINES`
: `CodeFile -> CodeSymbol`. The relationship ties each indexed definition to its
containing file and stores provenance, source system, source symbol id, and sync
token metadata.

`CALLS`
: `CodeSymbol -> CodeSymbol`, `CodeSymbol -> ExternalSymbol`, or
`CodeSymbol -> UnresolvedCallee`. The relationship stores the call file, line,
callee name, source file path, source symbol id, provenance, source system, and
sync token metadata.

## Consumer Contract

`gwiki` reads this graph live from shared FalkorDB. When FalkorDB is missing or
unreachable, code graph reads return empty code edges and attach a degradation
note for `gcode_code_graph`.
