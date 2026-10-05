# Pi, PX4, and HIL Links

The Pi uses its dedicated PX4 Ethernet connection at `10.41.10.1/24`; the PX4
endpoint is normally `10.41.10.2`.  The workstation--Pi link is separate and is
used for the HIL simulator and normal developer SSH.

These address assignments preserve a clear test topology. They do not impose
an access policy or firewall restriction. `iii px4 inspect --host <pi>`
confirms the Pi-side address and route plus the DDS and MAVLink listeners of
the profile provisioned on the Pi: UDP `8889`/`14542` for HIL and
`8888`/`14540` for real and OptiTrack. This inspection is read-only.

The Pi USB Ethernet adapter is dual-mode: it retains `10.42.0.15/24` for a
direct workstation link and is a DHCP client when attached to a router or a
computer providing DHCP. A workstation configured only as a DHCP client does
not provide an address; use `10.42.0.1/24` locally in that direct-link case.

The PX4's Ethernet transport is a separate, one-time PX4 parameter baseline;
follow [PX4 HIL Ethernet Baseline](px4-hil-ethernet-baseline.md) for HIL or
[`deployment/px4/opti-track.nsh`](../deployment/px4/opti-track.nsh) for the
OptiTrack lab before expecting any DDS or MAVLink packet from the PX4.

## Stack ROS domain

Provisioning writes one ROS 2 domain for the aircraft stack into
`/etc/iii/runtime.env` for every aircraft profile (`iii_ros_domain_id`, default
42, `iii host provision --ros-domain-id N`), together with Fast DDS over UDPv4.
The flight controller's `UXRCE_DDS_DOM_ID` must equal it; otherwise the agent
creates PX4's topics in a domain the stack does not see.

## Optional Wi-Fi client

`iii host provision --wifi-ssid <ssid>` adds a Wi-Fi client on `wlan0` in its own
root-only netplan file (`/etc/netplan/85-iii-wifi.yaml`). It does not change the
PX4 Ethernet link or the workstation USB-Ethernet link. While associated, the
Wi-Fi owns the default route (route metric 50 against 100 for wired DHCP); out
of range it never delays boot. The passphrase comes from an owner-only file
outside the checkout or a prompt and is stored only on the Pi.

## OptiTrack lab data flow

```text
Motive PC 192.168.10.3 --NatNet unicast--> lab gateway 192.168.10.1
                                            ROS domain 0: /body_splitter/body_<id>/pose
                                                  |
                                     Wi-Fi OptiTrack_5G (192.168.10.0/24)
                                                  |
drone Pi wlan0 --> opti_track_pose_relay --> /fmu/in/vehicle_visual_odometry (stack domain)
                                                  |
               micro_ros_agent UDP 8888 <-- eth0 10.41.10.1 <--> 10.41.10.2 PX4
ground computer (OptiTrack_5G) --SSH, Runtime API--> drone Pi
QGroundControl --telemetry radio--> PX4
```

The lab gateway owns the lab network (DHCP, NAT, internet). Only the relay's
motion-capture subscription joins the lab ROS domain; the stack and PX4 stay in
the provisioned stack domain. See [OptiTrack lab readiness](opti-track-lab-readiness.md)
for the lab facts, commissioning, and acceptance.
