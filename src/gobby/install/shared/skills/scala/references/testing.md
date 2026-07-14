# Testing

## Match The Repository Stack

- Use the existing framework and style: MUnit, ScalaTest, Weaver, ZIO Test,
  JUnit, Specs2, uTest, or a project-specific harness.
- Keep tests beside the affected module and configuration. Reuse fixtures,
  effect runtimes, actor test kits, containers, and generators already present.
- Prefer focused unit and boundary tests before aggregate or cross-build tasks.

## Behavioral Coverage

- Cover valid and invalid smart-constructor inputs, enum/sealed exhaustiveness,
  contextual resolution, serialization, Java nulls, and error translation.
- Test effect success, typed failure, defects where relevant, cancellation,
  timeout, retry exhaustion, resource release, and blocking boundaries.
- Use ScalaCheck or framework generators for algebraic laws, codecs, parsers,
  collection invariants, and state machines when the input space matters.
- Use deterministic test clocks, schedulers, execution contexts, and runtime test
  services. Avoid sleeps and real network dependencies in focused tests.

## Compile-Time Contracts

- Add compile-time checks when the behavior is type safety: accepted and rejected
  constructions, implicit/given resolution, variance, derivation, match types,
  macros, or public API compatibility.
- Use the framework's compile-check support or Scala 3 compile-time testing APIs.
  Assert the relevant diagnostic or guarantee rather than an incidental full
  compiler message.

## Focused Commands

Use the exact module and suite syntax configured by the build:

```bash
sbt "module/testOnly com.acme.OrderSpec"
sbt "module/testOnly com.acme.OrderSpec -- -z decoder"
mill module.test.testOnly com.acme.OrderSpec
scala-cli test src --test-only com.acme.OrderSpec
```

Run formatter/static analysis and the focused compile target with the test. Add
cross-version or platform runs when the changed source is shared. Use broad
aggregates only when the affected integration surface requires them.
