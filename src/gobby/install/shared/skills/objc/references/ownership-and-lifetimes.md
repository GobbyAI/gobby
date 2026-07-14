# Ownership And Lifetimes

## Establish The Mode

- Determine ARC per target and source file before changing ownership code.
- Under ARC, never add explicit `retain`, `release`, `autorelease`, or
  `[super dealloc]`. Preserve explicit cleanup for non-object resources.
- Under MRC, follow the create/copy/retain ownership rules, balance every owned
  object on all exits, release owned ivars in `dealloc`, then call `[super dealloc]`.
- Keep mixed ARC/MRC boundaries explicit. Do not convert one file incidentally
  while solving an unrelated behavior change.

## Properties And References

- Use `strong` under ARC or `retain` under MRC for owned object relationships.
- Use `copy` for stored blocks and for values whose mutable subclass must not leak
  through an immutable API contract, commonly strings and declared copyable values.
- Use `weak` for supported zeroing non-owning relationships. Check deployment and
  class support; use an established alternative when zeroing weak references are
  unavailable.
- Use `assign` for scalar values. An object `assign` or `unsafe_unretained`
  relationship can dangle and requires an explicit lifetime proof.
- Match property attributes, ivar qualifiers, synthesized accessors, and custom
  getter/setter behavior.

## Autorelease Pools And Temporary Lifetimes

- Treat autorelease pools as scoped lifetime boundaries, not ownership transfer.
- Add local `@autoreleasepool` scopes to long loops, worker threads, or batch jobs
  only when profiling or the host lifecycle shows retained temporary growth.
- Ensure manually created threads and long-lived callbacks follow the host's pool
  conventions.
- Do not rely on an autoreleased value after its pool drains.

## Core Foundation And C Resources

- Apply the API's create/copy/get ownership rule to Core Foundation values.
- Under ARC, use `__bridge` for no transfer, `__bridge_retained` when transferring
  an Objective-C object to a retained CF reference, and `__bridge_transfer` when
  transferring an owned CF reference to ARC.
- Balance `malloc`, file descriptors, locks, dispatch sources, callbacks, and
  opaque C handles independently from Objective-C object ownership.
- Make cleanup correct on success, failure, cancellation, partial initialization,
  and object teardown.

## Review Checklist

- Draw the ownership graph for delegates, parents/children, caches, observers,
  timers, tasks, callbacks, and blocks.
- Verify the object that owns cleanup remains alive until cleanup completes.
- Exercise deallocation in tests or Instruments when the change can introduce a
  cycle, leak, dangling reference, or premature teardown.
