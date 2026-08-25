# Automation-Ready Documentation Contract

This contract governs maintained III-Drone operating procedures. The canonical
manual is indexed by [the documentation map](README.md), while domain language
and architecture ownership are indexed by [the context map](../CONTEXT-MAP.md).

Executable workflows must state all of the following:

1. Purpose and the exact authority boundary.
2. Prerequisites, supported operator hosts, target profiles, and required safety state.
3. A non-mutating plan or preflight command.
4. The exact explicit mutation command and whether confirmation is required.
5. Human output, structured-output schema, and stable exit-status families.
6. Retained evidence and its verification command.
7. Interruption, operation-ID reattachment, and idempotent resume behavior.
8. Rollback, recovery, and stop conditions.
9. Context-aware `Next:` commands, including prerequisites and mutation labels.

Architecture and ADR documents link to the owning contracts instead of copying
command sequences. Agent instructions route to the same manual and schemas used by
operators and CI; they are not an independent operational truth.

Generated, vendored, third-party, dependency-cache, build, install, log, dataset,
artifact, and sealed evidence trees are excluded explicitly by
`deployment/documentation-policy.json`. The reviewed inventory is
`deployment/documentation-manifest.json` and is validated offline.

