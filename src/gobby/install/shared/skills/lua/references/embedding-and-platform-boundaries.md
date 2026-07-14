# Embedding And Platform Boundaries

## C API Stack Discipline

- Record the expected stack shape at each native entry point: arguments at
  entry, temporary values, and exact result count at return.
- Validate with `lua_type`, `lua_is*`, or `luaL_check*` according to whether bad
  input is recoverable or a caller contract error.
- Use `lua_checkstack` when pushing a data-dependent number of values. Convert a
  relative index with `lua_absindex` before pushes when the original value must
  remain addressable.
- Restore or deliberately transfer stack ownership on every branch. In host
  tests, compare `lua_gettop` before and after calls that promise balance.
- Keep a Lua reference alive while retaining pointers derived from Lua values;
  follow the API's lifetime rules for strings and userdata.

## States Modules And Userdata

- Create states and allocators through the host's established wrapper. If the
  host uses `lua_newstate`, preserve its allocation accounting and failure rules.
- Register native modules with the target runtime's auxiliary-library pattern,
  expose a narrow module table, and return the documented number of results from
  `luaopen_<module>`.
- Store host-owned objects in full userdata when Lua must hold their lifetime.
  Check the metatable/type before casting and make close/finalize idempotent.
- Use light userdata only for non-owning pointer identity with a lifetime
  guaranteed elsewhere.
- Keep registry keys collision-safe and unreference them when ownership ends.

## Protected Execution And Capabilities

- Load and run chunks through protected host boundaries. Distinguish load errors
  from execution errors and consume the correct stack values for each status.
- Open only required libraries for restricted scripts. `luaL_openlibs` installs
  the complete standard set; selective registration gives the host a smaller
  capability surface.
- Treat `io`, `os`, `package`, `debug`, dynamic native modules, and host callbacks
  as capabilities. Grant them from an explicit trust policy.
- Enforce CPU, memory, recursion, output, and wall-time limits outside script
  cooperation. Keep the host process boundary appropriate to the threat model.

## Runtime-Specific Hosts

- LuaJIT, OpenResty, Luau, Redis, Nginx, game engines, and plugin hosts provide
  distinct APIs, lifecycle hooks, yield rules, and allowed globals. Read their
  local docs and tests before editing portable code.
- Keep LuaJIT FFI declarations synchronized with the actual ABI and target
  architecture. Treat FFI access as native-code trust.
- Avoid importing Neovim, LÖVE, or another framework's conventions into a generic
  Lua module unless that framework is the declared host.
