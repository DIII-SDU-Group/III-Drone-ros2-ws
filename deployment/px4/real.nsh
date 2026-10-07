# PX4 real-aircraft (real) transport baseline
#
# Run this in the PX4 NSH console only while the aircraft is disarmed and has
# no propulsion battery connected, or apply it with
# `iii px4 param-baseline --profile real`. III deployment never applies it
# automatically. It saves the parameters and reboots the flight controller;
# the transport selections take effect only after that reboot.
#
# This baseline is the transport only. The estimator, the failsafes and the
# tuning of the real aircraft are set in the field and are not touched here.
#
# Pi PX4-facing interface: 10.41.10.1
# PX4 Ethernet endpoint:   10.41.10.2
#
# The uXRCE-DDS client uses the Pi MicroXRCEAgent at UDP 8888 and MAVLink
# instance 2 broadcasts to UDP 14540, the real/opti_track Runtime API
# endpoint. The HIL baseline (hil-ethernet.nsh) uses 8889 and 14542 instead.

# uXRCE-DDS over Ethernet (UXRCE_DDS_CFG 1000 = Ethernet).
param set UXRCE_DDS_CFG 1000
# 10.41.10.1 as the int32 PX4 expects.
param set UXRCE_DDS_AG_IP 170461697
param set UXRCE_DDS_PRT 8888
# Must equal the stack ROS domain provisioned on the Pi (iii_ros_domain_id,
# default 42; ROS_DOMAIN_ID in /etc/iii/runtime.env).
param set UXRCE_DDS_DOM_ID 42
# No agent time sync, as qualified in HIL.
param set UXRCE_DDS_SYNCT 0

# MAVLink instance 2 over Ethernet to the Pi. The telemetry radio's MAVLink
# instance, used by QGroundControl, is deliberately left untouched.
param set MAV_2_CONFIG 1000
param set MAV_2_UDP_PRT 14540
param set MAV_2_REMOTE_PRT 14540
param set MAV_2_BROADCAST 1

param save

# The transport selections take effect after an FMU reboot.
reboot
