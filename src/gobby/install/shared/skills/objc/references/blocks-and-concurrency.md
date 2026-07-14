# Blocks And Concurrency

## Block Storage And Capture

- A copied block retains captured Objective-C objects. Treat every escaping block
  as an ownership edge in the object graph.
- Declare stored block properties with `copy`. Under MRC, balance copied blocks
  according to the repository's ownership convention.
- Audit direct `self`, ivar, collection, operation, timer, observer, and nested
  block captures. An owner that stores a block which retains the owner creates a
  strong reference cycle.
- Under ARC, use a weak owner capture only when early owner deallocation is valid;
  promote it strongly inside the block for the duration of one operation when
  needed.
- Do not assume `__block` weakens an object capture under ARC. MRC block-capture
  behavior differs, so follow the file's actual mode and deployment runtime.

## Callback Contracts

- State whether a completion is required, optional, single-shot, repeatable, or
  cancellable. Invoke it on every documented success and recoverable failure path.
- State the callback queue. Dispatch to the main queue for UI work and preserve
  an existing caller-supplied queue contract.
- Avoid holding locks while invoking external blocks. A callback can re-enter,
  block, mutate shared state, or release its owner.
- Copy caller-provided blocks before asynchronous storage. Clear one-shot stored
  blocks after use when the ownership contract permits it.

## Concurrency

- Use the repository's queue confinement, lock, atomic, operation, or dispatch
  source model. Keep reads and writes under the same synchronization contract.
- Protect check-then-act sequences as one operation. Property atomicity alone does
  not make a multi-step invariant safe.
- Keep cancellation and teardown idempotent. Coordinate callbacks racing with
  `dealloc`, invalidation, queue suspension, or task cancellation.
- Scope autorelease pools in long-running background loops when the host does not
  provide a suitable pool boundary.

## Tests

- Test synchronous and asynchronous completion, success, error, cancellation,
  repeated delivery, and exactly-once behavior.
- Test the documented queue and reentrancy behavior.
- Release the owner and verify whether work cancels, completes independently, or
  intentionally keeps another object alive.
- Use Thread Sanitizer or the repository's race harness for changed shared state.
