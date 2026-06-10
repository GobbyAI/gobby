# Configuration

Use this reference before changing Kotlin build files, compiler flags, source
sets, dependencies, or CI.

## Project Shape

- Identify the build system first: Gradle Kotlin DSL, Gradle Groovy DSL, Maven,
  Android Gradle Plugin, Kotlin Multiplatform, included builds, or convention
  plugins.
- Prefer the checked-in wrapper: `./gradlew`, `gradlew.bat`, `./mvnw`, or repo
  scripts.
- Match the configured Kotlin version, Java toolchain, Android Gradle Plugin,
  Compose compiler, KSP/KAPT, serialization plugin, native targets, and CI
  matrix.
- Keep source-set wiring explicit. Do not move code between `commonMain`,
  `jvmMain`, `androidMain`, `iosMain`, `jsMain`, or test source sets unless the
  platform contract changes intentionally.

## Dependencies

- Preserve version catalogs, dependency locks, BOMs, plugin management, and
  dependency scope. Avoid one-off versions in module build files when the repo
  centralizes versions.
- Do not add libraries for functionality already covered by the Kotlin standard
  library, kotlinx libraries already in use, platform SDKs, or local helpers.
- Keep annotation processors and code generators deterministic. KSP and KAPT
  changes must include generated-code and incremental-build implications.
- Avoid mixing incompatible coroutine, serialization, Compose, AndroidX, or
  Kotlin compiler plugin versions.

## Compiler And Static Analysis

- Preserve explicit API mode, progressive mode, `allWarningsAsErrors`,
  nullability annotations, opt-in annotations, binary compatibility validation,
  Detekt, ktlint, Spotless, Android lint, and custom rule sets.
- Treat `@OptIn`, `@Suppress`, compiler flag changes, and Detekt exclusions as
  design decisions. Keep them narrow and explain why the local rule is wrong for
  the changed code.
- Prefer typed Gradle accessors and convention plugins over duplicated module
  setup.

## Commands

Choose focused commands that compile and test the changed source set:

- `./gradlew :service:compileKotlin`
- `./gradlew :service:test --tests com.acme.ProfileClientTest`
- `./gradlew :app:detekt :app:ktlintCheck`
- `./gradlew :shared:jvmTest :shared:iosSimulatorArm64Test`
- `./gradlew :app:lintDebug` when Android resources, manifests, Compose, or
  lifecycle boundaries are involved

Record the exact tasks and explain why they cover the changed platform/module.
