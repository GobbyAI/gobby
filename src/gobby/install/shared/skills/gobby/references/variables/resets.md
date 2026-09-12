# Variable resets and recovery

Load before resetting a setting or recovering unexpected state. Read the
intended session, scope and `exists`, then compare enabled project/global defaults.
There is no generic public unset/reset-all variable tool. Null is a stored override;
copying today's default also remains an override. Deleting a definition does not
clear stored session values.

Use ordinary set only for an authorized user setting. Reserved runtime values,
loaded-instruction tracking, errors, ownership and handoff receipts must be reset
through their owning lifecycle operations. Ordinary resume preserves state;
context-loss reset clears applicable instruction/schema tracking. Do not reset
session-lifetime found-work alerts on turn start or erase dirty-path history.

Operator template restore resets a live definition's value/description. It neither
undeletes definitions nor resets sessions. Missing template/instance, reserved
variable and scope errors need their specific recovery, not repeated blind writes.
See [resets and recovery](../../../../../../../../docs/guides/variables.md#resets-and-recovery) and
[internal state](../../../../../../../../docs/guides/variables.md#internal-variables-set-by-rulesengine).
