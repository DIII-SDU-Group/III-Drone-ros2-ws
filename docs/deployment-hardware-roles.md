# Shared Aircraft Hardware Roles

The Raspberry Pi attached-device contract is the single shared manifest at
`deployment/hardware/shared-hardware-role-manifest.json`. It is a hardware-class
contract, not an aircraft inventory. Ansible installs that exact canonical file
as `/etc/iii/hardware-role-manifest.json` and installs its checked-in,
deterministically generated rules as
`/etc/udev/rules.d/90-iii-hardware-roles.rules`.

The declared real-aircraft paths are:

| Role | Requirement | Stable path | Match boundary |
| --- | --- | --- | --- |
| cable camera | required | `/dev/iii/cable-camera` | the single USB V4L2 capture node (`index=0`), never `/dev/video0` |
| charger/gripper | required | `/dev/iii/charger-gripper` | USB vendor/product; a second match is ambiguity |
| FMU | required | `/dev/iii/fmu` | USB vendor/product; a second match is ambiguity |
| mmWave CLI | required | `/dev/iii/mmwave-cli` | USB vendor/product/interface 00 |
| mmWave data | required | `/dev/iii/mmwave-data` | USB vendor/product/interface 01 |

The manifest explicitly carries both required and optional indexes; the current
hardware class has no optional attached-device role. Absence of a future optional
role will remain visible as `missing` but will not be treated as presence or block
health. Every missing, duplicate, ambiguous, or incorrectly resolved required
role blocks activation health for `real` and `opti_track`.

## Inspection

Use the authenticated receiver-backed inspection from the operator computer:

```bash
iii host inspect --target real --json
iii host hardware inspect --target real \
  --capture .iii/evidence/hardware/baseline.json --json
```

The first and second forms are aliases. The optional capture is created with
owner-only permissions and is never overwritten. The report contains only
attached USB role evidence: device node, USB identity/interface, serial when the
device exposes one, USB path, driver, V4L2 capture properties, resolution state,
and stable-link verification. It does not collect environment variables,
accounts, network state, files, arbitrary udev properties, or other host data.
Unmatched devices remain in the sanitized capture for review.

Inspection is read-only. It cannot add a serial, edit the manifest, generate a
different rule, or convert an observed replacement into a supported device.

## Physical Commissioning Gate

Retiring `III-Drone-Core/udev/99-diii-usb.rules` is forbidden until one physical
Pi produces accepted captures for all of these phases against one manifest ID:

1. baseline with all devices connected simultaneously;
2. unplug and replug every role;
3. reboot and recapture under a distinct boot ID;
4. move devices between available USB ports and recapture;
5. reconnect all devices simultaneously and prove every stable link;
6. run each role's functional evidence named in the manifest.

The commissioning evaluator rejects a missing phase, rejected inspection,
changed manifest ID, learned policy, or a reboot sequence with no distinct boot
identity. Physical evidence is retained and signed by the commissioning workflow;
it is not written into Git as aircraft inventory. Until that evidence exists,
the manifest states `retained-pending-physical-evidence`, and the conflicting old
serial observations remain historical evidence only.

## Replacement Devices And Match Changes

A replacement that already matches exactly one existing role still requires the
role-specific functional test and complete recommissioning. The manifest does not
change merely because the unit or its USB port changed.

An unmatched replacement must first be captured with `iii host hardware inspect`.
Supporting it then requires a normal feature branch that changes the shared
manifest, regenerates the golden rule file, passes schema/missing/duplicate/
ambiguity/port-change tests, converges Ansible twice with zero second-run drift,
and repeats physical commissioning. Serial allowlists are accepted only when a
commissioning-record reference proves they are necessary. There is no automatic
learning path.
