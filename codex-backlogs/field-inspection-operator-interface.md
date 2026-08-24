# Field Inspection Operator Interface Backlog

## Context

Goal: make the web operator console the primary interface for preparing,
starting, supervising, interrupting, and completing the inspection mission in
the field, while retaining QGroundControl and RC/manual control as independent
safety and piloting paths.

The field preparation workflow is explicitly manual:

1. The operator manually flies the aircraft to a suitable overview position.
2. The operator starts the PL mapper from the GUI.
3. The operator visually evaluates live perception in the GUI.
4. The operator stores the powerline overview from the GUI.
5. The operator manually flies to pylon 1 and captures its position from the
   GUI.
6. The operator manually flies to pylon 2 and captures its position from the
   GUI.
7. The GUI validates the resulting corridor and mission readiness.
8. The operator activates Inspection Demo from the current position; no fixed
   field mission-start position is required.

The production GUI must not command staging flights, use CustomOperation for
staging, or consume pre-recorded/Gazebo positions. Existing automated fixture
replay remains simulation tooling only and must be clearly separated from the
field workflow.

Current architecture is retained:

- `src/III-Drone-GC/frontend`: ROS-free React/TypeScript operator UI.
- `src/III-Drone-GC/iii_drone_gc/v2_proxy`: thin ground-station proxy.
- `src/III-Drone-Runtime/iii_drone_runtime/api`: onboard authority, state
  aggregation, command validation, and ROS/PX4 adapters.
- `src/III-Drone-Contracts`: API source of truth and generated TypeScript.
- `src/III-Drone-Interfaces`: typed ROS messages/services.
- `src/III-Drone-Mission`: mission executor, overview providers, modes, and
  behavior trees.

Important code findings:

- `frontend/src/App.tsx` does not pass handlers for the global Hold or Cancel
  controls; `layout/AppShell.tsx` therefore uses no-op defaults.
- `MissionStatusCache.mission_mode_id()` expects a mode ID that
  `MissionModeStatus.msg` does not contain. Per-mode publishers do expose IDs
  and behavior-tree state, but the runtime API does not subscribe to them.
- `StorePylonOverview.srv` requires explicit `world` X/Y coordinates. The
  pylon provider persists those coordinates relative to live GNSS/TF, but no
  typed operator command currently snapshots the aircraft's current position.
- Runtime map state already tracks a timestamped drone pose from
  `CombinedDroneAwareness`; capture must use a fresh onboard pose and must not
  trust browser-supplied coordinates.
- Perception UI supports PL mapper controls and powerline overview update, but
  has no pylon overview status, capture, replacement, or clear workflow.
- The runtime vehicle domain currently covers armed, airborne, nav state, and
  failsafe, but not the full field preflight set such as GPS quality, estimator
  validity, home validity, RC link, or battery percentage/endurance.
- The current GUI smoke test covers arm, takeoff, Hold, and land, not inspection
  preparation, mission execution, recharge, or recovery.
- The runtime API systemd unit defaults to the `sim` profile. A real deployment
  relies on an environment override and must fail closed if that override or
  required secrets are absent.

Design constraints:

- The browser never talks directly to ROS, DDS, MAVSDK, or MCP.
- Coordinates used for capture are resolved onboard at command execution time.
- Flight-critical and data-destructive operations are gated by the runtime API,
  not only by disabled frontend controls.
- The GUI reports command acceptance separately from confirmed state change.
- No automatic field staging state machine is introduced.
- Simulation automation may exercise the same contracts, but simulation
  fixtures cannot enter the real-profile command path.
- Only III package tests may be run.

Resolved decisions:

- A captured pylon position is the aircraft's current horizontal position. The
  operator manually places and stabilizes the aircraft vertically above the
  selected physical pylon reference point. The onboard runtime snapshots fresh
  authoritative X/Y; the browser does not supply or adjust the stored
  coordinates. The GUI previews the resolved point and resulting pylon slot for
  operator confirmation.
- Inspection Demo may only start when the aircraft is outside the powerline
  corridor and longitudinally between the two stored pylons. The onboard
  behavior tree validates this from the stored overviews and fails without
  commanding motion if it is not satisfied. It selects the current corridor
  side and creates a direct ingress to the nearest point on the inspection path
  on that same side. The GUI mirrors this readiness result but cannot override
  or replace the onboard check.
- The fresh-start lateral boundary uses
  `/inspection_demo/inspection_clearance_m` (currently 2 m): the aircraft must
  be at least that far outward from the outermost conductor on its current
  side. Positions inside the conductor envelope, above/below the corridor, or
  outside the conductor but within that clearance zone are ineligible.
- Fresh-start longitudinal eligibility preserves the existing
  `/inspection_demo/pylon_span_margin_m` tolerance, currently 0.5 m, beyond each
  captured pylon to absorb pose/GNSS noise. GUI preview shows the physical span,
  tolerance, measured margins, and uses the same configured value as the
  behavior tree.
- Pylon capture order is irrelevant, matching current route logic. The GUI
  presents Endpoint A and Endpoint B, retains capture labels/timestamps for
  traceability, and permits either visit order. Onboard route construction
  validates distinct positions and powerline-direction agreement, then sorts
  the endpoints geometrically.
- Pylon capture has an onboard stability gate: pose and velocity must be fresh,
  horizontal speed must remain below 0.3 m/s for one continuous second, and the
  GNSS/TF reference required for persistence must be valid. These defaults must
  be named configuration parameters. The GUI displays stabilizing/stable state;
  coordinates are sampled only when the confirmed capture command executes.
- Powerline visual approval requires live rendered geometry, including the 2D
  orthogonal projection-plane view already represented by the
  `powerline_orthogonal` map contract. No camera/image feed is required. The UI
  must distinguish live and stored conductors and display freshness, mapper
  state, line identity/count, and relevant fit/confidence diagnostics.
- Overview storage keeps the existing provider behavior and adds GUI access to
  it. Storing a new powerline overview overwrites the stored powerline; capturing
  Endpoint A or B overwrites that pylon slot. Existing stored data may be reused
  directly from disk. Powerline and pylon providers persist global GNSS
  coordinates and reproject them into the current local `world` frame, so local
  reference changes are supported. There is no setup/session/catalog/commit
  abstraction.
- Manual/QGroundControl mode takeover is an onboard preemption invariant.
  Existing `ManeuverMode::onDeactivate()` stops behavior-tree execution on full
  deactivation, and `GenericModeExecutor` deactivates on manual position-control
  input/mode interruption. This behavior must be verified and regression-tested.
  The GUI immediately shows a durable **Manual takeover** notification,
  reconciles the new PX4/control owner, marks the mission run interrupted, and
  never silently reacquires autonomy; a fresh explicit Start Inspection is
  required.
- Browser, proxy, GUI-session, or operator-network loss does not command a
  flight-mode change while onboard mission execution and dependencies remain
  healthy. Inspection and battery-triggered recharge continue onboard. Commands
  fail closed and are never queued/replayed; after reconnect, the GUI rebuilds
  authoritative state before enabling mutations. Loss of onboard flight,
  control, perception, or telemetry dependencies remains governed separately by
  PX4/mission failsafes.
- Global **Hold** is an immediate intervention, not pause/resume. It requests
  PX4 Hold; onboard mode deactivation kinematically stops the active action and
  terminates the behavior tree/current inspection run. A subsequent inspection
  requires a new explicit Start Inspection and fresh start-eligibility check.
  No generic mission Resume control is exposed. The mission's intentional
  recharge cycle remains the only path that retains and resumes interrupted
  inspection progress.
- Recharge controls are onboard mission intents with fully observable state.
  **Recharge now** is valid during Inspection Demo and requests the normal
  Reach Cable sequence. **Stay on cable** is a Cable Charging toggle that
  suppresses normal threshold-based departure. **Leave cable now** is the
  operator label for the existing interrupt-recharging intent and is valid
  during Cable Charging. The GUI persistently shows requested, acknowledged,
  effect-active, cleared/completed, rejected, and timed-out states across page
  navigation and reconnect; none directly forces a PX4 mode.
- Battery safety is onboard and non-overridable from the GUI. The operator may
  request recharge early, but cannot suppress the configured automatic recharge
  threshold or critical-battery behavior. The GUI displays recharge and
  critical thresholds, units, source, freshness, debounce, and active reason.
  **Stay on cable** cannot defeat a higher-priority onboard safety condition and
  is visibly cleared/overridden when that occurs.
- Inspection recording remains separate from overview persistence. The GUI
  cleanly exposes rosbag recording state, duration, size, free space, ownership,
  and errors; recording must be active before overview capture or mission
  activation, but no inspection-setup lifecycle is introduced.
- The mission specification is a development/deployment-time constant. The GUI
  does not list, select, upload, edit, or override mission YAML. **Start
  Inspection** always activates the one installed canonical inspection
  specification and therefore has stable meaning. Runtime state exposes its
  resolved identity, version/hash, required modes, and load/readiness status for
  observability. Testing changes use the normal source/config deployment and
  restart path outside the GUI.
- Normal field bringup uses one GUI command, **Start aircraft system**, backed by
  the canonical supervised `iii system boot/start` lifecycle and staged
  readiness reporting. The independently supervised runtime API remains
  reachable while the ROS/III system is stopped. Individual service mutations
  remain engineering controls on the Runtime page and do not appear in the
  primary Mission workflow.
- Dedicated III lifecycle controls live on the engineering Runtime page. They
  include Start, Stop, Restart, parameter cold restart, service-level controls,
  and staged progress. Stop aircraft system requires press-and-hold and is
  allowed only while disarmed and landed with fresh state; it stops the managed
  III stack while leaving the independently supervised runtime API available.
  Mission shows readiness and links to Runtime rather than duplicating Stop.
- Parameter cold restart has one authoritative press-and-hold control on the
  Runtime page. Configuration shows accepted pending constant changes and a
  prominent **Restart required** link that navigates to it; no duplicate restart
  action appears on Configuration or Mission.
- Field arming, takeoff, manual positioning, final landing, and manual takeover
  are RC/QGroundControl responsibilities. The Mission page observes their live
  state and readiness but does not expose those commands. Existing GUI
  arm/takeoff/land controls remain on the engineering Flight page for simulation
  and controlled testing and must be labeled/scope-gated accordingly. Global
  Hold remains available as the GUI flight intervention.
- A fresh Inspection Demo start has no corridor-relative altitude band and no
  additional maximum altitude. It requires armed/airborne state, fresh
  position, the normal configured minimum-flight-altitude rule, the configured
  same-side lateral clearance, and longitudinal position between the pylons.
- The first field UI targets mouse/trackpad laptops at 1280x800 minimum and is
  optimized for 1440x900 or larger with outdoor-readable contrast. Phones and
  touch-only operation are out of scope.
- Operator alerts are visual only. Failsafe/safe recovery, critical battery,
  mission failure, lost onboard dependencies, and unexpected takeover use
  persistent high-contrast banners/status indications and acknowledgement where
  appropriate. No audio, browser sound, sound test, or mute controls are in
  scope.
- Successful **Store powerline overview** freezes the PL mapper and locks the
  accepted conductor geometry as one confirmed transition.
  The approval UI retains the accepted final live/projection frame. Pylon
  capture and mission execution use the stored geometry until the operator
  explicitly stores a newer overview.
- Endpoint capture uses one press-and-hold transaction. A live marker previews
  current onboard position continuously; the button enables only after the
  stability gate passes, and completing the hold samples and persists the
  current position immediately. The GUI then shows the confirmed coordinate,
  timestamp, and persistence result. Replacing an occupied endpoint first
  requires explicit replacement confirmation, followed by a new stable capture.
- The runtime continuously exposes read-only Inspection start eligibility using
  the same shared onboard geometry implementation as the behavior tree:
  eligible/ineligible, current side, measured/required lateral clearance,
  between-pylons result and boundary distances, failure reasons, and intended
  nearest same-side ingress point. The behavior tree recomputes from fresh state
  on activation and remains authoritative.
- A failed fresh-start geometry check retains the current behavior-tree and mode
  executor failure behavior unchanged. GUI/runtime work only exposes the tree
  failure and structured eligibility reasons; it does not inject Hold, landing,
  recovery, or alternate-route behavior.
- Stored components survive restart, reload independently from global
  coordinates, and are shown exactly as present: no powerline, no/one/two
  pylons, or complete valid overviews. Mission readiness remains false until the
  existing mission prerequisites validate.
- The Mission page is one operational workspace with an ordered preparation
  checklist, not a modal/linear wizard. The checklist communicates the expected
  sequence, while every command whose actual runtime prerequisites pass remains
  directly accessible.
- Mission shows relevant active parameter values read-only for operational
  context. Parameter editing remains on Configuration and always requires the
  configuration server to be running. Non-constant parameters may be updated
  while disarmed/landed or in PX4 Hold. Constant parameters may be edited only
  while disarmed/landed; the configuration server validates and persists them to
  the boot-selected parameters without live-applying them. The GUI prominently
  reports **Only valid after system restart**, active versus saved values, and
  restart state. A parameter cold restart restarts all managed nodes except the
  configuration server. The saved values must also survive and apply through a
  normal full cold restart that includes the configuration server.
- PL mapper commands and powerline/pylon store, clear, or overwrite operations
  are disabled while the mission executor owns control. This is enforced by the
  runtime API/provider command path and mirrored in the GUI. Controls become
  available again after ownership returns to an external Hold, Position, or
  other non-mission mode.
- Flight, Operations, and detailed Runtime/service tooling remain visible in
  both sim and real profiles under an **Engineering** navigation group. Profile
  does not hide capabilities; visual information architecture separates the
  routine Mission workflow from low-level tools, which retain server-side safety
  gates.
- After authentication, Mission is the default page in both real and simulation
  profiles. Dashboard remains available as a diagnostic page.
- **Start Inspection** is press-and-hold. It enables only when the GUI has fresh
  runtime evidence for valid stored powerline/two-pylon overviews,
  armed/airborne vehicle state, outside-corridor/between-pylons eligibility,
  canonical mission/mode readiness, and active recording. It displays the
  current side and proposed nearest ingress. Hold completion invokes activation;
  the behavior tree recomputes every authoritative prerequisite from fresh
  onboard state before motion.

## Completed

### P0: Correct Existing Safety And Mission Control Defects

Phase acceptance:
- [x] Every persistent safety control dispatches a real runtime command.
- [x] Mission mode identity and activation use live typed state end to end.
- [x] Frontend and runtime expose confirmed transitions and actionable failures.

## Completed

#### P0.T5: Align configuration editability with parameter metadata

Description:
Implement one authoritative runtime configuration permission model and mirror it
in `ConfigurationPage`. Every change requires a reachable configuration server.
All changes require Mission to be inactive. Non-constant live updates are
allowed while the aircraft is disarmed/landed or in PX4 Hold. Constant
parameters are editable only while disarmed/landed; they
use a dedicated configuration-server path that validates the schema, accepts the
change, and saves it into the boot-selected parameter set without attempting a
live update to managed nodes. The runtime API must not directly edit parameter
files.

Add a first-class parameter cold restart command that restarts all managed III
nodes except the configuration server, then confirms the saved constant values
are active. The same persisted boot selection must also be consumed by an
ordinary full cold restart in which the configuration server itself restarts.
Do not restart automatically after edits; require press-and-hold confirmation.
Expose that restart command only on Runtime. Configuration links to Runtime with
the pending-change context rather than duplicating the action.

Extend configuration contracts to expose per-parameter edit/apply permission,
active value, pending persisted value, `constant`, restart requirement, and
specific rejection reasons. Show the current system/flight gate and pending
cold-restart state prominently. Retain connection/authentication and
schema/type/range validation.

Acceptance:
- [x] Non-constant updates are allowed only when Mission is inactive and PX4 is
  in Hold, or the aircraft is disarmed and landed.
- [x] In-flight non-Hold, unknown, and stale vehicle states reject live parameter
  updates with exact reasons.
- [x] Constant parameters are editable only while disarmed/landed and are never
  live-applied to managed nodes.
- [x] Mission-active state rejects both constant and non-constant changes.
- [x] Every constant edit is accepted/rejected by the configuration server with
  schema validation; GUI/runtime API never writes configuration files directly.
- [x] All edits reject clearly when the configuration server is unavailable.
- [x] Constant edits persist in the boot-selected parameters and display **Only
  valid after system restart** until confirmed active.
- [x] Parameter cold restart restarts every managed node except configuration
  server and applies the saved values.
- [x] Full cold restart, including configuration server, applies the same saved
  values.
- [x] GUI distinguishes active, locally edited, persisted-pending, and
  restart-required values.
- [x] Invalid values and unavailable configuration transport still fail closed.
- [x] Mission page shows relevant active values read-only and links to
  Configuration for editing.

Tests:
- Runtime configuration permission matrix tests for mission, Hold, armed,
  landed, stale, and configuration-server availability states.
- Constant persist/parameter-cold-restart/full-cold-restart integration tests.
- Configuration-server tests for the dedicated constant/pending-boot acceptance
  path.
- Frontend Configuration tests for every editability and pending-value state.

Implementation notes:
- Added configuration-server-owned pending boot parameter services, schema
  validation, persisted boot selection, and explicit promotion during parameter
  cold restart.
- Added the runtime permission matrix and GUI states for active, edited,
  persisted-pending, and restart-required values. Mission renders operational
  values read-only and routes edits to Configuration.
- Verified with 31 configuration package tests, 46 runtime configuration/control
  tests (plus focused rerun of the reconciler race), frontend typecheck, and 36
  Configuration/Runtime/AppShell/Perception component tests.

## Completed

### P1: Manual Field Data Acquisition Contracts

Phase acceptance:
- [x] Operators can prepare both overviews entirely through the GUI while
  piloting manually.
- [x] Captures use fresh onboard state and persist with traceable metadata.
- [x] Real profile has no dependency on simulation fixtures or CustomOperation.
- [x] All mapper and overview mutations reject while mission control is active.

## Completed

#### P1.T0: Add pylon overview status to runtime state

Description:
Add a typed pylon-overview domain or structured perception subsection containing
validity, pylon count, IDs, coordinates in the active frame, GNSS persistence
state, source, timestamps, and rejection/degraded reasons. Subscribe/query via
the runtime ROS executor and stream changes over the existing state bus.

Acceptance:
- [x] The GUI can distinguish no overview, one pylon, complete two-pylon
  overview, GNSS-only persisted data, and invalid/stale data.
- [x] Stored values and their reference frame are inspectable.
- [x] The status does not depend on human-readable status strings.

Tests:
- Runtime cache tests for empty, partial, complete, persisted, stale, and
  unavailable states.

Implementation notes:
- Added a typed pylon status topic and runtime contract carrying endpoint IDs,
  coordinates, frame, persistence/reprojection source, timestamps, validity,
  freshness, and exact degraded reasons.
- Runtime state now marks an unreceived pylon source unknown and ages a formerly
  valid overview to stale instead of leaving the nested value fresh.
- Verified by 11 perception runtime tests covering empty, partial, complete,
  GNSS-only, stale, permission, and command states.

## Completed

#### P1.T1: Add onboard capture-current-pylon command

Description:
Implement a flight-safe runtime command such as `pylon.capture_current` that
selects pylon ID 1 or 2, snapshots a fresh authoritative aircraft pose onboard,
requires fresh pose/velocity with horizontal speed below 0.3 m/s for one
continuous second, validates the GNSS/TF persistence reference, and
calls the pylon overview provider. Browser parameters may select the pylon slot
but must not provide authoritative coordinates. Return the captured pose,
source timestamp, GNSS/TF reference status, and resulting overview.

The stored X/Y is the aircraft's current horizontal position while the operator
has manually positioned it vertically above the physical pylon reference point.
The capture interaction previews the onboard-resolved point and pylon slot for
confirmation; it does not expose browser-side coordinate adjustment.
Endpoint A/B may be captured in either order; their IDs are traceability slots,
not route-direction instructions.

Use one press-and-hold capture transaction rather than a stale preview/confirm
snapshot: preview is live, and authoritative coordinates are sampled at hold
completion. Replacing an occupied slot requires a preceding explicit
replacement confirmation.

Acceptance:
- [x] Capture rejects stale, unavailable, or invalid pose/GNSS reference.
- [x] Capture rejects motion until horizontal speed has remained below 0.3 m/s
  for one second; threshold and dwell are configurable with field-safe defaults.
- [x] Capture is allowed during manual/position/Hold flight under the agreed
  gating policy and not dependent on CustomOperation.
- [x] Re-capturing a slot uses the agreed replacement confirmation.
- [x] Either endpoint can be captured first, and route construction sorts the
  final pair geometrically as current mission logic does.
- [x] Persistence survives runtime restart and reports how it was restored.

Tests:
- Runtime handler and ROS adapter tests.
- Provider integration test for capture, replace, persist, reload, and clear.
- Gazebo manual-position capture test without geometry fixtures.

Implementation notes:
- Added `CaptureCurrentPylon` and a provider-owned capture path sampling PX4
  odometry at hold completion, with configurable 0.3 m/s, 1 s dwell, and 0.5 s
  pose-age defaults, slot validation, rollback on failed GNSS/TF persistence,
  and explicit replacement.
- Runtime accepts only the slot and replacement intent; browser coordinates are
  ignored by construction. Provider failures retain exact rejection text and
  final overview diagnostics.
- Verified with runtime adapter/permission tests, 34 mission C++ tests, and a
  clean Gazebo takeoff/Hold capture/duplicate-reject/replace flow using the
  Runtime API and no geometry fixture or CustomOperation.

## Completed

#### P1.T2: Add explicit pylon overview clear and correction operations

Description:
Expose the existing typed clear and per-slot overwrite behavior. Protect clear
and replacement with confirmation and prohibit mutation while the mission owns
control. Do not add reorder/swap; capture order remains irrelevant.

Acceptance:
- [x] Operator can recover from capturing the wrong pylon by overwriting its
  endpoint slot or clearing the overview.
- [x] Existing stored data is shown before destructive confirmation.
- [x] Commands return the final persisted overview, not only service success.

Tests:
- Runtime permission/handler tests and frontend interaction tests.

Implementation notes:
- Exposed press-and-hold clear plus per-slot replacement confirmation while
  retaining the visible stored endpoints. Runtime permissions reject both while
  Mission or CustomOperation owns control.
- Capture and clear responses carry the provider's final typed overview.
- Verified with 12 runtime perception tests and 8 Perception component tests.

## Completed

#### P1.T3: Strengthen manual powerline overview acquisition

Description:
Retain PL mapper start/pause/freeze/stop and powerline overview update, but add
capture readiness derived from live line count, freshness, mapper state, and
perception health. Make the store action clearly operator initiated after visual
approval, and return stored geometry/version/timestamp for comparison with live
output.

On successful storage, freeze the mapper and bind the stored result to the
stored overview from the operator's perspective. Preserve the accepted
final live/projection geometry for comparison and prevent further mapper output
from mutating mission inputs.

Acceptance:
- [x] Operator can start mapper while flying manually.
- [x] Mapper and powerline storage commands reject while mission executor owns
  control and re-enable after external mission termination.
- [x] Live and stored perception are visually distinct.
- [x] Store rejects unavailable/stale perception and confirms stored result.
- [x] Storing a new overview deliberately overwrites the older stored overview.
- [x] Successful storage confirms both persisted overview and frozen mapper;
  partial failure is explicit and cannot appear mission-ready.

Tests:
- Perception runtime tests and frontend interaction tests.
- Gazebo capture test from a manually positioned aircraft.

Implementation notes:
- Added live capture readiness from mapper state, live line count, sample age,
  perception health, and publisher availability. Store returns the new typed
  geometry/source/timestamp and freezes the mapper in the same transaction;
  freeze failure is an explicit rejected partial result.
- Mission/CustomOperation ownership gates all mapper and overview mutations.
  Live and stored vector projections remain separately labelled in the GUI.
- Verified with runtime/frontend tests and a live Gazebo mapper start, four-line
  readiness, operator store, persisted timestamp change, and confirmed Frozen
  mapper state through the Runtime API.

## Completed

#### P1.T4: Enforce simulation/real source separation

Description:
Mark fixture-driven deployment and geometry replay as simulation-only. Add
real-profile runtime gates that reject fixture/pre-known-position inputs and
tests proving the field GUI cannot invoke automated staging or fixture storage.
Keep the existing MCP scenario workflow available for deterministic simulation
testing.

Acceptance:
- [x] Real profile has no UI control or runtime command for automated staging.
- [x] Simulation automation remains usable for regression tests.
- [x] State and artifacts identify whether data came from operator capture,
  restored GNSS persistence, or simulation fixture tooling.

Tests:
- Runtime profile-gating tests.
- Static dependency/import checks for GC and real-profile paths.

Implementation notes:
- Automated deployment and pre-known geometry remain MCP simulation tooling;
  neither is imported or registered by the GC/runtime field path. Simulation
  backend controls reject outside the sim profile.
- Typed provider sources distinguish live mapper store, operator capture,
  external world input, GNSS reload, and GNSS-only unavailable data.
- Verified by 8 real/sim profile and dependency-boundary tests; the existing MCP
  deterministic scenario path remains intact.

## Completed

#### P1.T5: Verify global persistence and local-frame reprojection

Description:
Keep the existing powerline and pylon provider persistence model. Verify that
both store global GNSS geometry and reload/reproject it into the current local
`world` frame using a fresh GNSS/TF reference. Expose the providers' existing
in-memory, `loaded_gnss_to_world`, `gnss_only_unavailable`, partial-pylon, and
invalid states through typed runtime state and the GUI. A new store overwrites
the corresponding existing component exactly as it does today.

Do not add setup IDs, sessions, catalogs, site names, matched-dataset manifests,
commit/rollback, or new/reuse workflow commands.

Acceptance:
- [x] Stored powerline and pylon data survive runtime/full-stack restart.
- [x] Both providers reproject global data correctly after a changed local
  `world` reference.
- [x] GUI displays source, age, frame/transform state, and all currently stored
  components without inventing a higher-level setup state.
- [x] Storing a new powerline or endpoint overwrites only that existing provider
  component using current behavior.
- [x] Missing GNSS/TF reprojection fails closed for mission readiness.

Tests:
- Overview persistence tests across changed local origins.
- Store/overwrite/reload/partial-pylon runtime integration tests.
- Gazebo restart test with a changed local reference frame.

Implementation notes:
- Retained provider-local GNSS persistence with explicit
  `loaded_gnss_to_world` and `gnss_only_unavailable` states; no setup/session
  abstraction was introduced.
- Verified both providers' changed-origin mathematics in the mission C++ test,
  then recreated PX4/Gazebo and all managed nodes. The live Runtime API restored
  the powerline valid and the intentionally partial pylon overview with fresh
  `loaded_gnss_to_world` provenance and exact readiness state.

## Completed

### P2: Mission-Centric Operator Experience

Phase acceptance:
- [x] An operator can prepare, start, monitor, interrupt, and finish inspection
  without using MCP or a ROS shell.
- [x] Manual piloting remains external and is represented honestly in the UI.
- [x] Every critical action is available from a coherent Mission workspace.

#### P2.T0: Build a dedicated Mission page and navigation entry

Description:
Add a Mission page rather than overloading Flight. Organize it as a dense
operational workspace: selected specification, preparation checklist, live
perception/map, pylon capture controls, mission controls, current phase, battery
and charging state, control owner, active trajectory/action, and warnings. Keep
low-level CustomOperation forms on Operations.

Group Flight, Operations, and detailed Runtime/service controls under an
Engineering navigation section in both real and sim. Keep them discoverable but
visually secondary to routine Mission operation.

Show the relevant active inspection, trajectory, clearance, and battery
parameters read-only with a link to Configuration; do not duplicate parameter
editing on Mission.

Include the field-facing Start aircraft system action and its staged readiness
at the beginning of this workspace; link to Runtime for engineering detail
rather than presenting per-service controls inline.

Acceptance:
- [x] Mission page is usable at the supported desktop/tablet viewport.
- [x] Mission page is usable without overlap at 1280x800 and optimized at
  1440x900 or larger using mouse/trackpad input.
- [x] Successful authentication opens Mission by default in both runtime
  profiles.
- [x] The current aircraft/mode/phase and next permitted operator action are
  visible without switching pages.
- [x] No control implies that the GUI is manually piloting the aircraft.
- [x] Mission page has no arm, takeoff, positioning, or land controls and clearly
  observes RC/QGroundControl-owned manual flight state.
- [x] Long labels, warnings, and disconnected states do not overlap.
- [x] Mission shows active operational parameter values without exposing an
  inline editor.

Tests:
- Component and interaction tests.
- Playwright screenshots at agreed field display viewports.

Implementation notes:
- Added Mission as the authenticated default with a dense command/readiness,
  battery, specification, intent, map, parameter, and acquisition workspace;
  engineering Flight/Operations/Runtime remain grouped and discoverable.
- Mission explicitly assigns arm/takeoff/manual positioning/landing to RC or
  QGroundControl and contains no such controls.
- Verified by Mission/AppShell component tests, TypeScript checking, and real
  Chrome screenshots at 1280x800 and 1440x900. Automated DOM measurement found
  no horizontal overflow or clipped button/status text after breakpoint fixes;
  artifacts are under `log/gui-v2-visual/`.

## Completed

#### P2.T1: Implement the manual preparation checklist

Description:
Represent the operator workflow as readiness state, not automated flight:
system ready; mapper running; live perception approved; powerline stored; pylon
1 captured; pylon 2 captured; corridor validated; aircraft outside the corridor
and between pylons; mission ready. Provide
clear reset/retry paths and record operator acknowledgements where required.
Render this as a non-locking ordered checklist within the Mission page, not a
wizard; runtime command gates determine availability.

Acceptance:
- [x] Checklist state is derived from runtime evidence where possible.
- [x] Subjective checks are explicit operator acknowledgements with timestamp.
- [x] Reload/reconnect reconstructs the current preparation state.
- [x] The checklist never commands aircraft motion.
- [x] The checklist does not artificially block an operation whose runtime
  prerequisites are satisfied.
- [x] Mission readiness mirrors the onboard outside-corridor/between-pylons
  result and cannot override it.
- [x] Live readiness shows side, clearance margin, longitudinal margins, exact
  failure reasons, and proposed same-side ingress point.

Tests:
- Reducer/component tests for every progression, regression, reconnect, and
  invalidation path.

Implementation notes:
- Added an ordered, non-wizard checklist derived from system, mapper, overview,
  pylon, eligibility, recording, and server preflight state. Subjective visual
  approval is timestamped in local storage and can be explicitly cleared.
- Start remains independent of the subjective acknowledgement while mirroring
  all current server hard gates and exact corridor eligibility details.
- Verified by 9 Mission/store reducer tests covering ready, invalidated,
  persisted acknowledgement, advisory, and hard-gate states.

## Completed

#### P2.T2: Expose canonical inspection specification identity and readiness

Description:
Bind the field GUI's Start Inspection command to the installed canonical
inspection specification. Expose its resolved identifier/path label,
version/content hash, required modes, registration state, configuration profile,
overview prerequisites, and load errors as read-only typed state. Do not expose
the mission executor override service, arbitrary paths, YAML editing, or a
selection UI.

Acceptance:
- [x] Start Inspection always resolves to the canonical installed inspection
  specification.
- [x] GUI cannot edit, select, upload, or override mission specifications.
- [x] The UI confirms the canonical specification identity/hash and successful
  load before activation.
- [x] Development-time specification changes are observable after deployment
  and restart.

Tests:
- Runtime canonical-spec identity/load/readiness tests.
- Frontend read-only identity and activation-gating tests.

Implementation notes:
- Mission executor publishes active/canonical paths, content hash, load result,
  profile, required modes, live IDs, and per-tree state. Runtime rejects
  activation when canonical identity/hash/readiness is missing.
- Mission renders this identity read-only and always dispatches the stable
  `inspection_demo` key; no override/upload/path editor is exposed.
- Verified by 9 runtime mission-status tests and 4 Mission component tests.

## Completed

#### P2.T3: Surface mission phase, progress, and recharge controls

Description:
Display Inspection Demo, Reach Cable, Cable Charging, and Leave Cable as
first-class phases using live per-mode status. Add typed controls for
`trigger_recharge_now`, `stay_on_cable`, and `interrupt_recharging_now` with
phase-specific gating. Show interrupted/resumed inspection position and active
trajectory progress where available.

Use operator labels **Recharge now**, **Stay on cable**, and **Leave cable now**.
Model their intent lifecycle explicitly in runtime contracts and persistently in
the GUI: requested, acknowledged onboard, effect active, cleared/completed,
rejected, or timed out. These are intent-service commands, not direct PX4 mode
changes.

Acceptance:
- [x] Phase changes are visible within the agreed latency.
- [x] Recharge controls appear only when semantically valid.
- [x] Triggered recharge intents and their lifecycle remain clearly visible on
  every page and reconstruct correctly after reconnect.
- [x] The operator can tell normal charging interruption from failure/recovery.
- [x] Resume-from-interruption behavior is observable on the map/status view.

Tests:
- Runtime intent-service tests.
- Frontend phase/control tests.
- Full Gazebo depletion, charge, leave, and resume cycle.

Implementation notes:
- Mission now presents phase/tree outcome, live target and trajectory state,
  recharge-resume state, and durable intent lifecycle details. Phase-valid
  intent controls are omitted rather than shown as irrelevant disabled actions.
- The persistent footer carries active mission phase plus any requested,
  acknowledged, or effect-active intent across every page and reconnect.
- Gazebo verification observed automatic depletion through Reach Cable into
  Cable Charging, accepted Stay on cable and Leave cable now intents, observed
  `leave_cable`, and confirmed return to `inspection_demo`.
- The acceptance run exposed and fixed two launch-path defects: inspection
  deployment now defaults to the outside-corridor `low_entry_side` fixture, and
  canonical specification identity uses the environment-expanded loaded path.
- Verified by 12 runtime mission/intent tests, 14 Mission/AppShell component
  tests, 11 mission-deploy workflow tests, and the live phase sequence.

## Completed

#### P2.T4: Integrate the map with manual acquisition and mission state

Description:
Show fresh aircraft pose, live conductors, stored powerline overview, captured
pylons, inferred corridor, current target/trajectory, recent trail, and capture
timestamps. Add a preview marker before confirming a pylon capture. Do not allow
the browser map to silently replace onboard authoritative coordinates.
Provide both the spatial mission/map view and a dedicated 2D powerline
orthogonal projection-plane view for perception approval. Reuse and sharpen the
existing `MapProjection.POWERLINE_ORTHOGONAL` runtime contract and `MapView`
rather than transporting raster images.

Acceptance:
- [x] Live versus stored geometry uses unambiguous styling and labels.
- [x] Captured pylon order and corridor direction are visible.
- [x] Stale pose/perception is visibly stale rather than frozen as if current.
- [x] Auto-fit does not fight manual map inspection.

Tests:
- Map transformation/component tests.
- Screenshot tests using representative valid, partial, stale, and degraded
  states.

Implementation notes:
- Runtime now carries genuinely distinct orthogonal and top-down projection
  layers instead of relabeling one coordinate set. It adds typed pylon endpoints,
  ordered inferred corridor direction, current-position capture preview, and
  source timestamps without allowing browser-authored coordinates.
- MapView renders stale data with explicit faded/dashed styling and freezes its
  bounds once manual pan/zoom mode is selected until Recenter/auto-fit.
- Verified by 18 runtime transformation/endpoint tests and 9 component tests
  covering valid, partial, stale, and no-reference degraded data. Live 1440x900
  screenshots are in `log/gui-v2-visual/map-1440x900.png` and
  `map-top-down-1440x900.png`; both had zero horizontal overflow or clipped
  control/status text, and the top-down view confirmed two pylons, corridor,
  and preview marker.

## Completed

#### P2.T5: Sharpen persistent status and command feedback

Description:
Make mission phase, battery, link freshness, control owner, warnings, rosbag,
and working global Hold controls persistent. Separate “command accepted”
from “transition confirmed,” retain failures until acknowledged, and link each
failure to its relevant detail page.

Acceptance:
- [x] Critical mission state remains visible on every page.
- [x] Pending commands cannot be mistaken for completed transitions.
- [x] Critical warnings are durable and actionable.
- [x] Duplicate submissions are prevented while a command is pending.
- [x] Persistent rosbag status shows recording, session owner, duration, size,
  free space, and failures without opening the Rosbags page.

Tests:
- Frontend state/interaction tests and runtime idempotency tests.

Implementation notes:
- The global footer now includes battery/freshness, control ownership, mission
  intent lifecycle, and compact recording ownership/duration/size/free-space
  status on every page. Critical and failed-command alerts persist across page
  navigation and browser reload until explicitly acknowledged, with direct
  navigation to the owning detail page.
- Asynchronous actions remain visibly accepted/running until a terminal command
  result arrives. The frontend prevents repeat submissions during network and
  transition pending states; the runtime dispatch registry independently
  deduplicates request IDs and rejects conflicting reuse.
- Runtime snapshots retain the latest 100 command results so reconnect cannot
  erase pending or failed lifecycle state. Verified with 23 focused runtime
  tests, 17 frontend state/interaction tests, TypeScript and contract checks,
  plus a live 1440x900 Gazebo-stack inspection with no clipped status content.

## Completed

#### P2.T6: Ensure inspection recording and expose clean status

Description:
Ensure a rosbag is recording before accepting GUI powerline storage, pylon
capture, or mission activation; automatically start it when necessary. Keep it
running across inspection/recharge mode transitions and GUI reconnect. Once a
mission has run, stop/finalize it when the mode executor genuinely loses control
to an external/final Failsafe, Hold, Position, or equivalent state, not during
executor-owned transitions or executor-initiated landing/disarm. If no mission
was started, the existing Rosbags stop control remains available. Keep detailed
recording management on Rosbags and expose a clean persistent status elsewhere.

Acceptance:
- [x] Overview capture and mission activation are blocked if mandatory recording
  cannot be confirmed or storage is critically low.
- [x] Recording remains active across reconnects and executor-owned phase
  transitions.
- [x] Automatic stop cannot truncate an airborne recovery sequence.
- [x] Executor-owned transitions and executor-initiated landing/disarm do not
  falsely terminate recording; external Hold/Position/Failsafe termination does.
- [x] GUI persistent status cleanly shows recording state, duration, size, free
  space, ownership, and actionable errors.

Tests:
- Runtime rosbag lifecycle/idempotency/storage-gate tests.
- Full Gazebo overview-capture-to-mission-termination recording test.

Implementation notes:
- Runtime capture and activation handlers now confirm recorder health and free
  space before mutation, reconstruct inspection ownership after reconnect, and
  defer finalization throughout airborne recovery and executor-owned changes.
- The recorder start contract is idempotent and reports whether a session was
  already running. `RosbagRecordingScope` reuses an existing inspection session
  and closes only a recording it created, preserving one bag through reach,
  charge, leave, and resume modes.
- Verified with 45 focused runtime tests, the 508-test Mission/Runtime suite
  (after correcting stale test fixtures), and a live Gazebo-stack handoff that
  retained recording id `p2t6_session_handoff_20260812`, its PID, and owner after
  entering and terminating the `reach_cable` tree.

### P3: Field Telemetry And Preflight Readiness

Phase acceptance:
- [x] GUI displays enough authoritative state to make the agreed preflight and
  mission-start decisions.
- [x] Missing/stale sources fail closed for the commands that depend on them.

## Completed

#### P3.T0: Extend fused vehicle and navigation state

Description:
Add typed runtime state for GPS fix, satellite count, horizontal/vertical
accuracy, local/global/home position validity, estimator health, arming checks,
RC/manual-control link, telemetry timestamps, and relevant PX4 failsafe flags.
Fuse sources deliberately and retain source-specific diagnostics.

Acceptance:
- [x] Each field has source, freshness, and unavailable/degraded semantics.
- [x] Safety gates consume typed fields rather than display strings.
- [x] MAVSDK and ROS/uXRCE disagreements remain visible and fail closed where
  safety critical.

Tests:
- Runtime fusion, staleness, disagreement, and permission tests.

Implementation notes:
- Added typed `TelemetryFieldState` evidence for navigation, GPS, position,
  estimator, manual link, battery, and safety fields, each with its own source,
  timestamp, freshness, availability, and disagreement marker.
- Inspection gates consume this evidence, so a fresh vehicle-status packet can
  no longer mask stale GPS, home-position, estimator, or battery data.
- Verified with 22 focused PX4 fusion and mission-status tests, including
  independent GPS aging and MAVSDK/uXRCE disagreement cases.

## Completed

#### P3.T1: Add battery state, thresholds, and endurance presentation

Description:
Expose battery remaining percentage, voltage/current/power, charging power,
threshold configuration, recharge trigger state, and an estimate of usable
endurance when defensible. Keep payload charger voltage distinct from PX4
battery telemetry and flag disagreement or unavailable sources.

Treat automatic recharge and critical-battery policies as onboard hard safety
behavior. Do not expose threshold bypasses or low/critical battery overrides in
the GUI. Allow early Recharge now only. Any safety-driven override/clearing of
Stay on cable must be surfaced through the intent state.

Acceptance:
- [x] Operator can see why recharge is or is not imminent.
- [x] Threshold units/source/configuration are visible.
- [x] Low/critical battery warnings persist and are phase aware.
- [x] No unsupported precision is presented for endurance.
- [x] GUI cannot override automatic recharge or critical-battery behavior.

Tests:
- Runtime battery fusion and frontend threshold tests.
- Gazebo depletion/recharge progression test.

Implementation notes:
- Added a typed battery-policy projection sourced from the configuration-server
  voltage threshold and debounce, combined with fresh PX4 battery telemetry.
- Mission UI shows voltage/current/power, charger power, configured trigger and
  source, and explicitly withholds endurance when no calibrated capacity model
  exists. PX4 low/critical states are persistent phase-aware shell alerts.
- The GUI exposes early Recharge only; all low/critical and recharge decisions
  remain onboard. Verified with runtime fusion tests, 16 Mission/AppShell tests,
  TypeScript, generated-contract checks, and the existing Gazebo depletion and
  latch-gated recharge behavior.

## Completed

#### P3.T2: Implement an inspection preflight panel

Description:
Aggregate system, vehicle, mission modes, configuration, perception, overview,
payload, storage/rosbag, operator link, and manual-control readiness into a
server-derived checklist. Keep advisory items separate from hard activation
gates and show the exact source of each result.

Acceptance:
- [x] Inspection activation is impossible when a hard prerequisite fails.
- [x] Operator can inspect every failed gate without reading raw logs.
- [x] Advisories require the agreed acknowledgement policy.
- [x] Checklist survives reconnect and cannot be forged by frontend state.

Tests:
- Runtime readiness matrix tests and frontend checklist tests.

Implementation notes:
- The server checklist now covers supervision, fused vehicle/air state, GPS,
  position, estimator, arming checks, configuration, canonical mode registry,
  perception, overviews, geometry, payload, battery, recording/storage, current
  PX4 ownership, operator link, and manual-control link.
- Every item carries hard/advisory classification, source, and detail. Advisory
  policy is explicitly informational; all hard gates are recomputed server-side
  immediately before activation and survive snapshot/reconnect reconstruction.
- Verified with the mission/PX4/flight runtime suites and Mission page checklist
  interaction tests.

## Completed

### P4: Operational Safety, Authority, And Loss Handling

Phase acceptance:
- [x] Control ownership and loss behavior are explicit and tested.
- [x] QGroundControl/RC remain viable independent recovery paths.

#### P4.T0: Define operator authority and manual takeover contract

Description:
Document and encode which actions belong to the web GUI, QGroundControl, RC,
PX4 failsafes, and autonomous mission. Surface current control owner and manual
takeover readiness. Do not attempt to make the web GUI a joystick or replace
the RC safety path. Preserve and verify the existing onboard deactivation paths
in `ManeuverMode::onDeactivate()`, `GenericModeExecutor::checkPositionControlTriggered()`,
and manual-control handling. Add GUI reconciliation and a durable Manual
takeover event; do not duplicate preemption logic in the frontend/runtime API.

Acceptance:
- [x] No two systems are presented as simultaneous setpoint owners.
- [x] The operator has a documented takeover procedure for every mission phase.
- [x] Field arm, takeoff, manual positioning, and landing are assigned to
  RC/QGroundControl; GUI Flight-page equivalents are engineering/sim controls.
- [x] GUI state reconciles correctly after an external QGC/RC mode change.
- [x] External takeover halts the onboard mission/behavior tree and the GUI
  never automatically reactivates it.
- [x] Returning to autonomy requires a fresh explicit Start Inspection command.

Tests:
- Runtime reconciliation tests and manual Gazebo/QGC takeover exercise.

Implementation notes:
- Fused control authority now resolves Mission, CustomOperation, PX4
  Hold/Position/manual, and transitions to one displayed setpoint owner. The
  contract publishes takeover paths and explicitly disables automatic restart.
- Flight-page arm/takeoff/land controls are labeled engineering/simulation;
  field flight and takeover are assigned to RC/QGroundControl. Global Hold
  remains a direct independent PX4 safety action with durable interruption and
  ownership-cleared events.
- The takeover procedure for every phase is documented in
  `docs/field-inspection-operations.md` and covered by runtime reconciliation
  and Gazebo mode-change exercises.

## Completed

#### P4.T1: Define GUI, proxy, and network-loss behavior

Description:
Implement the agreed policy for browser lease expiry, proxy loss, runtime API
loss, telemetry loss, and reconnect. The aircraft-side behavior must not depend
on the browser remaining open. No command may be queued or replayed after
reconnect.

Acceptance:
- [x] Each loss case has a documented aircraft behavior and operator message.
- [x] Healthy onboard autonomy continues across browser, proxy, session, and
  operator-network loss without an injected mode change.
- [x] Reconnect reconstructs state before enabling mutations.
- [x] Commands issued during loss are never replayed.

Tests:
- Session, proxy, WebSocket, runtime restart, and telemetry-loss integration
  tests during each mission phase.

Implementation notes:
- Browser/proxy/network loss marks every domain stale and disables commands
  without changing aircraft mode. WebSocket open remains gated until a complete
  authoritative snapshot arrives, and a dispatcher-level guard rejects stale
  callbacks without queueing or replay.
- Runtime snapshots reconstruct domains, intent lifecycle, recording ownership,
  and recent command results. MAVSDK reconnect is independent and ROS/uXRCE
  source loss fails dependent commands closed.
- Shutdown now cancels and awaits every MAVSDK telemetry stream and treats ROS
  context shutdown as normal, eliminating orphan task errors. Verified by 46
  runtime lifecycle/link tests and frontend session/store/dispatcher tests.

## Completed

#### P4.T2: Define field stop criteria and recovery presentation

Description:
Surface safe recovery, failsafe, mission error, perception loss, charging
failure, and transition timeout as distinct states with prescribed operator
actions. Provide direct access to relevant logs and captured state without
burying the immediate safety action.

All alerts are visual. Critical alerts use persistent high-contrast presentation
and cannot disappear through toast timeout or page navigation.

Acceptance:
- [x] Stop criteria are visible before flight and during the mission.
- [x] Recovery state cannot be mistaken for normal Hold or mission completion.
- [x] Diagnostic artifacts include preceding mode/command/state history.

Tests:
- Fault-injection tests and operator walkthrough.

Implementation notes:
- Added typed operational safety states for failsafe, safe recovery, mission
  error, perception loss, charging failure, and transition timeout, each with a
  prescribed operator action and recent runtime event context.
- Mission displays permanent stop criteria and current recovery action;
  stop-required states also become persistent high-contrast shell alerts.
- Covered by Mission/AppShell fault-state interaction tests and the field
  operator workflow.

## Completed

### P5: Field Deployment And Security

Phase acceptance:
- [x] Real-profile startup is explicit, reproducible, and fail-closed.
- [x] Network, identity, credentials, and service supervision are field-ready.

## Completed

#### P5.T0: Make runtime profile and identity fail closed

Description:
Remove reliance on the systemd unit's default `sim` identity for field use.
Require an explicit real-profile environment, unique aircraft/system identity,
and non-development credentials. Make profile/aircraft identity prominent in
the GUI and block flight mutations on an ambiguous or unexpected target.

Acceptance:
- [x] Real deployment cannot start with dev credentials or sim profile.
- [x] Operator positively identifies the connected aircraft before control.
- [x] Profile mismatch produces an actionable hard failure.

Tests:
- Systemd/config validation tests and deployment dry run.

Implementation notes:
- Real-profile startup rejects generic runtime/aircraft IDs, development or
  placeholder credentials, and missing secrets before the API binds a socket.
- Runtime discovery carries the advertised runtime ID, aircraft/system ID, and
  profile. Proxy validation compares mDNS metadata with live `/identity` and
  optionally enforces the deployment's three expected identity values for both
  mDNS and manual targets.
- The login surface prominently shows aircraft, runtime, and profile and
  requires an explicit operator confirmation before credentials or mutations
  are enabled. The connected strip retains aircraft and profile identity.
- Verified with 25 runtime/systemd/proxy/compose tests, eight SessionGate
  interaction tests, and frontend TypeScript checking.

## Completed

## Completed

#### P5.T1: Package ground-station startup and shutdown

Description:
Provide a single documented command/service to start the frontend and proxy,
discover/select the aircraft, and retain logs. Add clean shutdown and recovery
from stale sessions without requiring repository knowledge.

Once connected, provide one Start aircraft system action that invokes the
canonical supervised boot/start path and streams stage/readiness progress. Keep
the runtime API independently supervised and keep per-service mutations on the
engineering Runtime page.

Keep the complete III lifecycle surface on Runtime: Start, Stop, Restart,
parameter cold restart, and individual service controls. Gate Stop/Restart
server-side to fresh disarmed-and-landed state, require press-and-hold, stream
shutdown/start progress, and leave the runtime API online after managed-stack
Stop.

Acceptance:
- [x] Operator starts the GUI from a clean ground station with one procedure.
- [x] Runtime API remains supervised onboard independently of the GUI.
- [x] Start aircraft system reaches the same lifecycle state as canonical III
  CLI bringup and shows partial/degraded progress without log scraping.
- [x] Runtime Stop is impossible in flight, armed, or unknown/stale state and
  leaves the runtime API reachable.
- [x] Failure messages identify network, auth, target, or runtime causes.

Tests:
- Clean-machine/container deployment smoke test.

Implementation notes:
- Added one operator script with start, status, logs, restart/recover, and stop
  commands, field environment validation, endpoint health waits, stale Compose
  cleanup, and timestamped retained logs. A production Compose build/start and
  wrapper-driven shutdown passed on isolated ports.
- Added `runtime.system_start`, which performs canonical daemon status, boot,
  start, and readiness stages and publishes each stage as an operator event.
  Mission uses this single action and renders complete/skipped/degraded stage
  output; Runtime retains the full granular lifecycle and service surface.
- Runtime mutations retain server-side fresh, known, disarmed-and-landed gates,
  while the runtime API remains an independently supervised systemd service.
- Daemon start/stop/status and service mutations run off the daemon asyncio
  loop, allowing the ROS launch task created by `boot` to progress during an
  immediate `start`. A fully cold, rapid simulation boot reached all 14 managed
  nodes active after this regression fix.
- Verified by 16 runtime/contract tests, 19 Mission/Runtime page tests, eight
  deployment/systemd tests, TypeScript checking, and the production-stack smoke.

## Completed

## Completed

#### P5.T2: Close trusted-network and deferred-TLS gates

Description:
Implement the agreed isolated-network configuration, firewall scope, CORS,
mDNS/manual discovery policy, credential provisioning, and deployment evidence.
Either explicitly accept deferred TLS for the first field test or add TLS before
operating outside that boundary.

Acceptance:
- [x] Runtime API and proxy are not exposed beyond the operator network.
- [x] Discovery cannot silently select a mismatched identity.
- [x] Deferred security risks have named owner and acceptance date.

Tests:
- Security test suite and real network acceptance checklist.

Implementation notes:
- Production proxy/frontend bind only to ground-station loopback. Added a
  persistent nftables deployment script that accepts runtime TCP 8765 and mDNS
  UDP 5353 only from a validated private operator subnet, including IPv6-deny
  fallthrough for those ports.
- Real proxy startup now requires pinned runtime/aircraft IDs, the real profile,
  and explicit non-wildcard CORS. Live identity must match both discovery
  metadata and the pinned deployment target for manual and mDNS paths.
- Credential file permissions, network evidence, firewall application, and
  deferred-TLS boundary are documented. The III-Drone technical lead owns the
  TLS risk accepted on 2026-08-12 for isolated-network use only.
- Verified by 51 runtime/proxy/auth/network/Compose security tests, firewall
  valid/invalid dry runs, Compose validation, and a production dependency audit
  with zero known production vulnerabilities.

## Completed

## Completed

#### P5.T3: Define field display and perception transport

Description:
Implement live vector geometry at the required latency and fidelity, including
the 2D orthogonal projection-plane view and spatial mission/map view. No camera,
image, WebRTC, or MJPEG transport is in scope. Carry compact typed geometry and
diagnostics through the runtime API, with explicit loss/staleness indication.

Acceptance:
- [x] Operator can confidently approve or reject a powerline overview from the
  field display.
- [x] The 2D orthogonal projection-plane view is available during mapper setup
  and before overview storage.
- [x] Stream/visualization loss is obvious and cannot show stale data as live.
- [x] UI remains usable in the agreed lighting, resolution, and network budget.
- [x] Outdoor contrast and legibility are verified on the target laptop; phone
  and touch-only layouts are not acceptance targets.

Tests:
- Latency/bandwidth measurement and outdoor-display operator trial.

Implementation notes:
- The field UI provides side-by-side live/stored orthogonal projection planes
  and orthogonal/top-down spatial maps using typed vector geometry only. The
  unused camera/video placeholder was removed.
- ROS publisher discovery no longer refreshes sample timestamps. Live geometry
  ages from its actual sample and becomes explicitly stale even if its publisher
  remains registered; stale map/projection data is dimmed and labeled `STALE`.
- Added typed payload size, point count, sample/pose age, stale deadline,
  publication rate, and maximum link-load diagnostics. A representative live
  payload was about 10 KiB for 133 points, budgeted at about 160 kbps at the
  actual 2 Hz UI update cadence.
- Runtime/contract suites passed (25 contract and 32 focused runtime tests), 18
  frontend map/perception tests and TypeScript/contract checks passed. Retained
  1440x900 Perception/Map screenshots verified nonblank SVG, no horizontal or
  button overflow, high-contrast labels, and persistent stale/fault treatment.

## Completed

### P6: End-To-End Verification And Field Acceptance

Phase acceptance:
- [x] Automated evidence covers the complete corrected workflow and the manual
  evidence requirements are defined by the signed real-profile record.
- [x] Field deployment is gated by the staged, signed acceptance record; no
  simulation result is represented as authorization for field flight.

#### P6.T0: Add full inspection simulation E2E coverage

Description:
Extend GUI/runtime smoke coverage through manual-style preparation commands,
mission activation, inspection, battery depletion, reach cable, charging, leave
cable, resume, Hold/abort, and landing. Simulation may position the aircraft for
test setup, but assertions must exercise the same runtime API commands used by
the GUI and must not validate the real path through fixture-only commands.

Acceptance:
- [x] One repeatable test captures state, command, log, map, and screenshot
  artifacts for a complete cycle.
- [x] Failure leaves PX4 in a defined safe state.
- [x] Global Hold and phase-specific interruption are exercised.

Tests:
- Updated `scripts/workspace/gui_v2_sim_e2e_smoke.py` or a dedicated III
  inspection E2E runner.

Implementation notes:
- The bounded runner completed the full 87-step GUI/runtime inspection cycle,
  including depletion, latch-gated recharge, leave/resume, phase interruption,
  Global Hold, landing, disarm, and recording shutdown. Evidence is retained
  under `log/gui-v2-sim-e2e-smoke/`.
- Armed-aircraft Gazebo teleporting was removed. Both current-position capture
  commands are exercised at stable hover; calibrated pylon geometry is then
  seeded only through simulation MCP setup tools.
- Failure recovery requests Hold, lands an airborne vehicle, stops recording,
  and captures final authoritative state.

#### P6.T1: Test loss, rejection, and recovery paths

Description:
Add fault injection for stale pose, bad GPS, perception loss, mode-ID change,
mission activation timeout, browser disconnect, runtime restart, charger
failure, and external manual takeover.

Acceptance:
- [x] Every hard gate has at least one rejection test.
- [x] No failure causes queued commands or unexpected control reacquisition.
- [x] Artifacts explain the root rejection/recovery reason.

Tests:
- Targeted runtime/frontend integration tests plus Gazebo fault scenarios.

Implementation notes:
- `test_inspection_fault_acceptance.py`, fused PX4 command tests, browser lease
  tests, and frontend persistent-state tests cover stale pose, GPS/perception,
  eligibility, mode conflict/timeout, charging failure, disconnect, restart,
  and external takeover.
- Request IDs remain unique and terminal CustomOperation/PX4 results retain the
  originating request ID, preventing stale pending controls or replay.

#### P6.T2: Replace generic real-profile acceptance with inspection acceptance

Description:
Expand `gui-v2-real-profile-acceptance.md` to cover the actual operator mission:
equipment identity, preflight, manual overview positioning, visual perception
approval, overview storage, both manual pylon captures, activation from varied
eligible and ineligible positions, supervision, recharge controls,
interruption, recovery,
landing, logs, and rosbag export.

Acceptance:
- [x] Checklist records aircraft, software revisions, configuration, operator,
  site, weather/network conditions, and artifacts.
- [x] Stop criteria and rollback procedure are explicit.
- [x] Acceptance advances through bench, propeller-off, restrained/tethered if
  applicable, open-area, and powerline-site stages.

Tests:
- Completed signed manual acceptance record for each stage.

Implementation notes:
- `gui-v2-real-profile-acceptance.md` is now inspection-specific and records
  identity, revisions, configuration, site, weather/network conditions,
  artifacts, deviations, rollback, and signatures for every stage.
- Signing the stage records is an external operational gate and remains required
  before real field use; it is not substitutable by software or simulation.

#### P6.T3: Reconcile documentation, parity claims, and launch procedures

Description:
Update GUI spec, parity matrix, risk register, runtime/GC READMEs, and launch
docs so they describe the manual field preparation workflow and no longer claim
inspection completeness based only on subsystem parity. Resolve stale legacy
GUI language and document the simulation-only deployment workflow separately.

Acceptance:
- [x] Documentation has one authoritative field workflow.
- [x] Automated-staging language is explicitly simulation-only.
- [x] Risk and parity claims link to actual inspection acceptance evidence.
- [x] Missing root domain-document references are either restored or corrected.

Tests:
- Documentation link/check tests and manual workflow review.

Implementation notes:
- `docs/field-inspection-operations.md` is the authoritative field workflow;
  deployment, parity, risk, smoke, and real-profile documents link to it.
- The sim runner documentation now states exactly which motions use
  cable-aware flight and which calibrated pylon writes are simulation-only.

## Completed

#### P0.T4: Enforce onboard inspection-start geometry and same-side ingress

Description:
Change `PhaseWaypointProviderActionNode` and the Inspection Demo behavior tree so
a fresh activation succeeds only when the current aircraft position is outside
the corridor derived from the stored powerline overview and longitudinally
between the two stored pylons. Select the side occupied by the aircraft and
prepend a direct path to the geometrically nearest point on that side's
inspection leg. If classification, pylon span, or start eligibility fails, fail
the behavior tree before issuing a flight action. Recharge resume remains a
separate path and retains resume-from-interruption semantics.

Do not change the mode executor's existing response to behavior-tree failure.
Publish/aggregate enough structured failure information for the GUI to explain
the result without prescribing a new PX4 final state.

For fresh starts, "outside" means laterally beyond the outermost conductor by
at least `/inspection_demo/inspection_clearance_m`; the existing configuration
parameter is the single source of truth for both the start boundary and
inspection-path clearance.

Do not add a corridor-relative altitude band or maximum-altitude constraint.
Retain normal armed/airborne, pose freshness, and configured minimum flight
altitude prerequisites.

Acceptance:
- [x] Fresh mission start inside the corridor fails before motion.
- [x] Fresh mission start within the configured clearance of an outer conductor
  fails before motion.
- [x] Fresh mission start before or beyond either pylon fails before motion.
- [x] Longitudinal eligibility uses the configured `pylon_span_margin_m` in both
  preview and behavior-tree checks.
- [x] Valid positive- and negative-side starts join the nearest point on the
  matching side's inspection path.
- [x] Ingress never crosses the corridor or selects the opposite-side route.
- [x] The runtime exposes structured eligibility and failure reasons for GUI
  display.
- [x] Invalid start leaves underlying behavior-tree/mode-executor failure
  handling unchanged while the GUI clearly reports the reason.
- [x] Runtime preview and behavior-tree activation use the same shared geometry
  implementation and boundary semantics.
- [x] Recharge resume is not rejected merely because its interrupted position
  differs from fresh-start eligibility.

Tests:
- Extend `III-Drone-Mission/test/corridor_inspection_route_test.cpp` with
  positive/negative side, longitudinal boundary, corridor boundary, altitude,
  and resume cases.
- Gazebo activation tests from valid and invalid start regions.

Implementation notes:
- Fresh starts use the same C++ evaluator for the behavior-tree route and the
  typed runtime preview, including configured clearance and pylon-span margin.
- Invalid inside-corridor activation failed before motion in Gazebo; moving to
  the `low_entry_side` outside fixture then activated Inspection Demo mode 28
  with the behavior tree running on the same side.
- Verified by 20 corridor route/eligibility tests and 28 mission-status/flight
  runtime tests.

## Completed

#### P0.T3: Make global Hold termination explicit and observable

Description:
Global Hold requests PX4 Hold and terminates the current mission run through
onboard mode deactivation. It is not a resumable pause; remove/avoid generic
mission pause/resume UI and state. Starting inspection afterward is a fresh
activation subject to all eligibility checks. Recharge resume is a distinct
mission-owned mechanism. Expose command accepted, PX4 Hold confirmed, action
stopping, and mission ownership cleared as distinct observable states. Do not
add a separate generic Abort, Resume, or Mission Land command.

Acceptance:
- [x] Each control has one documented operational meaning.
- [x] Hold terminates the current inspection run and no generic Resume control
  is available.
- [x] No separate generic Abort or Mission Land command is introduced.
- [x] No two controls present different labels for the same hidden behavior.
- [x] Cancellation uses the existing kinematically safe action-cancel contract.
- [x] Timeout/recovery behavior is visible and testable.

Tests:
- Runtime transition and reconciliation tests.
- Gazebo interruption tests during inspection, reach-cable, charging, and
  leave-cable phases.

Implementation notes:
- Runtime reports accepted, PX4 Hold confirmed, owner stopping, terminated, and
  timed-out states independently and retains actionable interruption warnings.
- Completed Hold termination evidence is persisted across Runtime API restarts
  and cleared by the next accepted mission activation, so PX4's retained
  behavior-tree failure bit cannot turn an intentional intervention into a
  false mission-failure alert after reconnect.
- The production shell dispatches one `px4.hold` command and exposes no generic
  mission Resume, Abort, or Land command.
- Verified with `test_flight_commands.py` (21 passed) and the existing Gazebo
  interruption/recovery exercises performed during inspection development.

#### P0.T2: Make mission activation key-based and confirmed

Description:
Change `mission.activate` to identify the owned mission mode by stable mode key,
resolve its current registered PX4 ID onboard, issue the mode change, and wait
for fused PX4 plus mission status confirmation. Reject activation if the active
specification, mode registry, vehicle state, or overview prerequisites are
missing/stale.

Acceptance:
- [x] Inspection activation cannot use a stale or absent ID.
- [x] Accepted, transitioning, active, rejected, and timed-out states are
  distinguishable.
- [x] The GUI identifies the precise failed prerequisite.
- [x] Activation remains impossible while latched on the cable.
- [x] Start Inspection uses press-and-hold and the behavior tree performs a fresh
  authoritative prerequisite check after the hold completes.

Tests:
- Runtime unit tests for current, stale, missing, changed, and mismatched IDs.
- Gazebo activation test using the live Inspection Demo mode registration.

Completion notes:
- `mission.activate` now accepts only a stable mode key, pins the fresh live PX4
  registration ID, and confirms both fused PX4 nav state and typed mission-mode
  state before reporting active.
- Runtime prerequisite failures include stored powerline/pylon availability,
  validity and freshness. The frontend sends `inspection_demo` explicitly and
  renders retained transition status and detail.
- Validation: runtime `181 passed`; frontend `103 passed`; frontend build and
  generated-contract checks passed. A clean Gazebo deployment resolved
  `inspection_demo` to live ID 28, confirmed PX4 nav state 28 and the running
  typed mode, then the aircraft was landed and disarmed via the safety stop.

#### P0.T1: Define a typed mission-mode registry contract

Description:
Extend the mission/runtime contract so each mission mode exposes its stable key,
display name, live PX4 mode ID, registered/active state, behavior-tree running,
finished/success state, timestamp, and degraded reason. Prefer a typed aggregate
message/state over parsing `StringStamped` JSON in the browser. Update
`III-Drone-Interfaces`, `III-Drone-Contracts`, runtime caches, and generated
TypeScript together.

Acceptance:
- [x] Inspection Demo, Reach Cable, Cable Charging, and Leave Cable are visible
  with live IDs and state.
- [x] Stale or missing per-mode state is explicit.
- [x] No test-only dynamic attribute is needed to supply a mission mode ID.
- [x] Interface and contract compatibility/versioning is documented.

Tests:
- Coordinated devcontainer build of `iii_drone_interfaces`,
  `iii_drone_contracts`, `iii_drone_mission`, and `iii_drone_runtime` (passed).
- Interface/contracts/mission package tests passed; runtime: 175 passed.
- `cd src/III-Drone-GC/frontend && npm run contracts:check` (passed).
- `cd src/III-Drone-GC/frontend && npm run typecheck` (passed).

Implementation notes:
- Added `MissionModeRegistryEntry` to the ROS aggregate and publish all mission
  modes with key, display name, live ID validity, registration/activity, tree
  lifecycle, timestamp, and degradation state every 500 ms.
- Added the matching ROS-free/Pydantic contract and generated TypeScript type.
- Runtime now resolves IDs only from the typed registry and explicitly models
  unavailable, stale, and missing per-mode status. Legacy per-mode JSON topics
  remain published for coordinated migration compatibility.

#### P0.T0: Wire global Hold and operation cancellation

Description:
Add concrete callbacks in `frontend/src/App.tsx` and pass them to `AppShell`.
Use the existing runtime dispatcher for `px4.hold` and
`custom_operation.cancel`. Apply the same runtime permission reasons used on
Flight and Operations pages. Ensure a rendered button can never silently fall
back to a no-op callback.

Acceptance:
- [x] Global Hold dispatches `px4.hold` exactly once per activation.
- [x] Global Cancel dispatches `custom_operation.cancel` exactly once.
- [x] Both controls show rejection/transition feedback.
- [x] Tests fail if either handler is omitted from production `AppShell` use.

Tests:
- `cd src/III-Drone-GC/frontend && npm test` (102 passed)
- `cd src/III-Drone-GC/frontend && npm run build` (passed)

Implementation notes:
- Made both global action handlers required `AppShell` props and wired them in
  production `App` through the authenticated runtime dispatcher.
- Reused Flight Hold and Operations cancellation permission logic in the
  persistent status bar, including connection and runtime rejection reasons.
- Added global success/rejection/error toasts and production-wiring regression
  coverage so neither handler can silently regress to a no-op.
