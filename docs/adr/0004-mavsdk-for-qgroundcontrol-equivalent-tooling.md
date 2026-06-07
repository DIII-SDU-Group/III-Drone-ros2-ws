# MAVSDK For QGroundControl-Equivalent Tooling

Programmatic equivalents of QGroundControl actions and status will use MAVLink/MAVSDK rather than the ROS control path. Arm, disarm, mode selection, landing, and PX4 command telemetry are operator/autopilot interactions and should remain available even when ROS graph state is incomplete. ROS remains the primary path for III runtime control and direct operation maneuvers, while MAVSDK provides the independent PX4 command and status surface.
