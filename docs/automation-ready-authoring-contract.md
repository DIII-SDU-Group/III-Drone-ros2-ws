# Automation-Ready Documentation Contract

This contract governs maintained III-Drone operating procedures. The canonical
manual is indexed by [the documentation map](README.md), while domain language
and architecture ownership are indexed by [the context map](../CONTEXT-MAP.md).

Executable workflows must state all of the following:

1. Purpose, target profile, and exact authority boundary.
2. Prerequisites, supported operator hosts, and required physical safety state.
3. A non-mutating preflight command where the component is already available.
4. Any explicit mutation, its effect, and whether a user decision or hardware
   action is required before it may run.
5. Concrete success evidence and the command or observation that obtains it.
6. Stop conditions, recovery, and the next safe action.

Procedures must not invent network addresses, device identities, frame
conventions, or PX4 parameters. When a lab-supplied fact is required, the
document must name it explicitly and keep the affected profile non-bootable
until that fact is recorded and validated.

Architecture and ADR documents link to the owning contracts instead of copying
command sequences. Agent instructions route to the same developer manual used
by operators and are not an independent operational truth.

The developer workflow has no separate documentation manifest or `iii docs`
subcommand. Validate a procedure by checking referenced files and links,
running its stated read-only preflight where available, and running focused
tests for code changes. For `iii` commands, use the live `iii --help` parser
and retain the command's shared `iii.command-result/v1` result when structured
evidence is needed.

Do not hand-edit generated, vendored, dependency-cache, build, install, log,
or dataset trees. A documentation-only change needs `git diff --check`; a
procedure that changes runtime behavior also needs the focused validation for
the affected package or CLI command.
