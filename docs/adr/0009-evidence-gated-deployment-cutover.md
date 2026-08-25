# Evidence-Gated Deployment Cutover

## Decision

Legacy branch checkout, source synchronization, password SSH, onboard build,
onboard Docker, mutable `latest` publication, and Compose-per-node runtime paths
are removed only after one exact replacement candidate passes the signed Q131
factory, release, field, failure-injection, tuning, recovery, offline,
documentation, and retirement matrix.

The retired deployment repository retains its complete history and receives a
read-only migration pointer. No legacy runtime ownership is copied into the new
architecture, and no acceptance claim may substitute a plan, mock, or skipped
physical test for the required evidence.

## Consequences

Cutover is intentionally the last irreversible phase. Until then, legacy paths may
be blocked or clearly marked retired, but archival and final deletion of supported
entry points wait for the exact signed acceptance record.

