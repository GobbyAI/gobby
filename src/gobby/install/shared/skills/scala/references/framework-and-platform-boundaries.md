# Framework And Platform Boundaries

## JVM And Java Interop

- Normalize nullable Java returns immediately. Convert `Optional`, Java
  collections, `CompletionStage`, checked exceptions, and SAM interfaces at a
  narrow adapter boundary.
- Keep collection conversions explicit about mutability, copying, ordering, and
  view semantics.
- Preserve Java-visible names, annotations, overloads, static forwarders, and
  erasure-sensitive signatures when the API has Java consumers.
- Check source and binary compatibility for published libraries. Treat TASTy,
  inline definitions, enum encodings, and generated methods as part of the
  release surface.

## Cross-Platform Code

- Keep JVM APIs out of Scala.js, Scala Native, and shared source sets. Isolate
  platform implementations behind shared interfaces or the build's source-set
  mechanism.
- Validate behavior on every affected target when serialization, numeric types,
  concurrency, filesystem access, reflection, or native interop changes.
- Keep platform-specific dependencies and compiler/linker options in their
  owning target.

## Framework Boundaries

- Keep Play/Akka/Pekko transport messages, routes, actors, and persistence
  records separate from validated domain models.
- Keep Cats Effect and ZIO environment/layer wiring at composition boundaries.
  Domain services should expose the smallest effectful interface required by
  callers.
- In Spark and other distributed runtimes, review closure capture, encoder and
  schema compatibility, serialization, partition behavior, and executor-side
  availability before moving ordinary JVM code into distributed callbacks.
- Preserve framework-managed lifecycle, dispatcher, scheduler, transaction,
  request-context, and thread-affinity contracts.

## Data And Generated Code

- Validate JSON, protobuf, database, configuration, and message-bus inputs before
  constructing domain types.
- Preserve field names, enum values, defaults, unknown-field behavior, numeric
  precision, and versioning rules at serialization boundaries.
- Update generator inputs and regenerate outputs through the repository command.
  Keep hand-written behavior in adapters rather than generated files.
