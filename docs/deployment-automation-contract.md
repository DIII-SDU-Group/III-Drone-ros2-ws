# Deployment Automation Contract

The canonical machine contract is `deployment/automation-contract.json`; its
plans and durable operation state validate against
`deployment/schemas/v1/automation-plan.schema.json` and
`operation-state.schema.json`. The same ROS-independent Python primitives are
used by local commands and CI adapters.

## Lifecycle

1. **Plan** resolves authenticated repository refs and policy, records exact old
   and new SHAs, passing checks/evidence, permissions, and ordered mutations, then
   computes a content identity. It performs no mutation.
2. **Review** renders human output and structured JSON from the same result. Each
   next action states whether it mutates, its prerequisites, and whether explicit
   confirmation is required.
3. **Apply** binds an operation ID to one immutable plan, atomically retains the
   plan/state under the local `.iii` registry, and revalidates each mutation
   immediately before execution.
4. **Checkpoint** fsyncs every successful mutation and its evidence. A retry or
   resume skips only those exact completed mutation IDs.
5. **Stop** reports success, rejection, partial success, or interruption with a
   stable exit code and an exact status/resume/replan command. A changed plan or
   stale ref is never silently accepted.

The operation families are feature PR, stacked PR, develop-to-main, main-to-
release, qualification, artifact fetch, and deployment handoff. Operation-
specific adapters may add stricter policy but cannot weaken the common plan,
identity, persistence, or result contract.

## Trusted boundaries

Repository policy comes from protected trusted base/release code. Current GitHub
refs, PR merge state, and release/tag existence come from authenticated API calls.
PR bodies and comments can carry machine-readable locators or signed envelopes,
but their prose and marker fields are never authority by themselves.

For linked submodule PRs, the trusted verifier requires the marker set to exactly
match changed III gitlinks, binds every path to the trusted base `.gitmodules`
repository, and then verifies URL, base branch, and merged state through GitHub.
The workflow summary carries
`<!-- iii-linked-submodule-pr-verification-v1 -->` plus a human-readable table.

## CI constraints

Governance workflows use exact action commit pins, bounded timeouts, explicit
least-privilege permissions, non-cancelling concurrency groups, non-persisted
checkout credentials, and immutable short-retention intermediate artifacts.
Write-capable status-comment jobs consume generated evidence only. Candidate PR
code or metadata is never executed with a write token, and trusted policy gates
execute code checked out from the protected base.

## Failure handling

- Exit `0`: successful plan/apply/no-op.
- Exit `20`: policy, trust, stale-ref, permission, or safety rejection.
- Exit `30`: execution failure before a trustworthy completion result.
- Exit `31`: partial mutation; use the exact retained resume command.
- Exit `130`: client interruption; accepted/checkpointed work remains durable.
- Exit `64`/`70`: usage/internal contract errors.

Never edit an operation-state file, reuse an operation ID with another plan,
force a stale plan, or treat a workflow summary/PR marker as trusted evidence.
