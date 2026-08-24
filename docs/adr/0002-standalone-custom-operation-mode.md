# Standalone Custom Operation Mode

Direct operator and agent commands will run through a standalone `CustomOperationMode` executable in `III-Drone-Mission`, implemented as its own `px4_ros2::ModeBase` rather than through the mission executor's mode provider. PX4/px4_ros2 mode activation is the handoff boundary: the custom operation mode publishes setpoints only while active, while mission-owned modes publish only when their own PX4 mode is active. This avoids a second setpoint arbiter and keeps direct operation independent from mission behavior-tree sequencing.
