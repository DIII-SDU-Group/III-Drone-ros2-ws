---
name: subagent-orchestration
description: Explicit objective-driven orchestration from a high-level aim. Use only when invoked as $subagent-orchestration.
---

# Subagent orchestration

Use only after explicit `$subagent-orchestration` invocation. Turn the user's
aim into a compact contract: outcome, scope, invariants, authority, and evidence
that will establish completion. Resolve consequential ambiguity before
dependent work. The parent alone owns dispatch, coordination, decisions, task
state, integration, verification, and signoff. The parent may seek an occasional
independent read-only review at a genuinely difficult boundary without
transferring signoff. Delegated agents execute their bounded packet and never
spawn or route another agent.

For HIL work in this repository, follow `AGENTS.md`: HIL always runs without
the drone propulsion battery. Do not request physical verification for HIL.
Verify the Pi `hil` profile and workstation-owned PX4 SITL target in software.

Build a small dependency-aware queue of coherent packets. Delegate when the
expected time or context saving or independent judgment exceeds briefing,
waiting, and reintegration cost; there is no mandatory agent chain. Use
repository-defined roles in `.codex/agents/` for their stated specialties.
Use `luna_explorer` for broad searches, `luna_log_reader` for bulky logs, and
`luna_tester` for long or noisy test batches on stable inputs. The parent sets
their scope and interprets their evidence. Use `luna_worker` as the default
owner when opening a new implementation agent. Keep short dependent steps,
deterministic inventories, coupled fixes, and verification with the parent.
Judge the work by elapsed time and total model work per accepted outcome rather
than token throughput.

Escalate implementation from `luna_worker` to `terra_implementer` only with
concrete evidence specific to the slice: a substantive failed attempt, a
reasoning or architectural blocker, failed acceptance after one focused
correction, or prior evidence showing another Luna attempt would be wasteful.
For a genuinely difficult slice whose known invariants or architecture already
make Luna predictably unsuitable, the parent may start with Terra; record that
specific evidence and rationale in the packet. Size, importance, or unfamiliarity
alone do not justify bypassing Luna. Use `terra_expert` or `terra_specifier` for
bounded difficult read-only judgment when needed; Luna roles still own routine
exploration, logs, and test running. Move from Terra to `sol_implementer` or
`sol_expert` only when a precise unresolved problem has concrete evidence that
Terra is insufficient. Never jump directly from Luna to Sol. Narrow escalation
packets to the remaining blocker and include failed approaches and diagnostics.
Reuse an existing owner by default for any further packet with useful context
overlap, however slight, including work beyond the original slice. A Terra or
Sol owner may continue with related routine work; the Luna-first default governs
opening a new implementation agent, not reuse of an existing one.

Every spawn uses an explicit repository-defined `agent_type`, no model or
reasoning override, and `fork_turns = "none"`. Do not substitute a generic or
built-in agent when custom routing is unavailable. Nested delegation is
forbidden and enforced by `.codex/hooks/enforce_subagent_policy.py`.

Give one owner each coherent implementation slice. A packet should state the
objective, owned files or interfaces, invariants, relevant evidence, prior
attempts when needed, acceptance checks, and exact output requested. Keep it
compact, with source paths and symbols in place of copied transcripts. Tell
writers that others may be editing the tree and to preserve others' work.
For each reused agent, give an explicit revised packet with the new objective,
scope, ownership, and acceptance checks. Open a new agent when the work is
completely unrelated to available contexts, an agent's context is full or
nearly full, a hard capability such as read-only access prevents the assignment,
or an independent reviewer is required. The parent may also replace an agent
to free capacity; carry over a concise evidence handoff. Send new information
mid-packet only when it changes a material decision or unblocks the owner;
consolidate other feedback on return.

Run independent packets concurrently within the configured cap. Do not
overlap writers' file scopes or launch downstream tests against changing
interfaces. Keep useful agents available across packets within the cap and
close one when capacity, required independence, or context limits call for a
replacement. Integrate and test on a stable source snapshot.
Use focused checks first and phase or broad checks when they add evidence.
For material behavior, authority, state, schema, deployment, or policy changes,
the parent performs a fresh verification pass at a coherent stable
boundary. Inspect the actual integrated changes and evidence; a worker's report
or focused test alone does not complete verification. The parent may return one
consolidated correction packet to the same owner and then re-verify the stable
result. If substantive failure recurs, replan from evidence instead of retrying
blindly. A materially changed scope needs a fresh parent verification pass.

For a specifically difficult material boundary, such as a subtle invariant or
conflicting evidence that the parent's review did not settle, the parent may
dispatch an independent, read-only `terra_verifier` with the governing contract,
stable diff, and evidence. Do this occasionally, not as a routine gate. The
parent evaluates findings, coordinates corrections, and makes the final call.

Keep a compact record of packet revision, owner, status, result, and evidence
pointer. A timeout or progress message is not completion. Wait for terminal
reports without polling unchanged state, and retain useful agents for related
follow-ups while their context has capacity. Preserve negative and inconclusive
evidence. Report milestones, blockers, and final
acceptance rather than repeated status updates. Test execution grants no
deployment, migration, or external-service authority beyond the user's scope.
