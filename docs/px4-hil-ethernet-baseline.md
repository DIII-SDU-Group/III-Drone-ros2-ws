# PX4 HIL Ethernet Baseline

This is the one explicit PX4-side configuration required before the Pi can
bridge a physical PX4 through Ethernet. It is separate from `iii deploy dev`:
the developer deployment never writes flight-controller parameters, firmware,
or arming state.

Use it only for HIL with the vehicle disarmed and no propulsion battery
connected. The Pi must already be provisioned with its PX4 Ethernet interface
at `10.41.10.1/24`, and `iii px4 inspect --host <pi> --profile hil` must show
the PX4 peer at `10.41.10.2`.

## Preflight

From the Pi, inspect the link and its listeners without changing PX4:

```bash
iii px4 inspect --host <pi> --profile hil
```

The expected result includes the Pi address `10.41.10.1/24`, peer
`10.41.10.2`, DDS UDP `8889`, and MAVLink UDP `14542`.

## Apply once through PX4 NSH

Copy [`deployment/px4/hil-ethernet.nsh`](../deployment/px4/hil-ethernet.nsh)
to the PX4 SD card or open it from a PX4 NSH console, then run:

```nsh
source /fs/microsd/hil-ethernet.nsh
```

This explicitly changes the PX4 parameter store, saves it, and reboots the
flight controller. It does not arm the vehicle. The script selects Ethernet
for the uXRCE-DDS client, directs it to the Pi agent on UDP `8889`, and creates
an Ethernet MAVLink instance broadcasting to UDP `14542`.

## Verify after reboot

On PX4 NSH, the following read-only checks must show the configured values and
active modules:

```nsh
param show UXRCE_DDS_CFG
param show UXRCE_DDS_AG_IP
param show UXRCE_DDS_PRT
param show MAV_2_CONFIG
param show MAV_2_UDP_PRT
param show MAV_2_REMOTE_PRT
param show MAV_2_BROADCAST
uxrce_dds_client status
mavlink status
```

On the Pi, start or retain the HIL runtime and capture one bridge topic:

```bash
iii system boot --profile hil --confirm --non-interactive
iii system start --confirm --non-interactive
ros2 topic echo --once /fmu/out/vehicle_local_position_setpoint
```

The required evidence is a non-empty DDS message plus observed UDP traffic on
the Pi PX4 Ethernet interface. A listening Pi port or a successful ICMP ping
alone is not HIL transport proof.

## Recovery

If PX4 does not emit either transport after reboot, keep the aircraft disarmed,
preserve the NSH `status` output, and stop HIL validation. Do not reset all PX4
parameters: compare the seven listed values with the script first. To disable
this HIL transport deliberately, restore the prior PX4 parameter export through
QGroundControl or PX4 NSH and reboot the flight controller.

The script is idempotent: rerunning it restores exactly the same transport
values. It has no operation ID because it is a direct PX4 console action rather
than an `iii` deployment operation.
