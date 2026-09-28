# Canonical HIL mission progress — 2026-09-23

**Not final acceptance:** attempt15 passed two cycles, but repeat attempt16 failed at the CableTakeoff stream handoff after successful release. Fresh safe cleanup and native stop completed. Attempt17 has not flown. Offline repairs passed Core/Mission tests, but independent reviews found goal-ordering, blended-reference and cancellation-to-next-goal gaps before deployment. The request-identity correction passed 53 focused cases, broader Interfaces/Core/Mission tests and ARM64 build. Review then found that canonical CustomOperation forwarding also needs the ownership handshake. The producer-completeness and lifecycle replan is being implemented, including parent-authorized request-scoped queue cleanup across mode transitions; see `runtime/hil_canonical_recovery_20260922T093058Z/CUSTOM_OPERATION_IDENTITY_REPLAN.md`. None of the rejected candidates was deployed.

The native split HIL stack completed two consecutive cycles of **Inspection → Reach Cable → Charge → Leave Cable → Inspection** on attempt15. ROS2 mission/control ran on the Raspberry Pi (`iii@10.42.0.15`); Gazebo, PX4 SITL, simulation adapters and ground control ran on the workstation. Physical propulsion remained outside this run: user confirmed secured aircraft, battery removed, SITL only.

## Acceptance evidence

Evidence directory: `runtime/hil_canonical_recovery_20260922T093058Z/attempt15_cycle/`.

- `execution_result.json`: driver exit 0, independent Pi observer exit 0.
- `mission_lifecycle_observation.json`: two source-ordered completed cycles; no mission/mode failures or latched failure; fresh post-cleanup landed/disarmed samples, no PX4 failsafe.
- `driver_events.json`: positive simulated charging of 151.13 W and 159.19 W, successful departure and resumed Inspection in both cycles.
- `native_command_receipts.json` and `native_state_receipts.json`: acknowledged native intents and matching API-consumer state gates.
- `departure_cycle1_samples.json` and `departure_cycle2_samples.json`: recorded Core upward velocity +2 m/s and PX4 NED vertical velocity −2 m/s for approximately five seconds before each simulated gripper release. These are bounded 1 Hz samples, not exact transition timestamps.
- `ACCEPTANCE_SUMMARY.json`: compact machine-readable result.

## Operational entry point

From this workspace, use `./iii-dev hil start`, `./iii-dev hil status`, `./iii-dev hil restart`, or `./iii-dev hil stop`. These own only this workspace's simulation partition and coordinate the Pi graph with the workstation clock. Ground control is at `http://127.0.0.1:5174`; select and confirm aircraft `iii-drone`, profile `hil`. The installed production catalog is `inspection-production`.

Source `setup/setup_hil.bash` for workstation ROS tools. It sets CycloneDDS/domain 42 and the Pi peer. Normal editable deployment uses `iii deploy dev --host 10.42.0.15`; add `--build --restart` for installed-code changes. Builds occur on the workstation, including ARM64 output; no compilation or OS shutdown on the Pi.

## Repairs and validation

The departure failure came from stopping pre-release Hover immediately and reusing a paused stream when a new maneuver used the same provider. The mission now retains Hover for 7000 ms, and Core binds stream generations to maneuver executions. Callback ownership/resumption fixes preserve callbacks across partial deactivation. The driver waits for native fused Hold and fresh native mission phase before dependent commands. Native startup tolerates bounded API connection refusal and a briefly absent/refused daemon control socket after a service restart, using read-only checks before mutation.

The first request-identity candidate passed Interfaces 1, Core 17 and Mission 8 CTest targets; that candidate was then rejected by source review and was not deployed. Focused driver 25, Runtime command confirmation 33, and native coordinator/CLI 41 tests passed, alongside earlier callback/observer/profile and GC validations indexed in `runtime/hil_canonical_recovery_20260922T093058Z/FINAL_VERIFICATION_PACKET.md`. Submodule lock verification passed. An inherited documentation-policy test still expects removed deployment-security prose; it is documented separately and was not silently changed. All unsuccessful attempts remain retained.

Independent Terra review returned PASS for the historical attempt15 packet (`REVIEW_DEPARTURE_STREAM_RESULT.json`). Later offline source reviews are preserved as `attempt17_cycle/SOURCE_REVIEW_1.json` through `SOURCE_REVIEW_5.json`; they exposed handoff ordering, blended predecessor progress, canceled-goal ownership, cached-reference revocation and restart-identity gaps. Parent replans and consolidated corrections are recorded in `DECISIONS.md`. No rejected candidate was deployed. The recorded idle restoration after attempt15 was fresh disarmed/grounded Hold (`FINAL_IDLE_STATE.json`). The later attempt16 failed and was safely stopped; attempt17 is prepared with targeted diagnostics. An immediate start during explicit daemon restart initially found its socket unavailable; the failure was retained and the native retry passed after readiness was verified. This demonstrates the requested simulation-backed Pi mission loop, not physical flight or real charging qualification.
