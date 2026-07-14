# Swift And C-Family Interop

## Swift-Facing Headers

- Place public app-target Objective-C headers in the configured bridging header.
  Expose framework headers through the framework's public umbrella/module surface.
- Wrap audited public regions with `NS_ASSUME_NONNULL_BEGIN` and
  `NS_ASSUME_NONNULL_END`; annotate every genuine nullable result, parameter,
  property, block, and nested pointer level accurately.
- Add lightweight generics to Foundation collection declarations so Swift imports
  element and key/value types. Keep generic bounds compatible with Objective-C's
  class-constrained model.
- Use `instancetype`, refined imported names, and Swift naming attributes only
  when they improve an established public contract and compile in the minimum SDK.
- Verify the generated Swift signature. Unannotated object pointers import as
  implicitly unwrapped optionals and hide missing-data semantics.

## Mixed-Language Targets

- For app targets, maintain the configured Objective-C bridging header. For
  frameworks, maintain public header visibility, umbrella imports, and module
  settings.
- Import the generated `ProductModuleName-Swift.h` only where Objective-C must call
  Swift. Avoid exposing that generated header from another public header.
- Keep circular imports out of headers with forward declarations and narrow
  interfaces. Put concrete imports in implementation files when possible.
- Test representative Objective-C-to-Swift and Swift-to-Objective-C call sites
  after changing selectors, nullability, errors, protocols, blocks, or generics.

## C, Objective-C++, And Core Foundation

- Keep C-callable APIs under `extern "C"` guards when included by C++ and expose
  Objective-C declarations only in Objective-C-capable contexts.
- Hide C++ types, templates, exceptions, ownership, and standard-library objects
  behind `.mm` implementations, Objective-C facades, or narrow C handles.
- Define who allocates, retains, releases, and invalidates values at every C or
  Core Foundation boundary. Match ARC bridge casts to that transfer contract.
- Avoid passing Objective-C exceptions through C++ frames or C ABI boundaries.
  Translate failures at the boundary using the repository's result convention.

## Import Quality Checklist

- Compile the changed public header as each supported consumer language.
- Inspect Swift optionality, collection element types, method names, error import,
  block closure shape, and protocol conformance.
- Verify availability annotations and module visibility at the minimum supported
  platform version.
- Add a small compile or behavior test in the client language for any changed
  cross-language contract.
