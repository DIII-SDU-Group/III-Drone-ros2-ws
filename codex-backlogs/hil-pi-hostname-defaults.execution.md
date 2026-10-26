# HIL Pi hostname defaults execution

| ID | State | Owner / packet | Owned paths | Validation / evidence |
| --- | --- | --- | --- | --- |
| HN-01 | Completed | luna_worker plus parent, profile contract v2 | setup/setup_hil.bash, setup/setup_field.bash, setup/remote_runtime.bash, profile tests | 10 profile tests passed after stale-first DDS peer/source correction |
| HN-02 | Completed | replacement luna_worker plus parent, launcher contract v4 | tools/simulation/launch_hil_workstation.sh, launcher tests | Stale-first DNS, explicit-host, owner-route, and no-owner status checks passed in final combined suite; no live HIL run |
| HN-03 | Completed | parent, orchestration contract v2 | scripts/workspace/iii_dev.sh, scripts/workspace/coordinate_hil_restart.py, scripts/workspace/bind_hil_gc_target.py, tools/III-Drone-CLI/iii/runtime_routing.py, iii/__main__.py, tests | Coordinator forwards explicit host through launcher and GC selection; final combined suite and 24 CLI routing tests passed |
| HN-04 | Completed | replacement luna_worker, helpers contract v4 | tools/simulation/run_hil_perception_seam_probe.sh, scripts/workspace/resolve_sim_fixture.py, scripts/workspace/gui_v2_sim_e2e_smoke.py, tests | Ordered reachable IPv4 selection and pinned DDS peer; 78 focused helper tests and final combined suite passed |
| HN-05 | Completed | parent, docs/signoff v1 | relevant docs and final checks | Final combined workspace suite: 236 passed; native CLI routing: 24 passed. Independent terra_verifier PASS for code/docs, UNCERTAIN for live HIL (not run). Earlier wider integration: 123 passed, 3 pre-existing field-shell token/trust expectation failures; retained as negative evidence. |

No live HIL start, stop, or deployment is part of this execution.
Live read-only route check: `iii.local` resolved to `192.168.1.251`; kernel route selected `enp42s0` source `192.168.1.89`.
No Pi deployment or live HIL start/stop was performed. A physical/end-to-end mission run remains separate from this endpoint-default change.
