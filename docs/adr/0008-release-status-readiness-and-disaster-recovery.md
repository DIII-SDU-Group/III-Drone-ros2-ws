# Release Status, Readiness, And Disaster Recovery

## Decision

Qualified release artifacts and tags are immutable even when defects are found.
Withdrawal uses append-only independently signed `qualified`, `withdrawn`, or
`unsafe` status statements. New installation or activation of withdrawn/unsafe
releases is blocked; existing code is never switched autonomously merely because a
new status is learned.

Field readiness is a read-only, sealed PASS/WARN/FAIL observation and never an
authorization token. Failures cannot be waived. Acknowledged warnings retain their
severity and rationale.

Portable local evidence is held in the Git-ignored `.iii/` record registry and can
be archived deterministically without credentials, Wi-Fi secrets, private keys, or
machine identity. Complete SSH-authority loss has no hidden password, receiver
bypass, or boot-partition injection path: the supported recovery is read-only media
salvage, physical reimage, compatible state restore, fresh enrollment, and
recommissioning.

## Consequences

Release-status signing and bundle signing are independently rotatable. Offline
operation uses the newest verified cached status index and reports its age.
Repositories and compact GitHub attestations do not replace operator-managed
archives of bulky or irreplaceable local evidence.

