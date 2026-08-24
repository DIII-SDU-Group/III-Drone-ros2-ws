# GUI Operator Testing Backlog

Operator observations collected during interactive GUI testing. Do not
implement until operator says **go**.

## Context

- Current review surface: Flight page (previous Mission/Perception/Dashboard/Payload/Configuration/Rosbags/Logs/global
  observations remain in backlog).
- Each item is rewritten against current code and runtime semantics.

## Incomplete

None.

## In Progress

None.

## Completed

Completed 2026-08-17. Implementation spans shared interaction primitives,
navigation/layout, Mission, Perception, Payload, Flight, Custom operations,
Configuration, Rosbags, Logs, Runtime, runtime adapters/contracts, ROS recorder
interfaces, and configuration manifests. Original task detail and acceptance
criteria remain below as completion record.

Verification:

- Frontend: 121 tests passed; TypeScript passed; production Vite build passed.
- Frontend lint: zero errors; three pre-existing Fast Refresh warnings.
- ROS: six affected III packages built successfully in Jazzy devcontainer.
- ROS tests: 593 tests passed, zero failures/errors/skips.
- Submodule lock verification passed.

### RT-004 - Add per-entity and multi-entity lifecycle controls

**Operator observation**

Each managed entity should have Start/Stop/Restart-style lifecycle controls.
Provide an efficient way to select multiple entities and apply lifecycle actions
to the selection.

**Code understanding**

- Runtime currently lists managed entities with only ID, state, and Logs.
- Existing runtime Start, Stop, Restart, and Shutdown commands already accept a
  `select_nodes` array and `include_dependencies`; daemon and supervision layers
  support targeted and multi-node execution.
- Current global Runtime Mutation buttons omit `select_nodes`, meaning whole
  system. This capability can be reused without inventing parallel entity
  command IDs.
- Entity states are lifecycle labels such as `active`; action availability must
  be calculated per row rather than copied from whole-system boot/active state.

**Required change**

- Extend RT-003 Managed Entities table with a selection checkbox and compact
  row actions for Start, Stop, Restart, and Logs. Use familiar icons with
  tooltips where space is constrained.
- Make row action availability state-aware: suppress redundant transitions,
  reject unknown/impossible states, and combine entity-state reasons with the
  existing runtime mutation safety gate.
- Add a tri-state header checkbox for the current filtered/page scope, plus
  explicit `Select all N entities` and `Clear selection` affordances. Selection
  persists across pagination and refresh only for entity IDs still present.
- When one or more entities are selected, show a compact bulk-action toolbar
  anchored to the table with selected count and Start, Stop, Restart, and
  Shutdown actions. Labels/confirmation state must explicitly state target
  count; never silently reinterpret whole-system controls.
- Keep whole-system Runtime Mutations separate and visibly scoped to the entire
  system. Bulk entity actions use `select_nodes`; empty selection must never be
  sent as a bulk action because an empty array means all nodes to the daemon.
- Provide an `Include dependencies` checkbox for targeted actions, with concise
  scope explanation and a deliberate default. Forward its value unchanged;
  never infer/cascade dependencies invisibly.
- Reuse the page's `Cold restart` checkbox for selected Restart only when the
  selection toolbar clearly reflects that scope, or provide one synchronized
  shared restart option. Do not allow ambiguous global-versus-selected restart
  state.
- Require press-and-hold confirmation for row and bulk mutations. Deduplicate
  requests, disable controls while pending, and show per-entity result/progress
  from daemon response, including partial success/failure.
- After completion, automatically refresh inventory per RT-003. Retain failed
  entity selections for retry; clear successful selections only when doing so
  cannot hide partial failure.
- Apply G-004 tooltips to every disabled row/bulk action. Preserve Logs access
  independent of lifecycle mutation permission.

**Acceptance criteria**

- Every managed entity has correct state-aware Start/Stop/Restart controls and
  Logs access.
- Single-row actions send exactly that entity ID in `select_nodes`.
- Bulk actions send exactly the selected unique IDs; no-selection bulk dispatch
  is impossible.
- Selection works across pages, supports current-page/all/clear semantics, and
  reconciles safely after inventory changes.
- Whole-system controls remain unmistakably whole-system and are never altered
  implicitly by table selection.
- Include-dependencies and cold-restart choices produce exact daemon payloads
  and are visible before confirmation.
- Partial results identify success/failure per entity and remain observable.
- Tests cover entity lifecycle states, permission gates, selection across pages,
  select-all semantics, empty-selection guard, exact payloads, dependencies,
  warm/cold restart, partial failure, pending deduplication, and refresh.

**Likely files**

- `src/III-Drone-GC/frontend/src/pages/RuntimePage.tsx`
- `src/III-Drone-GC/frontend/src/pages/RuntimePage.test.tsx`
- `src/III-Drone-GC/frontend/src/components/interaction.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`
- Runtime contracts/tests only if per-entity progress/result structure needs
  normalization; daemon already supports selected node arrays

### G-004 - Standardize disabled-control reasons as accessible tooltips

**Operator observation**

Everywhere a red text only explains why a button/control is disabled, show that
reason as a tooltip on the inactive control instead of permanent page content.
This explicitly includes Runtime daemon-service controls.

**Code understanding**

- Shared `PressAndHoldButton` and `UrgentActionButton` currently render
  `DisabledReason` in normal flow; several native buttons/selects render nearby
  `.control-reason` paragraphs or expose no reason.
- Existing scoped items cover Mission, global Hold, Flight, Custom operations,
  and Runtime including service lifecycle actions.
- Audit finds additional disabled-control explanations in Configuration,
  Payload, Perception, and Rosbags, plus native refresh/download/select controls.
- Not all red text is a disabled reason. Live subsystem errors, degraded state,
  invalid field values, failed validation, command rejection, and recording
  errors are operational evidence and must remain persistently visible.

**Required change**

- Establish one shared disabled-control wrapper/tooltip primitive usable by
  ordinary buttons, press-and-hold buttons, urgent buttons, selects, inputs, and
  segmented controls.
- A disabled reason is rendered outside document flow and appears on pointer
  hover and keyboard focus of the wrapper. Associate it with the control using
  accessible description semantics.
- Apply globally wherever explanatory red copy is solely caused by a disabled
  control, including Runtime service Start/Stop/Restart.
- Remove duplicate page-level reason paragraphs when the same reason is attached
  to controls. If one reason governs a whole control group, expose it from each
  disabled control without duplicating visible page text.
- Do not convert actual errors/warnings/results into transient tooltips:
  connection/subsystem degradation, recorder errors, invalid input, failed
  validation, rejected commands, and safety alerts remain visible and retain
  alert/status semantics.
- Avoid native `title` as the sole implementation. Disabled native elements need
  a hoverable/focusable wrapper because they do not reliably receive events.
- Tooltips must remain within viewport, layer above fixed footer/panels, support
  wrapped long reasons, and dismiss on pointer exit, blur, Escape, state change,
  and unmount.

**Acceptance criteria**

- No persistent red paragraph exists whose only purpose is explaining an
  inactive control.
- Every disabled interactive control with a known reason exposes it on hover and
  keyboard focus.
- Enabled controls expose no stale tooltip.
- Genuine operational errors, warnings, validation feedback, and command
  results remain persistently visible.
- Runtime daemon-service reasons specifically use tooltips.
- Layout no longer shifts based on disabled-reason length.
- Shared component tests cover pointer, focus, Escape, viewport placement,
  dynamic reason changes, disabled native controls, and screen-reader linkage;
  page tests audit all current call sites.

**Likely files**

- `src/III-Drone-GC/frontend/src/components/interaction.tsx`
- `src/III-Drone-GC/frontend/src/components/interaction.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`
- All page/layout files currently rendering disabled reasons

### RT-003 - Auto-load and paginate managed entities and daemon services

**Operator observation**

Managed Entities and Daemon Services should refresh automatically when entering
Runtime page. Present both as short paginated tables instead of long embedded
lists.

**Code understanding**

- Runtime page currently depends on explicit `Refresh entities` and `Refresh
  services` commands before local result state is populated.
- Managed entities render as an unbounded row list; daemon services render as
  another unbounded list with lifecycle controls.
- Runtime page is conditionally mounted by `AppShell`, so a mount effect maps
  naturally to page entry.

**Required change**

- On Runtime page mount, automatically request runtime status, managed entities,
  and daemon services once the authenticated command/read path is available.
- Avoid duplicate entry requests caused by ordinary state updates or React
  development effect behavior. Cancel/ignore stale responses after unmount.
- Refresh the affected inventory after successful boot/start/stop/restart/
  shutdown and service mutation commands so displayed state converges promptly.
- Keep a compact manual Refresh/Retry action for explicit recovery, preferably
  one per table or one combined inventory refresh; do not require it for initial
  population.
- Render Managed Entities as a semantic table with columns Entity, State, and
  Logs action.
- Render Daemon Services as a semantic table with columns Service, State, and
  lifecycle actions.
- Paginate tables independently after data loading, default 10 rows per page,
  with result range/count and Previous/Next controls. Keep controls compact;
  reset/clamp current page when refreshed row counts change.
- Preserve exact entity-to-logs navigation and service command gating/tooltips
  from RT-001.
- Show loading, empty, stale, and load-failure states distinctly; do not show an
  empty table as a successful refresh while request is pending or failed.

**Acceptance criteria**

- Entering Runtime automatically populates status, entities, and services
  without pressing Refresh.
- Each inventory occupies at most one page of rows plus compact pagination.
- Pagination works independently, including first/last/partial pages and data
  shrink after refresh.
- Service lifecycle changes trigger automatic inventory reconciliation.
- Logs action still opens the correct entity/source where available.
- Failed automatic load exposes a concise retry action and exact error.
- Tests cover mount loading, duplicate-request prevention, unmount handling,
  refresh after mutation, pagination boundaries, loading/empty/error states,
  and responsive table layout.

**Likely files**

- `src/III-Drone-GC/frontend/src/pages/RuntimePage.tsx`
- `src/III-Drone-GC/frontend/src/pages/RuntimePage.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

### RT-002 - Replace Restart mode dropdown with Cold restart checkbox

**Operator observation**

Replace the Runtime `Restart mode` dropdown with a `Cold restart` checkbox.

**Code understanding**

- Runtime stores `restartMode` as `"warm" | "cold"` and sends
  `{ cold: restartMode === "cold" }` with the Restart command.
- This is a binary choice, so a checkbox communicates the behavior more directly
  than a two-option menu.

**Required change**

- Replace Restart mode select with a checkbox labeled `Cold restart`.
- Unchecked means warm restart and remains the default; checked sends
  `{ cold: true }`.
- Disable the checkbox whenever Restart itself is unavailable and expose the
  same reason through RT-001's accessible tooltip pattern.
- Keep the checkbox adjacent to Restart controls and visually subordinate to
  the destructive Restart command.
- Reset/preserve checkbox state deliberately across command completion and page
  navigation; do not silently change the chosen mode during one page session.

**Acceptance criteria**

- No Restart mode dropdown remains.
- Unchecked Restart dispatches `cold: false`; checked dispatches `cold: true`.
- Checkbox is clearly labeled and keyboard/screen-reader operable.
- Disabled checkbox exposes the exact Restart gating reason without persistent
  red text.
- Tests cover default warm, selected cold, disabled state, and dispatch payload.

**Likely files**

- `src/III-Drone-GC/frontend/src/pages/RuntimePage.tsx`
- `src/III-Drone-GC/frontend/src/pages/RuntimePage.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

### RT-001 - Move Runtime disabled reasons into control tooltips

**Operator observation**

Apply the same disabled-reason treatment on Runtime page: remove persistent red
text and show reasons as tooltips on inactive controls.

**Code understanding**

- Apply pending constants and all Runtime Mutation actions use
  `PressAndHoldButton`, which currently renders `disabledReason` below each
  progress bar.
- Managed-entity/service mutation controls use the same component and behavior
  farther down the page.
- Restart mode and several read/refresh controls can also be disabled, but do
  not consistently expose their reason at the control.
- Command failures/successes are delivered through toast results and must remain
  visible; only passive gating explanations move to tooltips.

**Required change**

- Enable the shared accessible disabled-tooltip presentation for Apply pending
  constants; Boot, Start, Stop, Restart, Shutdown; and all managed-service/node
  mutation controls.
- Attach the applicable reason to disabled Restart mode and disabled
  read/refresh controls using the same hoverable/focusable wrapper pattern.
- Remove all in-flow red disabled-state explanations and resulting empty space.
- Preserve visible pending-constant names, restart semantics, command results,
  runtime faults, and service/node failure evidence.
- Reuse M-002/G-001/F-001/O-001 tooltip machinery.

**Acceptance criteria**

- No runtime command gating reason is permanently displayed below a control.
- Every inactive control exposes its complete current reason on mouse hover and
  keyboard focus.
- Enabled controls expose no disabled tooltip.
- Progress bars and buttons remain aligned across the mutation grid regardless
  of reason length.
- Runtime status/fault evidence and command-result notifications remain visible.
- Tests cover already-booted/running, no pending constants, restart-mode gating,
  disconnected reads, managed entity/service restrictions, and accessibility.

**Likely files**

- `src/III-Drone-GC/frontend/src/pages/RuntimePage.tsx`
- `src/III-Drone-GC/frontend/src/pages/RuntimePage.test.tsx`
- `src/III-Drone-GC/frontend/src/components/interaction.tsx`
- `src/III-Drone-GC/frontend/src/components/interaction.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

### O-002 - Rename Operations page to Custom operations

**Operator observation**

Rename the `Operations` page to `Custom operations`.

**Code understanding**

- Navigator and page heading both derive from the `operations` entry in the
  shared `pages` array, currently labeled `Operations`.
- Internal route ID is `operations`; command/domain terminology uses
  `custom_operation`. Neither needs migration for a display-label change.

**Required change**

- Change the user-facing page/navigation label to `Custom operations`.
- Preserve internal `operations` page ID, routes, command IDs, domain keys, and
  Active Operation terminology.
- Verify the longer label fits the navigator at supported widths without
  truncation or layout shift.

**Acceptance criteria**

- Navigator and page title read `Custom operations`.
- Existing navigation, footer Operation status, commands, and deep-link/event
  routing remain functional.
- AppShell tests assert the new display label and unchanged internal routing.

**Likely files**

- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/layout/AppShell.test.tsx`
- Layout CSS only if required for label fit

### O-001 - Move Operations gating reasons into control tooltips

**Operator observation**

Remove persistent red disabled-reason text throughout Operations page. Show it
as tooltips on the relevant inactive controls.

**Code understanding**

- Active Operation renders a red CustomOperation-mode paragraph and
  `UrgentActionButton` renders its Cancel disabled reason in flow.
- Every operation card renders `operationDisabledReason()` as a red paragraph,
  then supplies the same reason to `PressAndHoldButton`, which renders it again.
- Validate is disabled from the same reason but exposes no control-specific
  tooltip.
- Validation results (`Readiness: ...`) and command-result toasts are outcomes,
  not passive gating text, and must remain visible.

**Required change**

- Remove in-flow red gating paragraphs from Active Operation and operation
  cards.
- Show `operationCancelDisabledReason()` through the shared accessible tooltip
  on inactive Cancel operation.
- Show each `operationDisabledReason()` through accessible hover/focus tooltips
  on both inactive Validate and Start operation controls.
- Where a disabled coordinate-frame selector needs the same explanation, attach
  an accessible description/tooltip to its wrapper without duplicating text.
- Consolidate identical reasons within each card; do not render the same text
  twice when moving focus between controls.
- Retain visible field-validation errors, validation/readiness results, runtime
  failures, and command-result notifications because they are not merely
  disabled-state explanations.
- Reuse tooltip machinery from M-002/G-001/F-001.

**Acceptance criteria**

- No operation eligibility/gating reason is permanently rendered in red.
- Hovering or keyboard-focusing any disabled Cancel, Validate, Start, or gated
  frame selector exposes the complete relevant reason.
- Enabled controls show no disabled-reason tooltip.
- Actual validation rejection and command failure remain visibly observable.
- Removing reason paragraphs closes card whitespace and keeps action rows
  aligned across cards.
- Tests cover inactive mode, missing in-flight state, missing perception/
  powerline context, active operation conflict, validation result, and keyboard
  access.

**Likely files**

- `src/III-Drone-GC/frontend/src/pages/OperationsPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/OperationsPage.test.tsx`
- `src/III-Drone-GC/frontend/src/components/interaction.tsx`
- `src/III-Drone-GC/frontend/src/components/interaction.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

### F-001 - Simplify Flight controls and tooltip disabled reasons

**Operator observation**

- Remove the blue `Field arm, takeoff, positioning, takeover...` information
  box; it is unnecessary.
- Show red disabled-command reasons as tooltips on inactive buttons instead of
  permanently below the controls.

**Code understanding**

- Flight renders the static guidance as `.manual-authority-note`, the same class
  scheduled for removal from Mission by M-003.
- Arm, Takeoff, Land, Start Inspection, and custom operation use
  `PressAndHoldButton`; Hold uses `UrgentActionButton`. Both shared components
  currently render disabled reasons in normal flow.
- The in-flow reasons make grid rows expand unevenly and separate controls from
  following content. M-002 and G-001 already require opt-in accessible tooltip
  variants for these two components.

**Required change**

- Remove Flight's static manual-authority paragraph and unused shared CSS once
  Mission's equivalent is also removed.
- Enable tooltip-mode disabled reasons for every Flight command control: Arm,
  Takeoff, Land, Hold, Start Inspection, and Activate custom operation.
- Tooltip must show the complete current result from `flightDisabledReason()` on
  wrapper hover and keyboard focus, without occupying layout space.
- Preserve exact command gating, press-and-hold behavior, urgent Hold behavior,
  progress bars, and source-disagreement diagnostics.
- Share tooltip primitives with M-002/G-001; do not create Flight-only tooltip
  logic.

**Acceptance criteria**

- Blue manual-authority box is absent with no residual gap.
- No disabled reason is permanently visible below Flight buttons.
- Every disabled control exposes its full reason by mouse hover and keyboard
  focus; enabled controls expose no disabled tooltip.
- Button/progress alignment remains stable regardless of reason length.
- Commands cannot be activated while disabled and existing hold durations remain
  unchanged.
- Flight and shared-component tests cover each control type and responsive grid.

**Likely files**

- `src/III-Drone-GC/frontend/src/pages/FlightPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/FlightPage.test.tsx`
- `src/III-Drone-GC/frontend/src/components/interaction.tsx`
- `src/III-Drone-GC/frontend/src/components/interaction.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

### G-003 - Remove Engineering navigator subdivision

**Operator observation**

Remove the Engineering divider. All navigator pages should have equal visual
and structural status.

**Code understanding**

- `AppShell` special-cases the Flight item with a `nav-engineering-start`
  wrapper and inserts a `nav-group-label` reading `Engineering` immediately
  before it.
- The divider has no permissions, routing, or runtime semantics; it only makes
  Flight, Operations, and Runtime appear subordinate/different.

**Required change**

- Remove the conditional Engineering label and special Flight wrapper/class.
- Render every page through the same navigator-item structure and spacing.
- Remove now-unused divider CSS.
- Preserve canonical page order, icons, active state, navigation guard, and
  routing behavior.

**Acceptance criteria**

- No `Engineering` divider appears.
- Every page is presented as an equal peer in one continuous navigator list.
- No extra gap, border, or indentation remains before Flight.
- Keyboard navigation and active-page styling remain consistent across all
  items.
- AppShell tests assert uniform structure and absence of the divider.

**Likely files**

- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/layout/AppShell.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

### L-001 - Wire production Logs page to runtime logs client

**Operator observation**

Logs page is useful, but Follow does not work and reports `Log follow is not
connected to the runtime API.`

**Code understanding**

- `LogsPage` requires an optional `RuntimeLogsClient`; without one it deliberately
  emits the observed Follow-unavailable error.
- Production `App` creates the command dispatcher but never creates a logs
  client. `AppShell` has no logs-client prop and renders `<LogsPage>` without
  one.
- Existing Logs page unit tests inject a mock client, so they do not cover the
  missing production composition wiring.
- `createRuntimeLogsClient()` and runtime REST/WebSocket endpoints already exist;
  runtime follow tests cover initial and appended lines. The primary defect is
  the unwired production dependency, though proxy WebSocket routing must also be
  verified end to end.

**Required change**

- Instantiate `createRuntimeLogsClient(proxyUrl, tokenProvider)` in production
  application composition using the current authenticated session token.
- Pass the client through `AppShell` into `LogsPage`; keep its lifetime stable
  for a token/proxy pair and replace/close follow sessions when either changes.
- Verify GC proxy routes authenticated log source/tail/download requests and the
  `/logs/follow/{source}` WebSocket to runtime correctly.
- Disable Refresh, Follow, and Export with an explicit reason when the client or
  authenticated token is genuinely unavailable, rather than presenting active
  controls that fail after interaction.
- Track WebSocket lifecycle truthfully: show Following only after open; on
  error/close, return to stopped state and provide the exact failure. Stop and
  clean up on source change, page unmount, logout, token replacement, or runtime
  disconnect.
- Append incoming lines with the existing bounded history and auto-scroll only
  when the operator is already at the bottom; do not pull the viewport away
  while inspecting older lines.

**Acceptance criteria**

- In the production GUI, Follow connects and streams newly appended lines for
  daemon, runtime API, and aggregate sources.
- Refresh sources/history and Export current view use the same authenticated
  client and work in production composition.
- Follow/Stop label reflects actual WebSocket state, including connection error
  and remote close.
- Switching source stops the old stream and starts no duplicate stream.
- Logout, disconnect, navigation/unmount, and token rotation close sockets.
- App-level integration test catches omission of the logs client; proxy/runtime
  integration test verifies authenticated REST and WebSocket flow.

**Likely files**

- `src/III-Drone-GC/frontend/src/App.tsx`
- `src/III-Drone-GC/frontend/src/App.commandWiring.test.tsx`
- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/layout/AppShell.test.tsx`
- `src/III-Drone-GC/frontend/src/pages/LogsPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/LogsPage.test.tsx`
- `src/III-Drone-GC/frontend/src/api/logs.ts`
- GC proxy routing/tests as required

### R-002 - Configure one rosbag root and simplify manual recording form

**Operator observation**

- Operator must not choose an output directory per recording. The system uses
  one configured output directory.
- Show configured path, editable recording ID, and any system-generated name
  portion together as one final-path composition.
- Replace free-text Topics input with a dropdown and make it visibly inactive
  while All topics is enabled.

**Code understanding**

- Recorder node already declares `artifact_root` with default
  `/tmp/iii_drone/rosbags`; empty service `output_dir` resolves to
  `artifact_root / recording_id`.
- `artifact_root` is not currently represented in the configuration manifest,
  while GUI/runtime still expose an arbitrary per-start `output_dir` override.
- Recorder naming currently uses a sanitized supplied ID unchanged. A blank ID
  becomes the misleading hardcoded `reach_cable_<timestamp>` regardless of
  recording owner.
- GUI accepts comma-separated topic text. Runtime forwards it without providing
  discoverable topic options.

**Required change**

- Add recorder artifact root as a first-class constant configuration parameter
  for `/mission/rosbag_recorder`, with current root as default. Persist through
  configuration server/snapshots and require the established constant-parameter
  cold restart before active recorder root changes.
- Make configured artifact root the sole storage root for manual and automatic
  recordings. Remove per-recording Output directory from GUI and operator
  command contract; retain compatibility only internally if required by ROS
  interface migration, but reject/ignore external path overrides safely.
- Ensure runtime listing, download path validation, free-space probe, recorder
  startup, and cleanup all resolve against the same active configured root.
- Define one owner-neutral recording-name policy. Remove hardcoded
  `reach_cable_` fallback; use a clear generated timestamp component and
  sanitized optional operator ID. Avoid collisions deterministically.
- Render a single path-composer row such as configured-root prefix + Recording
  ID field + generated suffix. Static prefix/suffix are visually noneditable;
  the composed preview must match the actual final recorder path/name. Show
  sanitization changes before submission rather than silently changing input.
- Expose available ROS topics and types from runtime/recorder state or a focused
  read endpoint. Replace comma-separated input with a searchable multi-select
  dropdown.
- Keep All topics as a binary option. When enabled, selected-topics dropdown is
  disabled, dimmed, non-focusable for editing, and clearly says all topics will
  be recorded. When disabled, require at least one selected topic.
- Keep Hidden topics independently meaningful for All topics mode; for selected
  topics, clarify/filter hidden topic discovery consistently with recorder
  semantics.
- Preserve selected topics when toggling All topics on and back off during the
  current form session.

**Acceptance criteria**

- Manual Recording has no editable output-directory field.
- Active configured root is visible, exact, and sourced from configuration
  state; unavailable configuration blocks recording rather than guessing.
- Final-path preview and resulting `recording_id`/`output_dir` agree, including
  generated timestamp/suffix and sanitization.
- Manual and mission-owned recordings share the configured root and cannot
  escape it.
- Changing root follows constant-parameter persistence/restart rules and works
  after cold restart.
- Topics are selected from discoverable runtime topics through an accessible
  searchable multi-select.
- All topics visibly disables topic selection; selected-topic mode rejects an
  empty selection.
- Tests cover configuration persistence/restart, path containment, name
  generation/collision, preview agreement, topic discovery, dropdown behavior,
  All topics toggling, hidden topics, and backend command validation.

**Likely files**

- `src/III-Drone-Configuration/config/parameters/parameter_manifest.yaml`
- Tracked real/sim parameter sets and configuration tests
- `src/III-Drone-Mission/src/mission/rosbag_recorder_node/rosbag_recorder_node.cpp`
- `src/III-Drone-Mission/include/iii_drone_mission/mission/rosbag_recorder_node/rosbag_recorder_node.hpp`
- `src/III-Drone-Mission` recorder tests
- `src/III-Drone-Runtime/iii_drone_runtime/api/rosbag.py`
- Runtime contracts/API and tests
- `src/III-Drone-GC/frontend/src/pages/RosbagsPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/RosbagsPage.test.tsx`
- Generated frontend contracts and styles

### R-001 - Format recording storage with adaptive units

**Operator observation**

Free space should use an appropriate human-readable unit instead of always
forcing values at or above one MiB to remain in MiB.

**Code understanding**

- Rosbags page `formatBytes()` supports B, KiB, and MiB only. The displayed
  `107690.3 MiB` is approximately `105.2 GiB`.
- The bottom status bar has a separate adaptive formatter using decimal KB/MB/
  GB/TB, creating inconsistent units for the same rosbag storage value.
- Recording size and recordings-list size use the Rosbags formatter too and
  should follow the same scale rules.

**Required change**

- Use one shared adaptive byte formatter for rosbag size/free-space displays.
- Use IEC binary units consistently: B, KiB, MiB, GiB, TiB, selecting the
  largest suitable unit without exceeding the value.
- Apply consistent precision: integer bytes, one decimal place for scaled
  values, and stable behavior at exact unit boundaries.
- Use the same formatter on Recorder State, recordings list, and bottom status
  summary; preserve context-specific unknown wording where needed.

**Acceptance criteria**

- Example free space renders approximately `105.2 GiB`, not `107690.3 MiB`.
- Values transition correctly at 1024-byte boundaries through TiB.
- Zero, null/unknown, negative-invalid, and very large inputs are handled
  explicitly without misleading units.
- Recorder State, recordings list, and status bar agree for identical byte
  values.
- Unit-boundary tests cover every supported scale.

**Likely files**

- `src/III-Drone-GC/frontend/src/pages/RosbagsPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/RosbagsPage.test.tsx`
- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/layout/AppShell.test.tsx`
- Shared frontend formatting utility and tests

### C-002 - Replace embedded parameter cards with searchable scrolling table

**Operator observation**

Parameter Manifest should be an actual table with search and in-table scrolling,
not a long in-page node/group/card expansion. Show more metadata for each parameter,
including whether it is constant and its categories where applicable.

**Code understanding**

- Current browser filters nested nodes, then renders every matching node, group,
  and parameter as vertically expanded sections/articles.
- `ParameterDefinition` already supplies node/group IDs, name, description, type,
  active/current/persisted/loaded/default values, constraints, restart policy,
  readonly, constant, apply permissions/rejections, and reference.
- Node and group labels are available in the surrounding manifest hierarchy but
  are not copied onto each parameter; table rows must be flattened with that
  context.
- Staged edits are keyed by parameter identity and persist independently of
  search/filter visibility.

**Required change**

- Flatten the manifest into semantic table rows and render a real HTML table
  with a sticky header where practical.
- Primary columns: Parameter, Node, Group/category, Type, Active, Persisted,
  Edit value, Flags/policy, Actions.
- Display `constant`, `runtime-editable`, `readonly`, restart scope, pending,
  unsaved, non-default, and apply-blocked state as concise noninteractive status
  indicators. Never imply constant values are immediately active after apply.
- Make description, default/loaded values, units, min/max/step or expressions,
  choices, regex, reference, and full rejection detail available through an
  expandable row-detail area so the main table remains scannable.
- Search case-insensitively across parameter name, description, node/group IDs
  and labels, type, flags, reference, and choice values.
- Add explicit Node, Group/category, and mutability filters where useful; search
  and filters combine.
- Render every filtered row in one bounded, keyboard-focusable table viewport
  with vertical and horizontal in-table scrolling, sticky headers, and a visible
  filtered/total result count. Do not paginate.
- Preserve staged edits, validation state, and selected values when rows leave
  search/filter visibility.
- Keep Apply/Reset per row and the existing Staged Edits workflow. Disabled
  actions expose their exact reasons without expanding row height by default.
- Provide responsive behavior: preserve table semantics on desktop; use
  horizontal scrolling or a deliberate compact-row treatment on narrow screens,
  never overlapping or silently dropping operational fields.

**Acceptance criteria**

- Manifest no longer renders as an unbounded sequence of parameter cards.
- Table headers align with data and support efficient scanning.
- Node and group/category are visible for every row.
- Constant and runtime-editable parameters are unmistakably differentiated.
- Search covers all specified metadata and updates filtered/total count correctly.
- All filtered rows remain in one bounded scrolling table; no page-size or
  previous/next controls remain.
- Edits survive scrolling, searching, filtering, and row-detail toggling.
- Apply/Reset, validation, persistence, restart notices, and configuration-server
  gating retain current behavior.
- Tests cover flattening, metadata, search, filters, scrolling structure,
  staged-edit retention, accessibility, and responsive table structure.

**Likely files**

- `src/III-Drone-GC/frontend/src/pages/ConfigurationPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/ConfigurationPage.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`
- Optional focused table component/helper files under
  `src/III-Drone-GC/frontend/src/components/`

### C-001 - Wrap long Configuration Status values without overlap

**Operator observation**

Long Loaded snapshot, Default snapshot, and related Configuration Status values
must wrap instead of overlapping adjacent columns.

**Code understanding**

- Configuration Status uses the shared five-column `.status-list` layout.
- Grid tracks correctly use `minmax(0, 1fr)`, but `.status-list dd` has no word
  breaking/overflow rule.
- Snapshot identifiers and paths contain long slash/underscore-delimited tokens,
  so their intrinsic text width spills into following status cells.

**Required change**

- Add a Configuration Status-specific class and allow long values to wrap within
  their own grid track using appropriate overflow wrapping.
- Prefer readable breaks at path separators where browser wrapping permits;
  guarantee unbroken identifiers still cannot escape their cell.
- Let row height expand naturally and keep each following row below the tallest
  value in the preceding row.
- Preserve full values; do not truncate data needed to identify the active or
  default snapshot.

**Acceptance criteria**

- Long loaded/default snapshot values remain entirely inside their cells.
- No text overlaps Configuration server, Pending edits, or other columns.
- Full values remain visible and selectable.
- Layout remains readable at supported desktop/mobile widths.
- Other shared status grids remain visually unchanged.
- Configuration-page tests include long path-like and unbroken snapshot IDs.

**Likely files**

- `src/III-Drone-GC/frontend/src/pages/ConfigurationPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/ConfigurationPage.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

### PL-001 - Make gripper controls reflect current gripper state

**Operator observation**

Open/Close gripper controls should be stateful based on current gripper state.

**Code understanding**

- Payload status exposes normalized `gripper_status` values `open`, `closed`, or
  `unknown`.
- Current Open and Close buttons share only the page-level permission reason;
  both appear actionable regardless of physical state.
- Runtime applies successful command results to its payload-state cache, so the
  control can update from the authoritative streamed state without maintaining
  a second persistent frontend state.

**Required change**

- Present Open/Closed as one binary segmented gripper-state control.
- Visually mark the segment matching fresh `gripper_status` as selected/current
  and disable its redundant no-op command; leave the opposite transition
  actionable when payload permissions allow it.
- For `unknown`, select neither state and preserve explicit unknown-state
  presentation. Do not falsely claim either physical state.
- While a gripper command is pending, expose the requested transition, prevent
  duplicate/conflicting dispatch, then reconcile from streamed runtime state.
- Global command restrictions from mission, custom operation, connection, and
  runtime permission remain authoritative and disable both transitions with
  their existing reason.

**Acceptance criteria**

- `open`: Open is visibly current/non-actionable; Close is actionable.
- `closed`: Closed is visibly current/non-actionable; Open is actionable.
- `unknown`: neither is shown as current; UI does not invent state.
- Only one command is dispatched per interaction; pending/rejected commands do
  not leave a false selected state.
- Mouse, keyboard, selected-state, disabled-state, and accessible semantics are
  tested.

**Likely files**

- `src/III-Drone-GC/frontend/src/pages/PayloadPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/PayloadPage.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

### G-002 - Restyle page-header state as noninteractive context

**Operator observation**

The Payload header's top-right `open` status looks like a button. Use a clearly
noninteractive layout there and everywhere using the same page-header status
pattern.

**Code understanding**

- `AppShell` renders every page's `pageModeLabel()` result in one `.page-mode`
  span.
- `.page-mode` uses a border, white background, padding, and 8px radius, closely
  matching button styling.
- Payload returns the raw gripper state (`open`); other pages use the same visual
  for mission phase, health, runtime state, flight mode, recording state, etc.
- M-001 separately removes this element entirely from Mission because its value
  is ambiguous there.

**Required change**

- Replace the bordered badge appearance with a noninteractive header-context
  pattern across all remaining pages: plain aligned text plus a small semantic
  status indicator, with no button-like border, fill, or raised shape.
- Add a concise context label where a raw value is ambiguous. For Payload, show
  `Gripper: open` rather than bare `open`.
- Preserve status-dependent visual differentiation using text/icon/color that
  does not rely on color alone, and maintain accessible text.
- Keep M-001's Mission-specific omission; do not restore the Mission badge.
- Ensure long multi-part states wrap or truncate accessibly without colliding
  with the page title at desktop/mobile widths.

**Acceptance criteria**

- Payload header state is immediately distinguishable from a clickable control.
- The same is true for every page using shared header context.
- No pointer/hover/focus styling implies interactivity.
- Payload reads `Gripper: open`, `Gripper: closed`, or `Gripper: unknown`.
- Disconnected/degraded/active states remain discernible and screen-reader
  accessible.
- Shared AppShell visual and responsive tests cover representative page states.

**Likely files**

- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/layout/AppShell.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

### V-001 - Use the Dashboard MapView layout as the canonical embedded map

**Operator observation**

The Dashboard map layout is preferred over the separate Live/Stored projection
currently shown elsewhere. Use this layout by default in other embedded map
locations.

**Code understanding**

- Dashboard uses the shared `MapView` in compact `powerline_orthogonal` mode.
  It overlays stored overview, live perception, aircraft, target, and other
  enabled layers in one scaled plot with grid, legend, source, freshness, and
  transport age.
- Perception uses a separate local `ProjectionView` implementation with two
  side-by-side Live and Stored plots. It duplicates projection/bounds/rendering
  logic and prevents direct spatial comparison of live versus stored geometry.
- Mission already uses shared `MapView`, but forces `top_down` rather than the
  preferred powerline-orthogonal default.
- Full Map page already defaults to `powerline_orthogonal` and appropriately
  retains explicit Powerline, Top-down, and Side-by-side controls.

**Required change**

- Make shared `MapView` the canonical visualization component for Dashboard,
  Perception, Mission, and Map page.
- Replace Perception's custom side-by-side `ProjectionView` with one combined
  powerline-orthogonal `MapView`, overlaying Live and Stored layers with clearly
  distinct legend entries and freshness treatment.
- Use compact powerline-orthogonal `MapView` as Mission's default embedded map.
- Keep full Map page projection and layer controls; it remains the place for
  top-down and side-by-side investigation.
- Preserve page-appropriate sizing while keeping the Dashboard visual language:
  framed plot, grid, compact legend, markers, source footer, and freshness/age.
- Delete obsolete local projection helpers/styles after migration. Fold P-002
  axis labels and metric ticks into shared `MapView` so every
  powerline-orthogonal instance receives them consistently.

**Acceptance criteria**

- Dashboard, Perception, and Mission use the same map renderer and default
  powerline-orthogonal visual structure.
- Perception shows Live and Stored geometry together, not as separate charts;
  either layer remains identifiable when both overlap.
- Mission no longer defaults its embedded context map to top-down.
- Full Map page still permits Powerline, Top-down, and Side-by-side modes plus
  layer selection.
- Axis labels/ticks, grid, legend, aircraft marker, source, stale state, and age
  are consistent across embedded powerline views.
- Tests cover layer coexistence, defaults at all call sites, missing/stale
  geometry, and removal of custom `ProjectionView`.

**Likely files**

- `src/III-Drone-GC/frontend/src/components/MapView.tsx`
- `src/III-Drone-GC/frontend/src/components/MapView.test.tsx`
- `src/III-Drone-GC/frontend/src/pages/Dashboard.tsx`
- `src/III-Drone-GC/frontend/src/pages/PerceptionPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/PerceptionPage.test.tsx`
- `src/III-Drone-GC/frontend/src/pages/MissionPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/MissionPage.test.tsx`
- `src/III-Drone-GC/frontend/src/pages/MapPage.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

### D-001 - Make Dashboard the default and first navigation destination

**Operator observation**

Dashboard should be the default page and appear at the top of the navigator.

**Code understanding**

- The `pages` array currently lists Mission first and Dashboard second; this
  array controls navigator ordering and the fallback `pages[0]` selection.
- `AppShell` independently initializes `activePage` to `"mission"`.
- There is no URL route or restored-page mechanism currently overriding that
  initial state.

**Required change**

- Put Dashboard first in the canonical `pages` navigation array.
- Initialize `activePage` to `"dashboard"`.
- Keep Mission directly below Dashboard; preserve remaining page order unless
  later navigation feedback changes it.
- Ensure fallback behavior also resolves to Dashboard if an invalid page state
  is ever encountered.

**Acceptance criteria**

- A fresh GUI load opens Dashboard.
- Dashboard is the first/top navigator item and Mission is second.
- Direct navigation from footer status items, events, and command results still
  reaches the correct page.
- AppShell tests assert initial content, active navigation state, and ordering.

**Likely files**

- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/layout/AppShell.test.tsx`

### M-007 - Reduce Mission page to mission overview and active control

**Operator observation**

Mission page is too long. It should present mission overview and mission actions;
detailed subsystem information belongs on each subsystem's page.

**Code understanding**

- `MissionPage` directly renders the complete `<PerceptionPage />`. This
  duplicates Perception Status, pylon capture, stored overview, mapper controls,
  overview update, projections, and diagnostics already available through the
  dedicated Perception navigation entry.
- `Battery And Charging` combines PX4 battery telemetry already suited to
  Flight, charger data suited to Payload, and thresholds defined in
  Configuration.
- `Aircraft And Corridor` embeds a compact map while Map already provides full
  projection, layer, and geometry controls. A compact mission-context map is
  still useful if limited to current path/aircraft context.
- `Active Operational Parameters` duplicates Configuration and links onward to
  Runtime.
- `Installed Inspection` exposes content hash, profile, registration, and
  freshness details that are primarily configuration/runtime diagnostics; only
  specification identity and validity are mission-overview information.
- Preparation renders both a compact checklist and every detailed onboard
  preflight item, duplicating the same readiness decision in two grids.
- `Stop And Recovery` mixes useful live safety state with a static five-item
  reference table. Detailed fault evidence already belongs in Flight, Payload,
  Perception, Rosbags, Runtime, and Logs.
- Mission Progress and Mission Intent are mission-specific and have no better
  destination.

**Required change**

- Remove embedded `<PerceptionPage />` from Mission. Keep all mapper, capture,
  overview, projection, and perception-diagnostic controls on Perception.
- Move the perception-review acknowledgment action to Perception. Mission may
  show its current acknowledged/not-acknowledged summary, but must not duplicate
  the control workflow.
- Keep Mission's command band, compact Preparation/readiness summary, Mission
  Progress, active Mission Intent controls/history, and current live safety
  summary/operator action.
- Replace the full detailed preflight grid with a concise readiness summary:
  show blocking/advisory exceptions when present, not every successful
  subsystem check. Provide direct navigation to the relevant subsystem page
  for detail where mapping is known.
- Reduce Battery And Charging to mission-relevant summary only: remaining
  charge, recharge threshold/state, and charging/latched state needed to
  understand mission phase. Leave voltage/current/power diagnostics on Flight
  or Payload and parameter source/debounce/configuration on Configuration.
- Reduce Installed Inspection to specification identity plus verified/not
  verified state. Move/remove content hash, configuration profile, mode
  registration, and freshness from Mission; expose them through Runtime or
  Configuration if not already present there.
- Keep a compact, non-interactive Aircraft And Corridor map only when it helps
  interpret active mission position, target, and trajectory. Full map controls
  and geometry diagnostics stay on Map.
- Remove Active Operational Parameters from Mission; Configuration is the
  authoritative surface.
- Remove the static Stop And Recovery reference table. Retain only live safety
  status, current summary, and current operator action, with links to relevant
  diagnostics.
- Add concise navigation actions from Mission summaries/errors to Perception,
  Map, Flight, Payload, Rosbags, Configuration, Runtime, or Logs as applicable.
  Do not duplicate those pages' controls inside Mission.
- Preserve a single Mission-owned toast region and command dispatch path after
  removing nested Perception page composition.

**Target Mission page order**

1. Current aircraft/mission phase and Start Inspection controls.
2. Compact Preparation/readiness, emphasizing unmet conditions.
3. Active mission progress and mission intent controls.
4. Compact battery/recharge and live safety summaries.
5. Compact mission-context map.
6. Minimal installed-specification identity/status.

**Acceptance criteria**

- Mission no longer embeds or duplicates Perception page.
- Mission contains no parameter editor/readout, detailed perception workflow,
  full preflight evidence matrix, or static recovery reference table.
- All removed operational capability remains reachable on an appropriate
  dedicated page.
- Mission still answers: Can inspection start? What phase owns the aircraft?
  What is it doing/targeting? Is recharge involved? Is operator action needed?
- Active mission intents remain immediately operable.
- Mission page is materially shorter at desktop and mobile widths, with no
  nested full-page component or duplicate toast region.
- Navigation and page tests verify retained overview content and dedicated-page
  ownership of moved details.

**Likely files**

- `src/III-Drone-GC/frontend/src/pages/MissionPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/MissionPage.test.tsx`
- `src/III-Drone-GC/frontend/src/pages/PerceptionPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/PerceptionPage.test.tsx`
- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

### G-001 - Show disabled global Hold reason as a tooltip

**Operator observation**

The bottom status bar permanently displays `hold requires the vehicle to be in
flight`. Show this reason only when hovering the inactive Hold button.

**Code understanding**

- `StatusBar` supplies `flightDisabledReason(state, "px4.hold")` to the shared
  `UrgentActionButton`.
- `UrgentActionButton` renders `DisabledReason` in normal document flow, causing
  the persistent red text above the footer action.
- A native disabled button cannot reliably own hover/focus interaction. The
  tooltip needs a hoverable, keyboard-focusable wrapper, matching the accessible
  disabled-tooltip pattern planned in M-002.

**Required change**

- Add an opt-in tooltip presentation for `UrgentActionButton.disabledReason`
  and use it for global Hold in the bottom status bar.
- Show the exact reason on wrapper hover and keyboard focus; link it as an
  accessible description of Hold.
- Remove the in-flow reason from the footer and ensure tooltip placement remains
  visible above the viewport-bottom bar.
- Reuse shared tooltip primitives/styles introduced for M-002 rather than
  creating a second interaction pattern.

**Acceptance criteria**

- Disabled Hold shows no permanently visible reason.
- Hovering or keyboard-focusing its wrapper reveals the complete reason.
- Enabled Hold has no disabled tooltip and retains current urgent-action
  behavior.
- Tooltip does not clip against viewport edges or obscure the status bar.
- Other `UrgentActionButton` consumers retain existing behavior unless they
  explicitly opt into tooltip presentation.
- Component and AppShell tests cover mouse, keyboard, enabled, and disabled
  states.

**Likely files**

- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/layout/AppShell.test.tsx`
- `src/III-Drone-GC/frontend/src/components/interaction.tsx`
- `src/III-Drone-GC/frontend/src/components/interaction.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

### P-002 - Label orthogonal-projection axes and show metric scale

**Operator observation**

Live and Stored orthogonal-projection charts need axis labels and units.

**Code understanding**

- `projectedPlanePoints()` calculates `u` as lateral displacement in the
  projection plane and `v` as vertical displacement from the projection-plane
  origin.
- Both values originate from world-coordinate positions and are measured in
  metres.
- The SVG currently draws bare axes and rescales data to computed bounds, but
  exposes neither axis meaning nor numeric scale.

**Required change**

- Label the horizontal axis `Lateral offset (m)` and vertical axis `Vertical
  offset (m)` on both Live and Stored projections.
- Add readable numeric tick labels in metres based on the same bounds used to
  position points.
- Reserve SVG margins for labels/ticks so they do not overlap axes, conductor
  markers, IDs, or panel edges.
- Use identical axis semantics and tick formatting for Live and Stored views;
  retain independent bounds unless later requirements request shared scaling.
- Include axis meaning and units in accessible SVG description.

**Acceptance criteria**

- Both projections visibly communicate lateral and vertical metric offsets.
- Tick values accurately correspond to rendered point positions and include
  negative/positive ranges when present.
- Labels remain legible at supported desktop/mobile sizes and with no geometry.
- Point labels and status overlays remain unobstructed.
- Tests verify axis labels, units, and representative tick/point mapping.

**Likely files**

- `src/III-Drone-GC/frontend/src/pages/PerceptionPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/PerceptionPage.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

### P-001 - Remove duplicate powerline-overview capture rejection

**Operator observation**

`Update Powerline Overview` displays the same red capture-readiness detail
twice. Retain only one copy.

**Code understanding**

- `PressAndHoldButton` renders its supplied `disabledReason` beneath the hold
  progress bar.
- `PerceptionPage` then renders `captureRejections` again in a separate
  `.control-reason` paragraph below the action row.
- The duplicate shown is therefore frontend rendering, not two distinct
  onboard errors.

**Required change**

- Render failed capture-readiness detail once, adjacent to the disabled Store
  action.
- Preserve the ready-state hint unless separately removed by later feedback.
- Do not hide a distinct page-level command permission failure if it differs
  from live capture-readiness rejection; consolidate multiple distinct reasons
  into one readable location without duplicate text.

**Acceptance criteria**

- Each unique Store-overview blocking reason appears exactly once.
- The disabled button remains visibly associated with its reason.
- Ready-state guidance and hold-progress behavior remain unchanged.
- Tests cover capture rejection, command permission rejection, and ready state.

**Likely files**

- `src/III-Drone-GC/frontend/src/pages/PerceptionPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/PerceptionPage.test.tsx`

### M-006 - Improve Battery And Charging row separation and remove explanatory copy

**Operator observation**

- The second metric row begins too close to the first row's values, making the
  row boundary difficult to scan.
- Remove `Automatic recharge and critical-battery policy remain onboard. The
  GUI can request an earlier recharge but cannot bypass thresholds.`

**Code understanding**

- Battery And Charging uses the shared `.status-list` five-column grid.
- `.status-list` currently applies one `10px` `gap` to both columns and rows.
  Changing it globally would alter unrelated Mission-page status grids.
- The explanatory sentence is a static `.control-hint` immediately following
  the battery status list; it contains no live state or command affordance.

**Required change**

- Add a Battery-specific status-list class and increase its row gap while
  retaining the existing column spacing and five-column desktop layout.
- Preserve responsive three-column behavior and ensure wrapped values cannot
  collide visually with the next row.
- Remove the static recharge-policy hint and any resulting excess bottom space.

**Acceptance criteria**

- Clear vertical whitespace separates each visual row of battery metrics.
- Column alignment and existing responsive wrapping remain intact.
- Other `.status-list` consumers are visually unchanged.
- The static recharge-policy sentence is absent.
- Mission-page tests assert the scoped class/structure and removed copy.

**Likely files**

- `src/III-Drone-GC/frontend/src/pages/MissionPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/MissionPage.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

### M-005 - Make mapper and recording mission-owned initialization, not preflight state

**Operator observation**

- PL mapper running is not a preflight requirement; the mission controls it.
- Inspection recording already being active is not a preflight requirement; the
  mission controls it.
- Both the compact Preparation area and detailed onboard-preflight boxes must
  reflect this ownership correctly.

**Code understanding**

- The compact Preparation section currently renders `Mapper running` and
  `Inspection recording active` as checks.
- `inspectionStartDisabledReason()` additionally disables Start Inspection when
  `rosbag.recording` is false.
- Runtime preflight currently publishes `Inspection recording` as a hard gate,
  so the detailed onboard-preflight grid also reports it as blocked.
- Mission activation already calls `runtime_rosbag.ensure_inspection_recording()`
  before evaluating activation preconditions. Recording is therefore an
  activation-owned transition, not state the operator must establish first.
- The inspection/recharge behavior trees own PL-mapper lifecycle transitions.
  Runtime preflight's broader `Perception services` check tests service
  availability and does not require the mapper to be running.
- `Recording storage` is distinct from active recording and remains legitimate
  readiness evidence for automatic recording.

**Required change**

- Remove `Mapper running` and `Inspection recording active` from the compact
  Preparation readiness checklist. Show live mapper/recording state only in an
  observability/status surface where it is not styled or worded as preflight.
- Remove the frontend active-recording condition from
  `inspectionStartDisabledReason()`.
- Remove `Inspection recording` as a runtime preflight item/hard gate, so it no
  longer appears blocked in the detailed onboard-preflight grid.
- Retain `Perception services` and `Recording storage` as capability/readiness
  checks. Do not reinterpret either as requiring an active mapper or recorder.
- Keep recording startup in the mission-activation transaction. Failure to
  start/confirm recording may reject that activation, but inactive recording
  before the command must not reject or disable the command.
- Keep PL-mapper start/pause/freeze/stop under mission behavior-tree ownership;
  do not start it from GUI preflight logic.
- Preserve recording finalization/cleanup if activation fails after automatic
  startup or when mission ownership ends.

**Acceptance criteria**

- With mapper stopped and recorder inactive, Start Inspection can be enabled
  when all genuine flight, geometry, service, and storage gates pass.
- Neither the Preparation summary nor detailed preflight grid presents mapper
  running or inspection recording active as a prerequisite.
- Starting inspection automatically starts/confirms recording before mission
  execution proceeds.
- Mission behavior controls mapper lifecycle without GUI intervention.
- Missing perception services, inadequate recording storage, or automatic
  recorder-start failure remain explicit, actionable activation failures.
- Tests cover frontend gating/rendering, runtime preflight contents, automatic
  recorder startup, failure cleanup, and stopped-mapper mission activation.

**Likely files**

- `src/III-Drone-GC/frontend/src/pages/MissionPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/MissionPage.test.tsx`
- `src/III-Drone-Runtime/iii_drone_runtime/api/app.py`
- `src/III-Drone-Runtime/test/test_api.py`
- Relevant runtime mission/preflight and rosbag tests

### M-004 - Make Preparation checklist labels truthfully reflect current state

**Operator observation**

Checklist labels are written as successful assertions even when checks fail.
Examples: `Mission ready` while mission is not ready and `Inspection recording
active` while recording is inactive. Existing detail text is useful and should
remain.

**Code understanding**

- `Check` receives one static `label` plus boolean `ok`; only marker/color
  changes between pass and fail.
- Missing/unknown values currently collapse to `false` for most checks.
- `Live perception approved` is a subjective browser-local acknowledgment. The
  button stores a timestamp under
  `iii-drone:inspection:perception-approved-at` in `localStorage`.
- This acknowledgment is not sent onboard, is not used by
  `inspectionStartDisabledReason()`, and cannot override onboard start
  eligibility or preflight. Its intent is only to record that operator visually
  reviewed live perception output.

**Required change**

- Give every Preparation check state-dependent primary text while retaining
  existing explanatory detail.
- Use explicit negative text for known failures/inactive states and `unknown`
  text where source state is unavailable rather than claiming success.
- Use accurate acknowledgment wording for subjective perception review; do not
  imply onboard approval or a hard mission gate.

**Expected label pairs/states**

- `Aircraft system ready` / `Aircraft system not ready` / `Aircraft system state unknown`
- `Perception review acknowledged` / `Perception review not acknowledged`
- `Powerline overview stored` / `Powerline overview not stored` / `Powerline overview state unknown`
- `Endpoint 1 captured` / `Endpoint 1 not captured`
- `Endpoint 2 captured` / `Endpoint 2 not captured`
- `Corridor geometry valid` / `Corridor geometry invalid` / `Corridor geometry state unknown`
- `Eligible inspection start position` / `Ineligible inspection start position` / `Inspection start position unknown`
- `Mission ready` / `Mission not ready` / `Mission readiness unknown`

**Acceptance criteria**

- No failed/unknown checklist row presents a successful assertion.
- Passed checks retain concise affirmative labels.
- Existing detail/rejection strings remain visible unchanged below labels.
- Acknowledgment action still stores/clears only its browser-local timestamp and
  never changes onboard eligibility.
- Tests cover pass, fail, and unknown labels plus acknowledgment persistence and
  non-gating behavior.

**Likely files**

- `src/III-Drone-GC/frontend/src/pages/MissionPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/MissionPage.test.tsx`

### M-003 - Remove static manual-authority information box

**Operator observation**

The blue manual arm/takeoff/positioning/landing information box is overly
informative and should be removed.

**Code understanding**

- `MissionPage` renders this as a static `.manual-authority-note` paragraph.
- It contains no live state, warning, command, or conditional safety evidence.
- Manual authority remains implicit in available GUI controls and is documented
  in the field operating procedure; persistent Mission-page space is not needed.

**Required change**

- Remove the manual-authority paragraph from Mission page.
- Remove `.manual-authority-note` CSS if no other consumer exists.
- Do not remove operational safety warnings or disabled-command reasons.

**Acceptance criteria**

- Blue manual-authority box is absent from Mission page.
- Command band flows directly into next live workflow content without excess
  gap or separator artifact.
- Field-operation documentation remains unchanged.
- Mission-page test asserts static copy is absent.

**Likely files**

- `src/III-Drone-GC/frontend/src/pages/MissionPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/MissionPage.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

### M-002 - Normalize Mission command buttons and move disabled reasons to tooltips

**Operator observation**

- `Aircraft system ready` does not read as a command button.
- Red disabled-reason paragraphs below both command buttons consume space and
  should appear on hover instead.
- Buttons and hold-progress bars must have identical dimensions and alignment.

**Code understanding**

- First button currently changes label from `Start aircraft system` to
  `Aircraft system ready` when `system.active` becomes true. It mixes command
  labeling with state reporting; state already appears elsewhere.
- `PressAndHoldButton` renders `DisabledReason` as an in-flow
  `.control-reason` paragraph after its progress bar.
- Different reason lengths wrap to different heights. Because both
  `.control-stack` elements share a CSS grid row, this changes internal track
  sizing and causes button/progress misalignment.
- Native disabled buttons do not reliably receive pointer/focus events. An
  accessible tooltip must be owned by a hoverable/focusable wrapper rather than
  depending only on the disabled button.

**Required change**

- Use stable Mission command label `Start III System` in active and inactive
  states. Disabled state remains communicated by styling and tooltip.
- Add an opt-in tooltip presentation for `PressAndHoldButton.disabledReason`;
  use it for both Mission command-band buttons without changing established
  inline-reason behavior elsewhere.
- Tooltip appears on wrapper hover and keyboard focus, is linked through an
  accessible description, does not occupy document flow, and contains full
  rejection/disabled reason.
- Give both Mission command stacks equal width and stable button/progress
  tracks. Long labels/reasons must not shift either progress bar.

**Acceptance criteria**

- First command always reads `Start III System`.
- Disabled buttons show no persistent red reason below them.
- Hovering either disabled command shows its exact disabled reason in a
  readable tooltip; equivalent keyboard access exists.
- Enabled buttons do not show a disabled-reason tooltip.
- Both buttons have equal width/height; both progress bars have equal width and
  aligned top/bottom edges at supported desktop/mobile widths.
- Hold progress, disabled command blocking, brief-click hint, and screen-reader
  semantics remain correct.
- Component tests cover tooltip hover/focus behavior and Mission tests cover
  stable label plus aligned command-band class/structure.

**Likely files**

- `src/III-Drone-GC/frontend/src/pages/MissionPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/MissionPage.test.tsx`
- `src/III-Drone-GC/frontend/src/components/interaction.tsx`
- `src/III-Drone-GC/frontend/src/components/interaction.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

### M-001 - Remove ambiguous Mission header `ready` badge

**Operator observation**

Top-right `ready` badge in Mission page header is uninformative.

**Code understanding**

- `AppShell.pageModeLabel()` supplies shared top-right page context.
- Mission-page value resolves to active mission mode display name, then raw
  `mission_state`, then `mission inactive`.
- Current `ready` therefore means mission domain state is `ready`. It does not
  prove inspection-start eligibility, corridor-position eligibility, stored
  geometry validity, or complete mission preflight readiness.
- Mission command band, Preparation section, onboard preflight evidence, and
  persistent footer already expose more precise mission state.

**Required change**

- Omit shared `page-mode` badge on Mission page.
- Retain page title and header alignment without empty placeholder space.
- Keep shared page-mode behavior unchanged on other pages unless later backlog
  feedback expands scope.

**Acceptance criteria**

- Mission header contains `Operator Console` and `Mission`, no ambiguous
  `ready`/raw mission-state badge.
- Active mode, mission phase, start eligibility, and preflight state remain
  visible in their existing labeled Mission-page surfaces.
- Header remains visually balanced at supported desktop/mobile widths.
- App-shell tests cover Mission-specific omission and unaffected contextual
  badges on another page.

**Likely files**

- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/layout/AppShell.test.tsx`
- Relevant layout CSS only if omission exposes alignment issues.
