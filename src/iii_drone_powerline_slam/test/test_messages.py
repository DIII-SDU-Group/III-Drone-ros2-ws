"""Frame record -> ROS message mapping (no estimator, no ROS graph)."""

import json
import math

from iii_drone_powerline_slam.messages import powerline_message, string_stamped, to_time


def _record(lines):
    return {"stamp_ns": 151_940_000_123, "lines": lines,
            "projection_plane": {"point": [0.0, 0.0, 0.0],
                                 "normal": [0.0, 1.0, 0.0] if lines else [1.0, 0.0, 0.0]}}


def _line(ident, y, z):
    h = math.sqrt(0.5)
    return {"id": ident, "frame_id": "drone", "stamp_ns": 151_940_000_123, "position": [0.0, y, z],
            "orientation_xyzw": [0.0, 0.0, h, h], "projected_position": [0.0, y, z],
            "in_field_of_view": ident % 2 == 0, "physical_id": ident % 1000 - 1, "lifecycle": "CONFIRMED"}


def test_to_time_splits_source_time_exactly():
    stamp = to_time(1_234_567_890_123)
    assert (stamp.sec, stamp.nanosec) == (1234, 567_890_123)
    stamp = to_time(999_999_999)
    assert (stamp.sec, stamp.nanosec) == (0, 999_999_999)


def test_powerline_message_maps_every_line_field():
    message = powerline_message(_record([_line(1_001_001, -1.5, 3.25), _line(1_001_002, 1.5, 3.5)]))
    assert (message.stamp.sec, message.stamp.nanosec) == (151, 940_000_123)
    assert [line.id for line in message.lines] == [1_001_001, 1_001_002]
    first, second = message.lines
    assert first.header.frame_id == "drone"
    assert (first.header.stamp.sec, first.header.stamp.nanosec) == (151, 940_000_123)
    assert (first.pose.position.x, first.pose.position.y, first.pose.position.z) == (0.0, -1.5, 3.25)
    assert (first.projected_position.y, first.projected_position.z) == (-1.5, 3.25)
    assert (first.pose.orientation.z, first.pose.orientation.w) == (math.sqrt(0.5), math.sqrt(0.5))
    assert (first.in_field_of_view, second.in_field_of_view) == (False, True)
    assert (message.projection_plane.normal.x, message.projection_plane.normal.y) == (0.0, 1.0)


def test_fail_closed_record_publishes_no_lines_and_the_default_plane():
    message = powerline_message(_record([]))
    assert list(message.lines) == []
    normal = message.projection_plane.normal
    assert (normal.x, normal.y, normal.z) == (1.0, 0.0, 0.0)


def test_string_stamped_is_compact_sorted_json_at_source_time():
    message = string_stamped(42, {"b": 1, "a": [1, 2]})
    assert message.data == '{"a":[1,2],"b":1}'
    assert json.loads(message.data) == {"a": [1, 2], "b": 1}
    assert (message.stamp.sec, message.stamp.nanosec) == (0, 42)
    assert string_stamped(7, "Running").data == "Running"
