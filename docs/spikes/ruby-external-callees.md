# Ruby External Callee Provenance Spike

## Decision

Ruby external callee classification must stay conservative. `require`, `require_relative`, constants, mixins, and dynamic dispatch do not provide enough evidence by themselves to emit external call targets.

The safe subset is limited to constant-qualified calls whose root constant is explicitly tied to an external gem or standard-library require, with no local constant collision. Bare calls, receiver calls, mixin-provided methods, and `require_relative` paths remain unresolved.

## Current Parser Surface

`languages.rs` currently captures:

- Symbols: `method`, `singleton_method`, `class`, and `module`.
- Imports: calls to `require`, `require_relative`, and `include`.
- Calls: `(call method: (identifier) @name)`.

That is enough to see that a file has `require "json"` and calls a method name. It is not enough to determine whether `JSON.parse`, `parse`, or `object.parse` belongs to the `json` library.

## Provenance Needed

A safe implementation needs a require-to-constant provenance map. Sources can include:

- Bundler metadata from `Gemfile.lock`.
- Gem specifications and loaded file maps for installed gems.
- A curated standard-library map for stable roots such as `JSON`, `FileUtils`, or `Net::HTTP`.
- Project-local constant indexing from `class` and `module` definitions.

Only classify when all of these are true:

- The call is constant-qualified, for example `JSON.parse`.
- The root constant has explicit external provenance from a gem or standard library.
- No project-local class/module defines the same root constant.
- The call target text is structurally direct, not dynamic send or metaprogrammed dispatch.

## Minimum Query Changes

Ruby call extraction needs to capture constant-qualified receivers, not just method names. The implementation should distinguish:

- `JSON.parse` as a candidate external call with root `JSON`.
- Receiver-dispatched calls like `client.parse` remain unresolved.
- Bare calls such as `parse` are unresolved unless a future static import model proves ownership.
- Dynamic invocations like `send(:parse)` and `public_send(:parse)` are also unresolved.

Imports should separate:

- `require "json"`: possible external provenance after manifest lookup.
- `require_relative "local"`: local provenance only.
- `include Foo`, `extend Foo`, `prepend Foo`: mixin surface, not call ownership.

## Fail-Closed Boundary

Leave these unresolved:

- Bare method calls such as `parse(...)`.
- Receiver calls such as `client.parse(...)`.
- Calls introduced through `include`, `extend`, or `prepend`.
- Calls through `send`, `public_send`, `method`, `const_get`, or generated methods.
- Constants defined in the project, even when a gem with the same root exists.
- `require_relative` and relative `load` targets.
- Any require string that is computed rather than a literal.

## Implementation Sketch

1. Build a project-local constant index from Ruby symbol captures.
2. Parse literal `require` targets and resolve them through `Gemfile.lock`, gemspec metadata, and a curated stdlib map.
3. Add query support for constant-qualified calls.
4. Add an external root binding only when the required file maps to one external root constant and no local constant shadows it.
5. Reuse the same fail-closed local-symbol collision checks used by Python, JavaScript, TypeScript, and the newer language producers.

_Last verified: 2026-05-24_
