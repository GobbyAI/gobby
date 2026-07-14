# Configuration And Modules

## Select The Runtime

- Read the build, container, shebang, rockspec, CI matrix, host documentation,
  `.luarc.json`, formatter config, and native headers before choosing syntax.
- Distinguish PUC Lua 5.1/5.2/5.3/5.4/5.5, LuaJIT, OpenResty LuaJIT, Luau, and
  patched embedded runtimes. Treat compatibility flags as evidence to verify.
- Check numeric representation, integer availability, bit operations, UTF-8
  library, `_ENV`, to-be-closed variables, coroutine close behavior, and C API
  version before using them.
- Preserve the host's `LUA_PATH`, `LUA_CPATH`, compile-time `luaconf.h`, and
  application-defined globals.

LuaJIT commonly presents a Lua 5.1 base plus extensions. Luau and host dialects
have separate syntax and libraries. Run their own tools.

## Modules And Namespaces

- Prefer local bindings. An undeclared name writes through the active
  environment and can silently create shared global state.
- Return an explicit module table or documented callable/value from the chunk.
  Match existing conventions when a host registers modules directly.
- Remember that `require` caches the loader result in `package.loaded`. Tests
  that clear or replace the cache must restore it and avoid order dependence.
- Keep module names aligned with file layout and configured `package.path` and
  `package.cpath`. Avoid modifying global search paths inside reusable modules.
- Make optional dependencies explicit at the boundary. Do not hide a missing
  dependency behind an unrelated fallback.

## Packages And Generated Files

- Preserve rockspec package name/version, supported Lua constraints,
  dependencies, source hashes, platform overrides, and module mappings.
- Update `luarocks.lock` and vendored or generated files only through the
  repository's established workflow.
- Keep C module ABI, shared-library naming, and `luaopen_<module>` entry points
  aligned with the loader and target runtime.

## Tool Configuration

- Set StyLua's syntax mode to the actual dialect; its union mode can accept
  constructs that collide across Lua versions and Luau.
- Configure LuaLS runtime version, workspace libraries, globals, and third-party
  annotations from repository facts.
- Keep Luacheck/Selene allowed globals narrow. A new global should reflect a
  real host contract, not silence a typo.
