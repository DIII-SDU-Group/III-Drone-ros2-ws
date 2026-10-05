"""Plain frame records of the powerline_slam pipeline -> ROS messages (no estimator logic here)."""

from __future__ import annotations

import json

from builtin_interfaces.msg import Time
from geometry_msgs.msg import Point, Quaternion, Vector3
from iii_drone_interfaces.msg import Powerline, SingleLine, StringStamped


def to_time(source_time_ns: int) -> Time:
    value = int(source_time_ns)
    return Time(sec=value // 1_000_000_000, nanosec=value % 1_000_000_000)


def powerline_message(record: dict) -> Powerline:
    """``iii_drone_interfaces/Powerline`` from a frame record's ``powerline`` content (drone frame, source-time stamps)."""
    message = Powerline()
    message.stamp = to_time(record["stamp_ns"])
    for line in record["lines"]:
        single = SingleLine()
        single.header.stamp = to_time(line["stamp_ns"])
        single.header.frame_id = line["frame_id"]
        single.pose.position = Point(x=line["position"][0], y=line["position"][1], z=line["position"][2])
        x, y, z, w = line["orientation_xyzw"]
        single.pose.orientation = Quaternion(x=x, y=y, z=z, w=w)
        single.projected_position = Point(x=line["projected_position"][0], y=line["projected_position"][1],
                                          z=line["projected_position"][2])
        single.id = int(line["id"])
        single.in_field_of_view = bool(line["in_field_of_view"])
        message.lines.append(single)
    plane = record["projection_plane"]
    message.projection_plane.point = Point(x=plane["point"][0], y=plane["point"][1], z=plane["point"][2])
    message.projection_plane.normal = Vector3(x=plane["normal"][0], y=plane["normal"][1], z=plane["normal"][2])
    return message


def string_stamped(source_time_ns: int, payload) -> StringStamped:
    message = StringStamped()
    message.stamp = to_time(source_time_ns)
    message.data = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                                                      default=str)
    return message
