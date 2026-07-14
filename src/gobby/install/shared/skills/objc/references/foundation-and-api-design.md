# Foundation And API Design

## Public API Shape

- Follow Cocoa naming conventions: readable selector pieces, `init...` initializers,
  Boolean `is...`/`has...` accessors where appropriate, and no redundant type words.
- Return `instancetype` from class factories and initializers when the dynamic
  receiver type is the contract.
- Preserve designated-initializer and convenience-initializer chains. Establish
  invariants before exposing a partially initialized object.
- Keep property mutability, ownership, custom accessor behavior, and copy semantics
  aligned with the declared API.

## Foundation Values And Collections

- Prefer typed Foundation values and lightweight-generic collections at public
  boundaries. Validate external object classes before casting or messaging.
- Distinguish `nil`, `NSNull`, absent dictionary keys, empty collections, and
  sentinel values. Foundation collections cannot store `nil` elements.
- Copy mutable input when the API promises an immutable snapshot. Document and
  test live mutable views when sharing is intentional.
- Use `isEqual:` and a compatible `hash` implementation for value equality. Keep
  collection keys stable while stored.
- Preserve locale, calendar, time zone, encoding, normalization, and formatting
  decisions rather than relying on machine defaults.

## Errors And Exceptions

- Use the established `NSError` convention for recoverable runtime failures.
  Return `NO`, `nil`, or the documented failure value and populate `NSError **`
  only when the caller supplied storage.
- Use stable domains and codes. Put actionable context and underlying errors in
  `userInfo` without leaking credentials or sensitive payloads.
- Reserve Objective-C exceptions for programmer errors and violated API
  preconditions. Do not use exceptions as ordinary network, file, parsing, or
  validation control flow.
- Preserve cancellation and partial-result semantics when translating errors
  across callbacks, delegates, operations, or Swift.

## Dynamic Foundation Conventions

- Preserve KVC-compliant accessor names and collection mutation hooks when a type
  participates in KVC, bindings, Core Data, or serialization.
- Preserve the repository's KVO registration, observation token, context, and
  teardown model. Avoid broad observation of mutable implementation details.
- Implement `NSCopying`, `NSSecureCoding`, comparison, or collection behavior only
  when the complete contract and tests are present.

## Tests

- Cover invalid classes, nil/empty distinctions, mutation after input, equality,
  copying, initialization failure, and localized formatting where relevant.
- Assert error domain, code, underlying cause, and Swift-imported error behavior.
