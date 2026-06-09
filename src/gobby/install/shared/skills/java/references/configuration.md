# Java Configuration

Use this reference before changing Java build files, dependency metadata,
toolchains, formatter/static-analysis setup, generated sources, or module
boundaries.

## Build Tool Rules

- Prefer the checked-in wrapper: `./gradlew`, `./mvnw`, or repo scripts.
- Match the configured JDK/toolchain. Do not bump `sourceCompatibility`,
  `targetCompatibility`, Maven compiler release, Gradle toolchains, or CI images
  incidentally.
- Keep dependency scopes intentional: `implementation` vs `api`,
  `compileOnly`, `runtimeOnly`, `testImplementation`, Maven `provided`,
  `runtime`, and test scopes all change consumers.
- Preserve dependency locking, version catalogs, BOMs, enforcer rules, and
  repository mirrors.
- Do not add plugins, annotation processors, or generated-source directories
  without wiring their output into clean, compile, test, and IDE/source-set
  behavior.

## Maven

- Keep parent POMs and BOM imports authoritative.
- Prefer `maven-enforcer-plugin`, toolchains, and compiler `release` over
  ambiguous local JDK assumptions.
- Avoid moving dependencies between parent and child POMs unless every module
  that inherits the change needs it.
- Run the narrow module command first, for example:
  `./mvnw -pl service -am test -Dtest=ProfileClientTest`.

## Gradle

- Prefer version catalogs and convention plugins when the repo already uses
  them.
- Avoid `allprojects` or `subprojects` expansion unless the repo is already
  organized around those hooks.
- Keep Kotlin DSL and Groovy DSL style consistent with surrounding files.
- Run targeted tasks first, for example:
  `./gradlew :service:test --tests com.acme.profile.ProfileClientTest`.

## Static Analysis And Formatting

- Treat Checkstyle, PMD, SpotBugs, Error Prone, NullAway, ArchUnit, and Spotless
  failures as design feedback.
- Do not suppress warnings unless the suppression is local, documented, and tied
  to a real boundary.
- Prefer generated-code exclusions over broad source-set exclusions.

## Modules And Packages

- Keep package names stable for public APIs, serialization, reflection, and
  framework scanning.
- Avoid split packages in JPMS or multi-module builds.
- Keep package-private types package-private when they are implementation
  details.
