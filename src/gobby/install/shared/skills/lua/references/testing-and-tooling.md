# Testing And Tooling

## Focused Commands

Use repository wrappers first. Typical narrow checks are:

```bash
luac -p src/policy.lua
stylua --check src/policy.lua spec/policy_spec.lua
luacheck src/policy.lua spec/policy_spec.lua
busted spec/policy_spec.lua
busted spec/policy_spec.lua --filter="rejects invalid request"
```

Point every command at the production interpreter or configured dialect. A
system `luac` from another version can reject valid syntax or accept an
incompatible feature.

## Behavioral Coverage

- Test required, optional, extra, false, nil, sparse, cyclic, aliased, and
  oversized table inputs as the contract requires.
- Test dot/colon calls, metamethod lookup/write behavior, protected metatables,
  equality, length, and multiple-return adjustment where exposed.
- Test coroutine creation, each yield, terminal return, resume failure, close,
  cancellation, and cleanup after partial progress.
- Test errors as values and strings, traceback enrichment, resource release, and
  repeat close/shutdown.
- Restore `package.loaded`, globals, environment tables, search paths, and hooks
  modified by a test. Keep tests order-independent.

## Embedded Coverage

- Exercise native entry points from the real host harness. Assert argument
  validation, exact result counts, stack balance, userdata type checks, registry
  references, callback failures, and state shutdown.
- Run sanitizers and leak detection on changed C/C++ bindings when the project
  supports them.
- Test allocator/quota failures and instruction/time cancellation without
  connecting to the user's running application or daemon.

## Version And Tool Matrices

- Run the smallest supported-runtime matrix that covers changed syntax or
  semantics. Include LuaJIT or a host runtime only when the project supports it.
- Keep StyLua syntax, LuaLS runtime, Luacheck/Selene standards, test interpreter,
  and production runtime aligned.
- Validate changed rockspec dependencies and module mappings with the repository's
  LuaRocks workflow.

## Test Doubles

- Prefer small table/function fakes at host boundaries. Preserve multiple return
  values and error conventions exactly.
- Use spies or mocks for interaction contracts, then retain integration coverage
  for stack, userdata, scheduler, and host lifecycle behavior.
