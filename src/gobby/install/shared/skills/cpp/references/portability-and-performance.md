# C++ Portability And Performance

Use this reference when editing platform-specific code, compiler extensions,
hot paths, memory layout, or performance-sensitive APIs.

## Portability

- Check the repo's supported compilers, standard libraries, C++ standard level,
  CPU architectures, operating systems, and build modes before using a feature.
- Guard compiler extensions, attributes, pragmas, intrinsics, and platform APIs.
- Be explicit about endian behavior, alignment, object representation,
  filesystem paths, locale, time, signals, and dynamic-library loading.
- Keep C and C++ ABI boundaries explicit when interoperating with C, Objective-C,
  Rust, Python, Java, or plugin hosts.

## Measurement

- Profile before optimizing unless the inefficiency is obvious and local.
- Compare against representative workloads, not toy inputs.
- Keep benchmarks deterministic and document hardware/toolchain assumptions when
  results influence the design.

## Hot Paths

- Look for unnecessary allocations, copies, string conversions, virtual dispatch,
  type erasure, lock contention, syscalls, cache misses, and template code bloat.
- Prefer clear value semantics and simple data structures until measurements
  justify custom allocators, PMR, intrusive containers, packed layouts, SIMD, or
  branch hints.
- Preserve correctness and tests before changing layout or ownership for speed.

## Build And Compile-Time Cost

- Keep heavy templates, inline functions, and includes out of broad public
  headers unless they are part of the API.
- Use forward declarations, PIMPL, explicit instantiations, and private targets
  where they reduce real compile cost without hurting clarity.
- Watch binary size when adding templates, exceptions, RTTI, static libraries, or
  generated code.
