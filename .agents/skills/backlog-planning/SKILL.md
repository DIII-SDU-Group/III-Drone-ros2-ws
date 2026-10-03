---
name: backlog-planning
description: Plan a substantial instruction batch or specification as an execution-ready backlog, reviewing document coverage before asking the user for decisions. Also use for plan-only requests.
---

# Backlog planning

The parent coordinates; a `terra_backlog_writer` owns all code and source
inspection, design synthesis, and backlog writing. Reuse an available writer by
default for any planning work with even slight useful overlap, including a new
backlog. Open a new writer only when the work is completely unrelated to the
available writer's context, that context is full or nearly full, or a necessary
capacity change requires replacement. Dispatch with an explicit
`agent_type`, `fork_turns = "none"`, and a compact packet naming the user's aim,
original instructions or documents, repository authority, expected backlog
path, and any known constraints. Let the writer read large documents at their
source rather than copying them into the parent context. The parent does not
draft the backlog or repeat the writer's investigation. If custom-agent routing
is unavailable, report that this workflow cannot run; do not substitute a
generic agent or silently plan in the parent.
Only the parent dispatches the writer and verifier; neither may delegate.

The writer produces or updates `codex-backlogs/<slug>.md`, preserving useful
existing content. It inspects relevant code and governing sources, records the
objective, scope, authority, design decisions, constraints, code findings, and
open questions, then creates a first draft before returning. Preserve a durable
pointer to each original instruction or document for the final execution review;
if the only source is pasted chat text, preserve its exact wording in an
adjacent source file or backlog appendix rather than only a summary. Every task
needs a stable ID, coherent outcome, dependencies, owned paths or interfaces, critical
invariants, observable acceptance criteria, focused verification, and role or
risk notes. Use `Incomplete`, `In-Progress`, and `Completed`; mark any task that
depends on an unresolved decision `Needs decision`. Note shared files so later
writers can be scheduled without overlap. Planning does not implement tasks.

In that first-draft handoff, the writer identifies all material inconsistencies,
ambiguities, missing decisions, and source conflicts it found, even when it
recommends an answer. It returns one numbered question set with context,
source/code pointers, consequences, recommendations, and dependencies, plus the
draft path and a compact findings handoff. Do not ask questions already settled
by code, governing instructions, or prior user answers. If none remain, say so
and mark the draft ready for the parent's consistency check or document review.

For a plan grounded in a substantial instruction or specification document of
any format, including a pasted document, spawn one independent
`terra_backlog_verifier` **before asking the user questions**. Give it the
original document(s), first draft, and proposed question set. It independently
checks coverage, conflicts, authority, dependencies, acceptance criteria, and
whether the question set is complete. It edits only the assigned backlog to fix
supported gaps and returns one corrected decision set. This is the independent
coverage pass; do not automatically add a second verifier after the interview.
Loose instructions without a substantial document do not require this pass.
The verifier must not be the writer of the draft it reviews. A verifier with
useful context from a related review may be reused only if its independence
from this draft and context capacity remain intact.

The parent resolves questions answered by existing authority or the user's
instructions. Conduct one decision round over the remaining set:

- If the user deliberately requested no follow-up questions, resolve what can
  be resolved from the stated scope and authority. Record assumptions and mark
  dependent work `Needs decision` where a consequential answer is unavailable.
- Otherwise, present independent questions together in one concise numbered
  message, each with a recommended answer and consequence. Ask dependent
  questions sequentially only when an earlier answer changes what must be
  asked next. Honor an explicit request for one-at-a-time questioning. Do not
  make the user repeat the writer's investigation.

If no user decisions remain and the verified draft is ready, the parent may
proceed without another writer turn. Otherwise send the consolidated answers
and verifier corrections back to the **same writer** as one follow-up while its
context has capacity. If that context is full or nearly full, use a fresh
`terra_backlog_writer` with a concise handoff and the draft instead. The
writer finalizes the draft, records decisions and provenance, and returns
ready tasks and `Needs decision` limits. Do not run a second discovery or
interview cycle by default. Newly exposed uncertainty
blocks only dependent work and is surfaced when a decision is actually needed.

The parent checks that the writer and, when required, verifier reached terminal
results and that the final backlog incorporates the answers consistently. A
plan-only request ends with the backlog path and readiness/blockers. When the
user asked to execute a substantial batch, continue in the same chat with
`backlog-execution` on the ready tasks; do not seek an extra invocation or
approval merely to follow the plan. The backlog never grants deployment,
migration, external-write, destructive, or governed-successor authority.
