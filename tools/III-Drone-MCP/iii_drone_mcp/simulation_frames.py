from __future__ import annotations

import math


# PX4 local NED is converted back to ROS ENU by the runtime adapters. With the
# current Gazebo bridge, that leaves ROS world XY rotated -90 degrees from
# Gazebo world XY. This position-frame transform is independent of estimator
# heading and must not be inferred from vehicle attitude.
GAZEBO_TO_ROS_POSITION_YAW_RAD = -math.pi / 2.0


def rotate_gazebo_xy_delta_to_ros(dx: float, dy: float) -> tuple[float, float]:
    return float(dy), -float(dx)
