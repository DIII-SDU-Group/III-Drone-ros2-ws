# Pi, PX4, and HIL Links

The Pi uses its dedicated PX4 Ethernet connection at `10.41.10.1/24`; the PX4
endpoint is normally `10.41.10.2`.  The workstation--Pi HIL link is separate
and is used for the HIL simulator, OptiTrack workstation traffic, and normal
developer SSH.

These address assignments preserve a clear test topology. They do not impose
an access policy or firewall restriction. For HIL, use
`iii px4 inspect --host <pi> --profile hil` to confirm the Pi-side address and
route plus DDS UDP `8889` and MAVLink UDP `14542` listeners. This inspection is
read-only.

The Pi USB Ethernet adapter is dual-mode: it retains `10.42.0.15/24` for a
direct workstation link and is a DHCP client when attached to a router or a
computer providing DHCP. A workstation configured only as a DHCP client does
not provide an address; use `10.42.0.1/24` locally in that direct-link case.

The PX4's Ethernet transport is a separate, one-time PX4 parameter baseline;
follow [PX4 HIL Ethernet Baseline](px4-hil-ethernet-baseline.md) before
expecting any DDS or MAVLink packet from the PX4.
