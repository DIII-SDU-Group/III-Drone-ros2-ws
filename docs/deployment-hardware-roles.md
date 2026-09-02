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
| mmWave CLI | required | `/dev/iii/mmwave-cli` | USB vendor/product/interface 00 |
| mmWave data | required | `/dev/iii/mmwave-data` | USB vendor/product/interface 01 |

The manifest explicitly carries both required and optional indexes; the current
hardware class has no optional attached-device role. Absence of a future optional
role will remain visible as `missing` but will not be treated as presence or block
health. Every missing, duplicate, ambiguous, or incorrectly resolved required
role blocks activation health for `real` and `opti_track`.

PX4 is not a USB hardware role. The production FCU link uses the Raspberry Pi's
built-in Ethernet interface and exposes MAVLink plus uXRCE-DDS. Its link,
protocol, compatibility, and fresh fused landed/disarmed state are independently
required by activation and field safety; USB-role acceptance cannot substitute
for PX4 transport evidence.

The host owns `eth0` as `10.41.10.1/24`; the expected PX4 peer is
`10.41.10.2`. Host firewall policy accepts only PX4-originated MAVLink on
`14540/UDP` and uXRCE-DDS on `8888/UDP`. The runtime API listens for MAVLink on
`udpin://0.0.0.0:14540`, while the daemon-managed micro-ROS agent owns the DDS
port when the application graph is started. The Pi-side USB Ethernet adapter
matches `enx*` and remains the DHCP operator/recovery link. A ping proves only
IP reachability; commissioning additionally requires fresh MAVLink heartbeats,
uXRCE-DDS vehicle topics, compatible firmware/parameters, and fused disarmed,
landed state. PX4 parameter or `net.cfg` changes are separate, explicit
backup-first flight-controller operations and are never inferred from host
convergence.

The release-owned source of truth is
`deployment/px4/network-baseline.json`. It binds the address pair, firmware
range, parameter ownership, exact `net.cfg` and `etc/extras.txt` content hashes,
and both transports under one content identity. PX4 uses a static address (not
DHCP), has no default route or DNS on this point-to-point control link, and starts
MAVLink and uXRCE-DDS explicitly from `extras.txt`. The parameter-managed
Ethernet owners (`MAV_2_CONFIG` and `UXRCE_DDS_CFG`) remain disabled because PX4
does not allow both applications to own the same parameter-configured Ethernet
port. A release is invalid if its real parameter manifest does not name the same
network-baseline identity.

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
