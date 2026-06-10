# Ruby Configuration

## Project Shape

- Identify the project type before editing: gem, Rails app, Rails engine,
  Hanami/Sinatra app, command-line tool, Chef/Fastlane/CocoaPods DSL project, or
  monorepo package.
- Follow `.ruby-version`, `.tool-versions`, `.ruby-gemset`, `Gemfile`,
  `Gemfile.lock`, `gems.rb`, `gems.locked`, gemspecs, and CI matrix policy.
- Preserve Bundler groups, platforms, source ordering, local path/git gems,
  engine constraints, and package release metadata.
- Keep generated files generated. Do not hand-edit lockfiles, schema files, RBI,
  RBS, or binstubs unless the repo workflow says to.

## Dependencies

- Prefer standard library, Rails APIs, ActiveSupport, and existing local helpers
  before adding gems.
- Add dependencies through the existing package workflow, then update the
  lockfile with the repo's Bundler version.
- Check transitive impact before changing `ruby`, Rails, Rack, Zeitwerk,
  Sidekiq, Active Record adapter, serializer, or HTTP client versions.
- Keep security-sensitive dependencies pinned according to local policy.

## Tooling

- Use the configured formatter/linter: RuboCop, Standard, rufo, syntax_tree, or
  repo wrapper.
- Preserve RuboCop inheritance, department enables, target Ruby version, pending
  cops, TODO files, and local disables.
- If Sorbet, Steep, RBS, ruby-lsp, or Tapioca is configured, update signatures
  and generated artifacts through project commands.
- Match Rails environment config, initializers, credentials, secrets, routes,
  autoload paths, engines, and Zeitwerk naming.

## Commands

Use focused project commands where available:

- `bundle exec rubocop path/to/file.rb`
- `bundle exec standardrb path/to/file.rb`
- `bundle exec rspec spec/path/to_spec.rb`
- `bin/rails test test/models/user_test.rb`
- `bundle exec srb tc` or `bundle exec steep check`
- `bundle exec rake test` only when the touched area has no narrower target

## Before Changing Config

- Confirm which environments are affected: development, test, CI, staging, and
  production can diverge.
- Keep boot-time, eager-load, autoload, credentials, and initializer behavior
  compatible.
- For gems, preserve public gemspec metadata, required Ruby version, file lists,
  executable names, and release automation.
