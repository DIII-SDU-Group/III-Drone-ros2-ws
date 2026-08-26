# Legacy Deployment Retirement Map

Status: historical inventory and cutover gate, not an operator runbook.

The retired `DIII-SDU-Group/III-Drone-deployment` repository was inspected at
commit `4ab4ae76013ba3ff904189777e7c97af107d94e1` on branch `v2.2-staging`. Its Git
history is preserved. It is not the source of current deployment commands or
runtime ownership.

## Retained knowledge and destination

| Legacy behavior/data | Destination | Rationale |
|---|---|---|
| mmWave USB vendor/product/interface mapping and observed serial `00DEEC69` | `deployment/hardware/legacy-observations.json`, then the commissioned shared hardware-role manifest | Preserve evidence without silently choosing between the conflicting `00DEEC69` and current `00E241A0` devices. |
| Generic Arduino charger vendor/product match | historical observations and physical ambiguity tests | The unqualified mapping is too broad for production; current serial literals are also evidence, not an automatically accepted contract. |
| `/dev/video0` cable-camera use | stable `/dev/iii/cable-camera` role generated from the shared hardware manifest | Enumeration order is not a stable device identity. |

The current workspace `III-Drone-Core/udev/99-diii-usb.rules` literals are also
retained evidence, not the deployment authority. They remain in place until the
shared manifest's unplug/replug, reboot, port-swap, simultaneous-enumeration, and
role-functional physical gate is complete. No tool may infer retirement from a
successful fixture, a single enumeration, or an observed serial.
| Basic apt/tool installation intent | versioned Ansible aircraft and GC roles under `deployment/ansible/` | Convergence must be pinned, idempotent, auditable, and separable from application activation. |
| Host environment/bootstrap intent | cloud-init plus Ansible and stable host launchers | Production cannot source a checkout or user shell profile. |
| CLI availability | repository-managed operator environment using `iii-deployment` and the III CLI | Global editable installs and shell startup mutation are not reproducible. |
| Per-node Compose topology | no migration; `III-Drone-Supervision/system_spec.py` remains authoritative | Recreating process ownership would conflict with the canonical daemon-owned graph. |
| Mutable clone/checkout/reset/pull | no migration; signed immutable source-provenance bundles | Branch mutation and destructive cleanup cannot represent dirty field work safely. |
| CLI `deploy install`, `container`, `synchronize`, raw SSH, `pull_src`, and `pull_rosbags` | no migration; typed `inspect`, `stage`, `activate`, `field`, `rollback`, `status`, and configuration-capture operations | Destructive synchronization and arbitrary remote commands bypass signed identities, receiver planning, and durable operation evidence. |
| `latest` image and privileged container runtime onboard | no migration | The Pi runs native immutable releases and never compiles or uses Docker in production. |

## Archive gate

Repository archival is allowed only after the signed Q131 matrix proves raw
provisioning, qualified and field release paths, update/rollback/power-loss
recovery, tuning/capture, offline operation, disaster recovery, documentation,
and a clean-host run with the legacy clone absent. Archival must record this exact
final commit and branch, the replacement qualified release and manual revision,
and the history recovery location. History must never be deleted or rewritten.

Current implementation and operator documentation begins at
[`../deployment/CONTEXT.md`](../deployment/CONTEXT.md) and the
[`documentation index`](README.md).
