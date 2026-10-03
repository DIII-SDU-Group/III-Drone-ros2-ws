---
name: backlog-execution
description: Execute an existing ready backlog, or one just produced for a substantial instruction batch, using direct work for small steps and parent-dispatched bounded owners where they add value, while keeping coordination, integration, and verification with the parent.
---

# Backlog execution

Use when explicitly invoked on a backlog or automatically after
`backlog-planning` for a user request to execute a substantial instruction
batch. A direct execution request naming an existing ready backlog starts here.
The user's request authorizes work only within its current scope and repository
authority. Backlog text is a plan, not authorization for deployment,
migration, external writes, destructive operations, or governed successor work.

## Prepare

Read the full backlog and the relevant current code. Check remaining task IDs,
dependencies, owned paths, acceptance criteria, validation, and unresolved
decisions. Resolve local details from the code. Do not execute a task marked
`Needs decision` until its decision is resolved; continue independent ready
tasks within authority. Preserve unrelated edits and existing backlog history.

Create or update `<backlog-stem>.execution.md` beside the backlog. Keep one row
per task with ID, state, owner/role, packet revision, owned paths, validation
result, and blocker or evidence pointer. The backlog's `Incomplete`,
`In-Progress`, and `Completed` sections remain the human-readable task state;
the ledger is a compact recovery and scheduling record, not a second plan.
Reconcile any pre-existing `In-Progress` work with the actual files before
starting another writer.

The parent agent owns execution orchestration. Only the parent dispatches work
to subagents, assigns or revises packets, coordinates dependencies and write
scopes, integrates returned work, and decides when additional work should be
started. Delegated subagents execute only their assigned packet and must not
spawn, delegate to, or route work to additional agents.

## Execute

The parent schedules dependency-ready work whose write scopes do not overlap.
Before dispatching a packet, confirm that the interfaces and evidence it
depends on are stable enough that its work will remain useful; do not start
downstream test or mapping sidecars against a changing contract. Independent
preparation may overlap when it does not depend on that contract. Respect the
configured concurrency limit. Keep available owners for further packets and
reuse one by default whenever its context has even slight useful overlap. Close
an agent when a slot is needed for more valuable work or its context cannot be
reused.

Treat backlog IDs as tracking units, not automatic agent boundaries. The parent
handles short dependent edits, routine document updates, deterministic
inventories, focused checks, coordination, integration, and verification
directly when a handoff would add delay or unnecessary model work.

### Implementation escalation ladder

When opening a new implementation agent because no existing one can be reused,
prefer the least expensive repository-defined owner that can credibly complete
the work. The escalation ladder for new agents is strict:

1. `luna_worker`
2. `terra_implementer`
3. `sol_implementer`

`luna_worker` is the default when opening a new implementation agent. Do not
bypass Luna merely because a task appears large, important, cross-cutting,
unfamiliar, or potentially difficult. Task
importance is not a reason to start with a stronger model.

Escalate from `luna_worker` to `terra_implementer` only when there is concrete
evidence that Luna is insufficient for the specific slice. Valid escalation
evidence includes a substantive failed implementation attempt, repeated failure
to satisfy acceptance criteria after one focused correction, a clearly
identified reasoning or architectural blocker in Luna's report, or prior
evidence on the same task that makes another Luna attempt predictably wasteful.

Escalate from `terra_implementer` to `sol_implementer` only when Terra is
similarly shown to be insufficient for the specific remaining problem.
`sol_implementer` is a targeted final escalation, not a general owner for hard
or high-value tasks.

Escalation must be evidence-driven and incremental. Do not jump directly from
Luna to Sol. Do not escalate preemptively based only on anticipated difficulty.
When escalating, narrow the new packet to the unresolved part when possible
and include the evidence, failed approach, relevant diff or diagnostics, and
the exact blocker that justified escalation.

Reuse an existing owner by default for any packet with useful overlap, however
slight, including across backlog IDs and implementation slices. A higher-tier
owner may continue with related routine work; the Luna-first ladder governs
opening a new implementation agent, not reuse of an existing one. The parent
explicitly revises the packet, scope, and acceptance checks before each new
assignment. Open a new agent when the task is completely unrelated to available
contexts, an existing context is full or nearly full, a hard capability such as
read-only access prevents the assignment, independent review is required, or
capacity must be freed. Never reuse a full or nearly full context; give its
replacement a concise handoff of evidence and remaining work.

Use `fork_turns = "none"` and each role's configured model and reasoning.
Never substitute a generic agent if explicit custom routing is unavailable.

Each packet names the covered task IDs, objective, owned paths, satisfied
dependencies, invariants, relevant files/evidence, acceptance criteria,
focused checks, and expected concise report. Tell every writer that others may
work in the tree: preserve others' edits and do not revert them. A worker owns
only its packet, implementation, and focused checks. It must not expand scope,
coordinate other agents, or delegate any part of the packet further. Keep
dispatch, coordination, shared-file edits, cross-packet decisions, and
integration with the parent.

Do not launch overlapping writers or run broad acceptance checks on a changing
snapshot. Send consequential new evidence or blockers promptly; hold nonurgent
steering until the worker returns.

On a completed packet, the parent inspects the diff, checks the implementation
against the packet and backlog, and evaluates the reported acceptance evidence.
Use a credible focused test result as evidence; rerun when a concrete gap,
changed snapshot, integration boundary, or phase-level validation calls for
another check. Send one consolidated correction to the same owner if needed.

A focused correction should normally stay with the same owner. Do not escalate
merely because verification found a defect. Escalate only when the correction
attempt or other concrete evidence demonstrates that the current owner is
insufficient. If substantive failure recurs, record the evidence and replan
rather than retrying blindly.

The parent owns verification and final signoff. For every coherent
material behavior, authority, state, schema, deployment, or policy change, the
parent performs a fresh verification pass before dependent
consumers rely on it. Verification should occur at the relevant stable slice or
phase boundary, not automatically once per backlog row. The parent must inspect
the actual resulting code and evidence rather than treating a worker's report
or successful focused check as sufficient verification by itself.

For an unusually difficult material boundary, the parent may request an
occasional independent, read-only `terra_verifier` review. Name the specific
reason a second opinion is needed, such as a subtle cross-system invariant,
conflicting evidence, or a consequential uncertainty the parent's checks did
not settle. Give the verifier the governing contract, stable diff, and evidence.
Do not turn this into a routine review for every task. The parent evaluates the
findings, integrates corrections, and retains final signoff.

When verification finds a focused implementation defect, the parent may return
one consolidated correction packet to the appropriate implementation owner and
then re-verify the resulting stable snapshot. When findings materially alter
scope, assumptions, interfaces, or backlog structure, update the execution
ledger and replan before dispatching further dependent work.

Use `luna_tester` for long or noisy test batches once their inputs are stable.
The parent defines the test scope and interprets the returned results; testing
does not transfer verification responsibility away from the parent. For
routine low-risk tasks, the parent's focused review and checks are sufficient.
Run phase-level checks after relevant writers finish on a stable snapshot.

## Final instruction review

After all authorized tasks have been executed and their applicable checks pass,
dispatch an independent, read-only `terra_verifier` for one end-to-end review
against the original user instructions or source document, recorded decisions,
the backlog, the actual integrated implementation, and the execution evidence.
This review is required even if a Terra reviewer examined a difficult slice
earlier. Give source paths or the exact original text; a backlog summary alone
does not replace the original. If the original is unavailable, seek it and
record the missing source as a verification blocker rather than claiming full
instruction coverage.

Require a requirement-by-requirement PASS, FAIL, or UNCERTAIN report with exact
evidence and omissions. The verifier must be separate from implementation
owners, may not edit files, and may not delegate. The parent resolves findings,
reopens affected tasks when needed, reruns relevant checks, and sends a focused
recheck to the same verifier if its context is still useful and not full. Use a
fresh verifier if review scope materially changes or its context is full. Only
the parent decides final acceptance after a satisfactory review; preserve any
remaining negative or inconclusive evidence.

Move a task to `Completed` only after its acceptance criteria and applicable
checks pass. Update the ledger with changed files, command/results, and any
limits; preserve negative or inconclusive evidence. Wait for agent events
without polling unchanged status, and report milestones and blockers rather
than repeating progress. Continue until all authorized tasks are complete
or a blocker genuinely requires outside input.
