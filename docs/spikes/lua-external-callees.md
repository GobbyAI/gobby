# Lua External Callee Provenance Spike

## Decision

Lua external callee classification should remain unresolved until `require` provenance and local table/module ownership are modeled.

Lua's module system is dynamic: `require("json")`, table aliases, metatables, `_G`, and monkey patching can all change call ownership. Receiver calls such as `client:get()` also need type or table provenance.

## Provenance Needed

- Literal `require` extraction.
- Local module roots from files returned by `return { ... }` and common module assignment patterns.
- Package manifests where available, such as LuaRocks rockspecs.
- Curated require-to-root bindings for stable libraries.
- Local table/global collision checks.

## Safe Future Boundary

Classify only direct module-qualified calls where a literal `require` is bound to a local name, the required module has external provenance, and no local module or table root shadows that binding.

## Fail-Closed Cases

- Bare global calls.
- Receiver calls using `:` syntax.
- Dynamic `require`, `load`, `loadfile`, or `_G` lookups.
- Metatable-provided methods.
- Monkey-patched module tables.

_Last verified: 2026-05-24_
