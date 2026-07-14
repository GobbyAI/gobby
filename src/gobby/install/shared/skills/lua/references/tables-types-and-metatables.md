# Tables Types And Metatables

## Boundary Tables

- Validate `type(value)` and required keys before reading nested fields. Report
  the failing path and expected shape at API boundaries.
- Distinguish missing (`nil`) from present `false`. Assigning `nil` removes a
  table key.
- Decide whether a function borrows, mutates, shallow-copies, or deep-copies a
  caller table. Document identity and aliasing where they matter.
- Reject unexpected keys when they would hide misspellings or cross a security
  boundary. Preserve extension fields when the contract explicitly permits them.
- Decide whether metatables are valid input at each trust boundary. For raw-data
  validation, traverse with `next` and read with `rawget` so caller-controlled metamethods
  cannot execute during validation or copying.
- Detect cycles and preserve or reject aliasing explicitly when copying nested
  tables. Bound depth and element counts before recursive traversal.
- Avoid `#table` as a general item count. It follows sequence-border rules and
  does not define the size of a sparse or mixed-key table.

## Iteration And Multiple Values

- Use `ipairs` only for a contiguous 1-based sequence; it stops at the first
  missing index. Use `pairs` for mappings and never depend on its traversal order.
- Preserve multiple-return semantics deliberately. Parentheses, assignment
  position, table constructors, and calls can adjust a result list to one value.
- Keep array and mapping roles separate when serialization or host conversion
  needs an unambiguous shape.

## Metatables

- Build a complete metatable before attaching it, then attach it immediately at
  construction. This is required for reliable finalizer marking and avoids
  partially configured objects.
- Use `__index` for a deliberate lookup protocol. Avoid long or cyclic fallback
  chains and recursive access through the same metamethod.
- Use `__newindex` to enforce a real write contract. Call `rawset` only when the
  implementation intentionally bypasses that contract.
- Set `__metatable` when public callers must not inspect or replace the real
  metatable. This protects the protocol from ordinary `getmetatable` and
  `setmetatable`; the debug library remains a privileged capability.
- Keep arithmetic, comparison, length, call, and string metamethods predictable.
  Avoid I/O, hidden mutation, or yields in operations callers expect to be local.

## Methods Weak Tables And Identity

- Define `function obj:method(x)` when callers use `obj:method(x)`; both add the
  receiver. Use dot syntax when there is no receiver.
- Choose identity or value equality explicitly. `rawequal` bypasses `__eq`.
- Use weak tables only after choosing weak keys, values, or both from the cache's
  ownership model. Test collection-sensitive behavior without assuming an exact
  collection instant.
