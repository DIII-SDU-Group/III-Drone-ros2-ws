# PX4 OptiTrack lab (opti_track) baseline
#
# Run this in the PX4 NSH console only while the aircraft is disarmed and has
# no propulsion battery connected. It is intentionally a PX4-side operation:
# III deployment never applies it automatically. It saves the parameters and
# reboots the flight controller; the transport and estimator selections take
# effect only after that reboot.
#
# Names, types, units, and enum values are checked against the pinned
# PX4-Autopilot submodule (deps/submodule-lock.txt, 2a2dc912).
#
# Pi PX4-facing interface: 10.41.10.1
# PX4 Ethernet endpoint:   10.41.10.2
#
# The uXRCE-DDS client uses the Pi MicroXRCEAgent at UDP 8888 and MAVLink
# instance 2 broadcasts to UDP 14540, the real/opti_track Runtime API
# endpoint. The HIL baseline (hil-ethernet.nsh) uses 8889 and 14542 instead;
# rerun it before HIL work on this flight controller.

# uXRCE-DDS over Ethernet (UXRCE_DDS_CFG 1000 = Ethernet).
param set UXRCE_DDS_CFG 1000
# 10.41.10.1 as the int32 PX4 expects.
param set UXRCE_DDS_AG_IP 170461697
param set UXRCE_DDS_PRT 8888
# Must equal the stack ROS domain provisioned on the Pi (iii_ros_domain_id,
# default 42; ROS_DOMAIN_ID in /etc/iii/runtime.env).
param set UXRCE_DDS_DOM_ID 42
# No agent time sync, as qualified in HIL: the pose relay sends timestamp 0,
# so PX4 stamps each vision sample on arrival and EKF2_EV_DELAY below models
# the capture-to-arrival latency.
param set UXRCE_DDS_SYNCT 0

# MAVLink instance 2 over Ethernet to the Pi. The telemetry radio's MAVLink
# instance, used by QGroundControl, is deliberately left untouched.
param set MAV_2_CONFIG 1000
param set MAV_2_UDP_PRT 14540
param set MAV_2_REMOTE_PRT 14540
param set MAV_2_BROADCAST 1

# EKF2 positions from OptiTrack external vision; the barometer is the height
# backup. EKF2_EV_CTRL bits: 0 horizontal position, 1 vertical position,
# 3 yaw (= 11). EKF2_HGT_REF 3 = Vision. EKF2_MAG_TYPE 5 = None.
param set EKF2_EV_CTRL 11
param set EKF2_HGT_REF 3
param set EKF2_GPS_CTRL 0
param set EKF2_BARO_CTRL 1
param set EKF2_MAG_TYPE 5
param set SYS_HAS_MAG 0
param set SYS_HAS_GPS 0
# Noise mode 0: the relay's reported variances, lower-bounded by EKF2_EVP_NOISE
# (m) and EKF2_EVA_NOISE (rad; 0.05 is its minimum).
param set EKF2_EV_NOISE_MD 0
param set EKF2_EVP_NOISE 0.05
param set EKF2_EVA_NOISE 0.05
param set EKF2_EV_QMIN 0
# Initial estimate (ms) of the Motive capture to PX4 arrival latency through the
# lab gateway, Wi-Fi, the relay, and Ethernet. Tune it from flight logs.
param set EKF2_EV_DELAY 30
# Maximum dead-reckoning time without vision, in microseconds (1 s).
param set EKF2_NOAID_TOUT 1000000

# Lab failsafes with a safety pilot on RC. NAV_RCL_ACT 3 = Land mode;
# COM_POSCTL_NAVL 0 = Altitude mode on position loss in Position mode;
# COM_POS_FS_EPH in m (PX4 invalidates position at 2.5x this value);
# MIS_TAKEOFF_ALT in m; COM_LOW_BAT_ACT 2 = Land mode; BAT1_N_CELLS 6 = 6S.
param set NAV_RCL_ACT 3
param set COM_POSCTL_NAVL 0
param set COM_POS_FS_EPH 1.0
param set MIS_TAKEOFF_ALT 1.2
param set COM_LOW_BAT_ACT 2
param set BAT1_N_CELLS 6

# Geofence. TODO: fill both limits from the measured cage before enabling these
# lines. The limits are distances from Home (the takeoff point); 0 disables a
# limit. GF_ACTION 2 = Hold mode.
# param set GF_MAX_HOR_DIST <m from the takeoff point to the nearest net, minus margin>
# param set GF_MAX_VER_DIST <m from the floor to the lowest ceiling point, minus margin>
# param set GF_ACTION 2

# MPC_THR_HOVER is deliberately not set here: measure it from a steady hover
# for each payload configuration (with and without the payload). PX4 restores
# it on every disarm, so every takeoff starts from it.

param save

# The transport and estimator selections take effect after an FMU reboot.
reboot
