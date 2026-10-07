# PX4 Parameter Baselines

Each aircraft profile relies on a set of PX4 parameters: the baseline of that
profile. Each baseline lives standalone in its own parameter file under
`deployment/px4/parameters`; nothing that applies or checks parameters holds
values of its own.

| Profile | File | What it holds |
| --- | --- | --- |
| `hil` | [`hil.params`](../deployment/px4/parameters/hil.params) | Ethernet transport to the Pi on the HIL ports (DDS UDP 8889, MAVLink UDP 14542). The physical flight controller does not fly in HIL. |
| `opti_track` | [`opti_track.params`](../deployment/px4/parameters/opti_track.params) | Ethernet transport on the aircraft ports (DDS UDP 8888, MAVLink UDP 14540, stack DDS domain), the vision-only estimator and the lab failsafes. |
| `real` | [`real.params`](../deployment/px4/parameters/real.params) | The same transport, the two RTK GNSS receivers, the GNSS/magnetometer/barometer estimator and the outdoor failsafes. |

The files are QGroundControl parameter files: tab-separated `MAV ID`,
`COMPONENT ID`, `PARAM NAME`, `VALUE`, `TYPE` rows (type 6 is INT32, 9 is
REAL32) and `#` comment lines that explain each group. QGroundControl can load
them too (Parameters, Tools, Load from file), followed by a reboot of the
flight controller.

The `real` baseline holds every parameter the `opti_track` baseline changes,
so applying it after a lab session restores the outdoor estimator and
failsafes. Its values other than the transport are the commissioned flight
controller's own: the parameter snapshot of 2026-09-04, which the flight
controller still held unchanged on 2026-10-07. In that configuration the two
receivers work as moving base and rover (`GPS_UBX_MODE 1`) and are blended for
position, while the heading comes from the magnetometer: `EKF2_GPS_CTRL` is 7,
so the GNSS heading is not fused.

Deliberately outside every baseline: sensor calibration, tuning,
`MPC_THR_HOVER`, the geofence and the RC input source. A parameter in a
baseline is held to its exact value at boot, so a baseline holds configuration,
not values tuned in the field. To track more parameters, add rows to the file.

`UXRCE_DDS_DOM_ID` is held to the stack ROS domain provisioned on the Pi
(`ROS_DOMAIN_ID` in `/etc/iii/runtime.env`), not to the value in the file.

## Apply a baseline

Run from the workspace on the ground computer, with the aircraft disarmed and
no propulsion battery connected:

```bash
iii px4 param-baseline --profile opti_track --host <pi> --dry-run
iii px4 param-baseline --profile opti_track --host <pi>
```

The command reads the flight controller's parameters, shows every parameter
that differs, and asks for confirmation (`--confirm` in scripts). It then
writes only those parameters, reboots the flight controller, and reads them
back. A flight controller that already matches is left alone and not rebooted.
`--dry-run` only shows the difference.

It refuses unless PX4 itself reports disarmed and landed.

It reaches the flight controller in one of two ways:

1. **Through the Pi**, when the Pi is provisioned for the same profile and its
   Runtime API has a live MAVLink link to PX4. The Runtime API owns the Pi's
   MAVLink port, so the parameters go through it.
2. **Over USB from the ground computer**, otherwise: for example the first
   time, when the flight controller still carries another profile's transport,
   or for `hil`, where the Pi's MAVLink link is the simulated PX4. Connect the
   flight controller's USB port to the ground computer; the device is found
   under `/dev/serial/by-id/*PX4*` (`--usb-device` overrides it), and
   QGroundControl must not hold the port. On this path the stack domain comes
   from the Pi when it is reachable and otherwise from the file;
   `--ros-domain-id` overrides it.

With neither path the command fails and names both reasons. `iii deploy dev`
and `iii host provision` never write PX4 parameters.

## Check at boot and start

On `real` and `opti_track` the Pi compares the flight controller with the
profile's baseline before every system boot and start: from the GUI, from
`iii system boot|start` routed through the Runtime API, and from `iii system
boot|start` on the Pi. A difference, or a flight controller whose parameters
cannot be read, refuses the boot or start with the differing parameters and
the command to run:

```text
PX4 parameters differ from the opti_track baseline: UXRCE_DDS_PRT is 8889
(baseline 8888), ... Run `iii px4 param-baseline --profile opti_track`.
```

Stop and shutdown are never refused. The current comparison is available from
`GET /cli/px4/parameter-baseline` on the Runtime API.

`hil` and `sim` fly a simulated PX4 and are not checked. The OptiTrack
rehearsals run an aircraft profile against a simulated PX4; they set
`III_PX4_SIMULATED=1` for the Runtime API, which skips the check there.
Never set it on an aircraft.
