# HIL Step 1 Correction — Continuation Handoff

Date: 2026-09-22 (Europe/Copenhagen)

Workspace: `/home/ffn/Workspace/III-Drone-ros2-hil-isolated`

## 1. Stop point

The user stopped the correction workflow after Luna completed implementation
Batch 2 and while the parent agent was reviewing that batch. Do not treat the
correction as finished:

- Batch 1 is implemented and parent-reviewed.
- Batch 2 is implemented; its focused offline tests and shell syntax check pass.
  Parent review had started and found no defect yet, but was not formally
  completed before the stop.
- Batch 3, which fixes report rendering, has not been implemented.
- The required final Terra review has not happened.
- No corrected live HIL attempt has been run.

The current source tree is intentionally dirty. All existing work must be
preserved. Do not reset, clean, discard, rebase, or rewrite unrelated changes.

## 2. Objective

This is Step 1 of the attached HIL endurance investigation. The intended Step-1
probe is a single bounded, stationary, non-flight observation across the
workstation/Pi perception seam. It must:

- use the canonical HIL ownership paths;
- record the relevant workstation and Pi topics;
- perform exactly one PL mapper START+RESET after a 5-second pre-roll;
- observe for 45 seconds after the reset;
- issue no arm, takeoff, mission, maneuver, custom operation, PX4 vehicle
  command, firmware write, or parameter write;
- refuse before runtime startup if another retained operation is planned or
  running; and
- preserve complete, correctly rendered evidence for success, failure, and
  refusal.

The implementation under correction is:

- `tools/simulation/run_hil_perception_seam_probe.sh`
- `scripts/workspace/analyze_hil_perception_seam_bag.py`
- `scripts/workspace/test_analyze_hil_perception_seam_bag.py`

## 3. What happened in the first live attempt

One live attempt was made with the two physical-safety acknowledgements set:

```text
HIL_SEAM_FIXTURE_CONFIRMED=YES
HIL_SEAM_PROP_BATTERY_REMOVED=YES
```

Artifact:

```text
/home/ffn/Workspace/III-Drone-ros2-hil-isolated/runtime/hil_wo010_perception_seam_20260922T083725Z
```

The attempt exited with code 2 and immutable status `REFUSED`:

```text
workstation retained-operation preflight failed
```

The refusal occurred before HIL runtime startup, recorder startup, or mapper
reset. The artifact must remain unchanged.

Manifest evidence currently says:

```json
{
  "run_id": "hil_wo010_perception_seam_20260922T083725Z",
  "status": "REFUSED",
  "status_reason": "workstation retained-operation preflight failed",
  "attempt": 1,
  "max_attempts": 1
}
```

## 4. Confirmed causes

### 4.1 Retained-operation scanner conflated two valid schemas

The wrapper's embedded scanner assumed every directory below
`.iii/operations/` was a universal CLI operation containing `plan.json` and
`state.json`.

That root legitimately also contains configuration reconciliation operations,
whose authority is `reconciliation-journal.json` with optional review and
decision records. Completed reconciliation records therefore caused a false
preflight failure.

The corrected scanner now recognizes and validates both operation families
without writing to either.

### 4.2 Report heredocs execute Markdown backticks

The wrapper writes `REPORT.md` through three unquoted shell heredocs. Markdown
backticks inside those heredocs are interpreted by Bash as command
substitution. This was observed in the refusal artifact:

- `Tag:` is blank;
- the status field is blank;
- the classification field is blank; and
- several backtick-wrapped evidence filenames are absent.

The unsafe report heredocs are still present in the current wrapper, currently
near lines 73, 526, and 1189. This is the unimplemented Batch 3.

Locale warnings seen during the refused attempt were incidental and are not the
cause of either failure.

## 5. Work completed

### Batch 1 — schema-aware, read-only registry scanner

Added:

- `scripts/workspace/hil_operation_registry.py`
- `scripts/workspace/test_hil_operation_registry.py`

The scanner:

- resolves the authoritative CLI and configuration operation roots;
- validates universal CLI plan/state identity and lifecycle state;
- validates configuration reconciliation journal/review/decision identity and
  phase;
- rejects malformed JSON, unsafe entries, symlinks (including broken root
  symlinks), mixed schemas, review-without-journal, decisions-without-review,
  and path-normalization tricks;
- performs no subprocess or filesystem write;
- exits 0 when all records are terminal, 3 when valid nonterminal records are
  present, and 2 when the registry cannot be trusted; and
- emits `hil-operation-registry-scan/v1` JSON.

Parent-verified focused result:

```text
29 tests passed
```

Parent-verified scan of the current workspace registry:

```text
exit: 3
terminal records: 182
nonterminal records: 10
terminal breakdown:
  CLI cancelled: 99
  CLI completed: 61
  CLI failed: 4
  CLI rejected: 4
  reconciliation complete: 14
```

### Batch 2 — wrapper integration

Changed:

- `tools/simulation/run_hil_perception_seam_probe.sh`
- added `scripts/workspace/test_run_hil_perception_seam_probe.py`

The wrapper now:

- calls the Batch-1 helper in the workstation devcontainer;
- streams the same local helper over SSH stdin to the Pi instead of assuming an
  uncommitted source file exists remotely;
- captures separate workstation/Pi JSON and stderr logs;
- accepts only the helper's defined exit codes 0 and 3;
- validates both JSON payloads and verifies exit-code/content consistency;
- writes combined `hil-operation-registry-preflight/v1` evidence before a
  nonterminal refusal; and
- preserves the refusal when either host has a valid planned/running operation.

Luna's validation and the parent's independent rerun both produced:

```text
37 tests passed
bash -n tools/simulation/run_hil_perception_seam_probe.sh: passed
```

No HIL, SSH, Docker, ROS, Pi, runtime, or operation command was run while
implementing these batches.

## 6. Current operational blocker

The false-positive schema failure is corrected, but the workstation registry
currently contains ten genuinely nonterminal universal CLI operations:

```text
hil-dual-agent-boot-plan                         planned
hil-dual-agent-start-plan                        planned
iii-1c5b8946c16c450d901cfbe8                     running
iii-1cfa7035760242fbb3f6ad6a                     running
iii-544fbc27443343cf967e39ec                     running
iii-6d1dd64fd1eb4f7eb89014f3                     running
iii-77da72a901ef4609961763df                     running
iii-7da2267c5e1e48d8b7857521                     running
iii-cfbea8c8756d4bd290eba244                     running
iii-f3f07ab1fd91497295763c37                     running
```

The corrected wrapper is expected to refuse while these records remain
nonterminal. This is intentional fail-closed behavior, not another scanner
bug.

Do not delete, cancel, complete, or rewrite these records automatically. The
next thread must inspect their provenance and ownership read-only and obtain an
explicit retention/lifecycle decision before any mutation. A live Step-1 run
cannot proceed safely until both workstation and Pi scans contain no valid
planned/running operation.

## 7. Work not completed

### Batch 2 parent review

The parent had inspected the new preflight block and independently rerun its
offline tests. No defect had been identified at the interruption point, but the
review was not formally closed. Continue from the current files; do not redo
Batch 1 or Batch 2 from scratch.

### Batch 3 — safe report rendering

This remains entirely outstanding. The planned correction is:

1. Add `scripts/workspace/hil_perception_seam_report.py` as a pure renderer.
2. Add `scripts/workspace/test_hil_perception_seam_report.py`.
3. Pass all dynamic fields as command-line arguments or structured input; do
   not interpolate them into executable shell source.
4. Make the renderer emit literal Markdown backticks and complete initial,
   refusal/failure, and final reports.
5. Replace all three unquoted report heredocs in the wrapper with the renderer.
6. Extend the wrapper source-contract test to prove every report path uses the
   renderer and no unsafe report heredoc remains.
7. Run only offline Python tests, `bash -n`, and non-mutating source checks.

### Final verification

Terra has not reviewed the integrated correction. Per the user's required
workflow, Terra must review only after Batch 3 and parent integration review are
complete. If Terra finds an issue, the parent must write another exact, small
implementation batch for Luna, then send the fully integrated result back to
Terra.

## 8. Required continuation order

1. Read this handoff and inspect the six relevant untracked files. Preserve the
   dirty workspace and immutable refusal artifact.
2. Finish the parent's Batch-2 code review. Do not run a live probe.
3. Parent writes an exact Batch-3 plan with file ownership and literal
   acceptance criteria.
4. Luna implements only Batch 3 and runs only the specified offline checks.
5. Parent reviews the complete integration.
6. Terra performs the final read-only verification of scanner correctness,
   fail-closed behavior, report safety, evidence completeness, and tests.
7. If Terra passes, have Luna run the final offline test command and return its
   exact output for parent interpretation.
8. Separately investigate the ten nonterminal records read-only. Do not mutate
   them without explicit authority.
9. Only after the operation registries are demonstrably clear and the physical
   gates are reconfirmed may one new bounded live attempt be considered.

## 9. Validation commands for the continuation thread

Offline checks only:

```bash
python3 -B -m unittest \
  scripts.workspace.test_hil_operation_registry \
  scripts.workspace.test_run_hil_perception_seam_probe \
  scripts.workspace.test_analyze_hil_perception_seam_bag

bash -n tools/simulation/run_hil_perception_seam_probe.sh
```

Read-only registry evidence:

```bash
WORKSPACE_DIR=/home/ffn/Workspace/III-Drone-ros2-hil-isolated \
  python3 -B scripts/workspace/hil_operation_registry.py
```

Expected current registry result is exit 3 because of the ten records listed
above. Do not reinterpret exit 3 as scanner failure.

## 10. Current relevant file state

All six relevant implementation/test files are currently untracked (`??`), not
committed:

```text
scripts/workspace/analyze_hil_perception_seam_bag.py
scripts/workspace/hil_operation_registry.py
scripts/workspace/test_analyze_hil_perception_seam_bag.py
scripts/workspace/test_hil_operation_registry.py
scripts/workspace/test_run_hil_perception_seam_probe.py
tools/simulation/run_hil_perception_seam_probe.sh
```

SHA-256 at this stop point:

```text
eb076c0f49a3b4ca6c510f27e93fdc1b44d97c673714537d872555d3b6c520af  scripts/workspace/hil_operation_registry.py
b3d6dce1563c44fa9b6298a6685fe5e21d60d0e8896cb96e63fa6323cc0e71b4  scripts/workspace/test_hil_operation_registry.py
060643223cfa780c8d017fd95cf1ddb51802892c94554894f41db225b3bb303b  scripts/workspace/test_run_hil_perception_seam_probe.py
da7b8ec018a972e1c489f3e4b6eb582069e223e7d87dac6261d0b80b933f71ec  scripts/workspace/analyze_hil_perception_seam_bag.py
f8221f982feceab0d1dd95e75c7fa5846168879f7bf279cab27819ceea59407e  scripts/workspace/test_analyze_hil_perception_seam_bag.py
a0f8dfafb55544cde6b84808217bb4ee9202ee36b97b797e16c1724c01bc93ca  tools/simulation/run_hil_perception_seam_probe.sh
```

If any hash differs in the continuation thread, inspect the diff before relying
on this report; another actor may have changed the shared workspace.

## 11. Non-negotiable safety boundaries

- Preserve the dirty checkout and unrelated work.
- Preserve all prior artifacts, especially the refused attempt.
- Do not infer authorization to cancel or rewrite retained operations.
- Do not run the live probe while any planned/running operation remains.
- Do not issue arm, takeoff, mission, maneuver, PX4 vehicle, firmware, or
  parameter commands.
- Only run tests for III packages; the continuation currently needs only the
  offline stdlib suites above.
- Use the user's requested workflow: parent plans, Luna implements small exact
  batches, parent integrates, Terra verifies only the complete result.
