# GC install and CLI routing execution

Source: `standalone-gc-install-and-cli-routing.md`. Preserve the verbatim request there and all pre-existing dirty work. No live deployment or HIL launch has been performed for this batch.

| ID | State | Owner / packet | Owned paths | Validation / blocker |
| --- | --- | --- | --- | --- |
| GC-01 | Completed | `/root/pi_cli_install`, `luna_worker`, r4 | standalone installer, QGC pin, installer tests | Parent verified legacy migration, source identity staging, managed-root refusal, 28 focused tests and live workstation installation. |
| GC-02 | Completed | `/root/pi_cli_install`, `luna_worker`, r4 | installed GUI snapshot and launcher | Snapshot includes build inputs and staged CLI/Contracts; isolated installed UI start/status/stop passed. |
| GC-03 | Completed | `/root/gc_cli_qgc`, `luna_worker`, r1 | native CLI QGC lifecycle, CLI tests | Parent inspected source; full CLI suite in ROS devcontainer: 162 passed; installed wrapper QGC start/stop passed. |
| GC-04 | Completed | `/root/gc_cli_qgc`, `luna_worker`, r3–r4 | CLI runtime routing/profiles, API and rosbag controls | Parent inspected route and local controls; 157 CLI tests passed in ROS devcontainer, 1 recorder helper test; live hosts pending GC-07 |
| GC-05 | Completed | `/root/pi_cli_install`, `luna_worker`, r3 | SIM/HIL launch paths and tests | Parent inspected orchestration and reran full focused suite: 92 passed, including previously intermittent progress assertion; live SIM/HIL remains untested |
| GC-06 | Completed | `/root/pi_cli_install`, `luna_worker`, r5 | iii-dev command boundary and stack migration | Parent corrected stale attach guidance and reran complete wrapper/coordinator suite: 91 passed; intermittent progress assertion passed this run |
| GC-07 | Completed | Parent; independent `/root/gc_install_final_review` (`terra_verifier`) | docs, installation integration, lifecycle and routing fixes | Live dev workstation install, QGC start/stop, isolated installed GUI start/status/stop, native SIM route and existing HIL project ownership check passed. CLI 162 passed; installer/launcher 28 passed; wrapper/coordinator/integration 93 passed; submodule lock and diff checks passed. Pi remained undeployed and field laptop unavailable, so live HIL/field route acceptance remains outstanding. |

Planning: Terra writer drafted seven tasks; independent Terra backlog verifier corrected coverage. Parent resolved command family, target selection, and iii-dev boundary from original instructions. User confirmed field GC Linux x86_64.

Final instruction review found and parent corrected four integration issues: unmanaged install-root replacement, explicit OptiTrack selection under a sourced field shell, foreign same-name Compose project mutation, and GC logs residing inside the replaceable install snapshot. The independent verifier rechecked the corrections and found no remaining material implementation defect. Read-only Pi route currently rejects with `III_RUNTIME_CHECKOUT_MISMATCH` (expected CLI hash `2d0c30270c9d8e4c22f169ad7e75defdf9d61ac2ff2d7e1d9b9c066c20515f54`, observed `cccd27cd56f80ad5abf7c9019990f5b69a9cf9a0df2040de5f781c1d79a0de26`). Live HIL/field acceptance is inconclusive until manual deployment and a field host are available; no HIL flight or Pi deployment was performed in this batch.
