---
name: lua
description: "Enforces default Lua coding standards for agents writing or refactoring Lua: runtime and module configuration, table and metatable contracts, errors and resources, coroutines, host embedding, focused testing, performance, and security. Use before editing Lua unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: lua, luac, luarocks, rockspec, busted, stylua, luacheck, lua-language-server, tables, metatables, metamethods, coroutines, userdata, lua-c-api, luajit, openresty
sources:
  - "Primary: current official Lua 5.5 Reference Manual, independently summarized for Gobby: https://www.lua.org/manual/5.5/"
  - "Primary: official Lua 5.4 Reference Manual for supported deployed runtimes: https://www.lua.org/manual/5.4/"
  - "Primary: official Lua 5.5 distribution readme for build and embedding layout: https://www.lua.org/manual/5.5/readme.html"
  - "Primary tooling references: LuaRocks, busted, StyLua, and Luacheck project documentation: https://luarocks.org/ https://lunarmodules.github.io/busted/ https://github.com/JohnnyMorganz/StyLua https://github.com/lunarmodules/luacheck"
  - "Seed provenance only: SkillsMP lua-guide was attributed to ar4mirez/samuel-claude-skills; the repository was unavailable at authoring time and no license could be verified, so no text or code copied: https://github.com/ar4mirez/samuel-claude-skills"
---

# Lua

Default coding standards for Lua. Repository conventions and configured tooling
take precedence. First identify the exact runtime, language version, host,
module loader, package manager, formatter syntax mode, and test framework.

## Tooling

Run the repository's configured formatter, linter, syntax check, and focused
tests before finishing. Use existing wrappers and CI tasks when available:

- Format: StyLua, lua-format, or repository tooling
- Static checks: Luacheck, Selene, LuaLS diagnostics, or host-specific checks
- Syntax: the declared `luac` or interpreter, never an arbitrary system Lua
- Tests: focused busted, luaunit, host integration, or repository targets
- Packages: preserve rockspecs, `luarocks.lock`, generated bindings, and vendored
  modules

Keep runtime compatibility, configured globals, warnings, and analysis intact.
Fix the underlying violation.

## Configuration And Modules

- Match Lua 5.1, 5.2, 5.3, 5.4, 5.5, LuaJIT, OpenResty, Luau, or a patched host
  exactly. Their syntax, standard libraries, C APIs, and numeric models differ.
- Keep module loading explicit: use locals, return a deliberate module value,
  and preserve the repository's `package.path`, `package.cpath`, and rockspec
  layout.
- Align formatter and language-server syntax modes with the runtime.

For runtime selection, version differences, LuaRocks, modules, paths, globals,
StyLua, Luacheck, and LuaLS:
`get_skill_file(name="lua", path="references/configuration-and-modules.md")`

## Tables Types And Metatables

- Treat external tables as unvalidated mutable references. Check required keys,
  value types, nil/false distinctions, sequence shape, and ownership at entry.
- Use metatables to express a small protocol. Keep metamethod behavior
  unsurprising, install complete metatables at construction, and protect public
  metatables when callers must not mutate them.
- Use `:` only when the function contract includes `self`; keep `.` calls and
  definitions consistent.

For table shapes, copying, sequences, iteration, metatables, metamethods, weak
tables, and colon/dot calls:
`get_skill_file(name="lua", path="references/tables-types-and-metatables.md")`

## Errors And Resources

- Follow the repository's error convention: raised errors, `nil, err`, status
  tuples, or a host-specific result type. Preserve original error objects and
  tracebacks at boundaries.
- Release files, sockets, userdata, registry references, and host handles through
  explicit or lexical lifetime management. Treat finalizers as a backstop.
- Keep cleanup correct across normal return, error, coroutine abandonment, and
  host shutdown.

For `error`, `pcall`, `xpcall`, protected host calls, to-be-closed variables,
finalizers, and cleanup:
`get_skill_file(name="lua", path="references/errors-and-resources.md")`

## Coroutines And Concurrency

- Treat coroutines as cooperative execution. They do not provide OS-thread
  parallelism or automatic scheduling.
- Check the leading status from `coroutine.resume` before consuming yielded or
  returned values. Define ownership, cancellation, close, and error propagation.
- Preserve the host framework's yieldability and continuation rules.

For lifecycle states, resume/yield values, `coroutine.close`, schedulers, C
continuations, cancellation, and shared state:
`get_skill_file(name="lua", path="references/coroutines-and-concurrency.md")`

## Embedding And Platform Boundaries

- Treat every Lua/C stack exchange as a typed boundary. Check argument types,
  stack capacity, result counts, ownership, and error status.
- Expose the smallest host API and standard-library surface required by the
  script. Keep untrusted code behind host-enforced resource and capability
  limits.
- Preserve host-specific APIs and dialects without leaking them into portable
  modules.

For the C API stack, userdata, module entry points, allocators, protected calls,
selective libraries, LuaJIT, OpenResty, and custom hosts:
`get_skill_file(name="lua", path="references/embedding-and-platform-boundaries.md")`

## Testing And Tooling

- Add focused tests for valid and invalid table shapes, nil/false cases,
  metamethod behavior, multiple returns, coroutine errors, cleanup, and host
  conversions.
- Test with the same interpreter and syntax mode used in production.
- Use host-level integration tests for C stack balance, userdata lifetimes,
  callbacks, quotas, and shutdown.

For focused commands, busted, luaunit, syntax checks, formatting, linting,
version matrices, and embedded tests:
`get_skill_file(name="lua", path="references/testing-and-tooling.md")`

## Performance And Security

- Measure allocation, table growth, string construction, GC, coroutine
  scheduling, and host crossings before optimizing.
- Keep loaded code, library access, search paths, bytecode, debug facilities,
  and FFI within the repository's trust model.
- Confirm performance changes with representative profiles or benchmarks and
  regression tests.

For hot paths, GC modes, LuaJIT, host crossings, code loading, capabilities,
resource limits, and untrusted scripts:
`get_skill_file(name="lua", path="references/performance-and-security.md")`

## Before You Finish

If you touched Lua: verify formatter/static checks, syntax with the declared
runtime, focused tests, and any relevant host or version matrix target.
