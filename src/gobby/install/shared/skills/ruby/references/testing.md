# Ruby Testing

## Test Selection

- Use focused tests for touched behavior before broader suite runs.
- Match the repo's framework: RSpec, Minitest, Rails test, Cucumber, Capybara,
  minitest-spec, or custom wrappers.
- Prefer pure unit tests for domain objects and integration tests for Rails
  routing, controller/request behavior, database queries, jobs, and mailers.
- Add regression coverage for failure paths, nil/blank inputs, authorization,
  retries, and boundary validation.

## RSpec

- Keep examples behavior-focused. Avoid testing private methods directly unless
  legacy code leaves no better seam.
- Use `let` and shared examples sparingly; make setup readable at the example.
- Prefer verifying doubles for collaborators when the project uses RSpec mocks.
- Use request/job/model/service specs according to local conventions.

## Minitest And Rails Test

- Use fixtures, factories, or builders consistently with the repo.
- For Rails, use the local helpers for Active Job, Action Mailer, time travel,
  system tests, and database cleanup.
- Keep test data minimal and explicit; avoid relying on global fixture state
  accidentally.

## External Effects

- Mock network, filesystem, clock, queue, and email boundaries with project
  helpers such as WebMock, VCR, Timecop/ActiveSupport time helpers, and job
  adapters.
- Mock external services, not the internals of the code under test.
- Ensure retries, idempotency, and timeout behavior are covered where changed.

## Commands

Prefer the narrowest command that proves the changed behavior:

- `bundle exec rspec spec/services/account_notifier_spec.rb`
- `bundle exec rspec spec/requests/accounts_spec.rb:42`
- `bin/rails test test/models/account_test.rb`
- `bin/rails test test/system/account_flow_test.rb`
- `bundle exec rake test TEST=test/foo_test.rb`

Then run related lint/type commands for touched files.
