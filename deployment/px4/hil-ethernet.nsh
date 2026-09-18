# PX4 HIL Ethernet transport baseline
#
# Run this in the PX4 NSH console only while the aircraft is disarmed and has
# no propulsion battery connected. It is intentionally a PX4-side operation:
# III deployment never applies it automatically.
#
# Pi PX4-facing interface: 10.41.10.1
# PX4 Ethernet endpoint:   10.41.10.2
#
# The UXRCE client uses the Pi MicroXRCEAgent at UDP 8889. MAVLink broadcasts
# its normal telemetry to UDP 14542 so the Pi runtime can receive it without
# a prior packet from the Pi.

param set UXRCE_DDS_CFG 1000
param set UXRCE_DDS_AG_IP 170461697
param set UXRCE_DDS_PRT 8889

param set MAV_2_CONFIG 1000
param set MAV_2_UDP_PRT 14542
param set MAV_2_REMOTE_PRT 14542
param set MAV_2_BROADCAST 1

param save

# These transport-selection parameters take effect after an FMU reboot.
reboot
