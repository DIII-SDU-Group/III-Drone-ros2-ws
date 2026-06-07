from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Iterable


Point = dict[str, float]


def point_from_any(value: Any) -> Point:
    return {"x": float(value["x"]), "y": float(value["y"]), "z": float(value["z"])}


def point_to_list(point: Point) -> list[float]:
    return [float(point["x"]), float(point["y"]), float(point["z"])]


def add(a: Point, b: Point) -> Point:
    return {"x": a["x"] + b["x"], "y": a["y"] + b["y"], "z": a["z"] + b["z"]}


def sub(a: Point, b: Point) -> Point:
    return {"x": a["x"] - b["x"], "y": a["y"] - b["y"], "z": a["z"] - b["z"]}


def mul(a: Point, scalar: float) -> Point:
    return {"x": a["x"] * scalar, "y": a["y"] * scalar, "z": a["z"] * scalar}


def dot(a: Point, b: Point) -> float:
    return a["x"] * b["x"] + a["y"] * b["y"] + a["z"] * b["z"]


def norm(a: Point) -> float:
    return math.sqrt(dot(a, a))


def normalize(a: Point) -> Point:
    length = norm(a)
    if length <= 1e-9:
        return {"x": 1.0, "y": 0.0, "z": 0.0}
    return mul(a, 1.0 / length)


def distance(a: Point, b: Point) -> float:
    return norm(sub(a, b))


def segment_projection(point: Point, start: Point, end: Point) -> dict[str, Any]:
    segment = sub(end, start)
    length_sq = dot(segment, segment)
    if length_sq <= 1e-12:
        closest = start
        t = 0.0
    else:
        t = max(0.0, min(1.0, dot(sub(point, start), segment) / length_sq))
        closest = add(start, mul(segment, t))
    return {"t": t, "closest": closest, "distance_m": distance(point, closest)}


def polyline_projection(point: Point, samples: list[Point]) -> dict[str, Any]:
    best: dict[str, Any] | None = None
    for index in range(max(0, len(samples) - 1)):
        candidate = segment_projection(point, samples[index], samples[index + 1])
        candidate["segment_index"] = index
        if best is None or candidate["distance_m"] < best["distance_m"]:
            best = candidate
    if best is None and samples:
        best = {"t": 0.0, "closest": samples[0], "distance_m": distance(point, samples[0]), "segment_index": 0}
    if best is None:
        best = {"t": 0.0, "closest": {"x": math.nan, "y": math.nan, "z": math.nan}, "distance_m": math.inf, "segment_index": 0}
    return best


@dataclass(frozen=True)
class SimulationGeometry:
    path: Path
    data: dict[str, Any]

    @property
    def frame_id(self) -> str:
        return str(self.data.get("frame_id", "world"))

    @property
    def powerlines(self) -> dict[str, Any]:
        return self.data.get("ground_truth", {}).get("powerlines", {})

    @property
    def conductors(self) -> list[dict[str, Any]]:
        return list(self.powerlines.get("conductors", []))

    @property
    def pylons(self) -> dict[str, Any]:
        return self.data.get("ground_truth", {}).get("pylons", {})

    @property
    def aggregate(self) -> dict[str, Any]:
        return self.powerlines.get("aggregate", {})

    @property
    def drone_positions(self) -> list[dict[str, Any]]:
        return list(self.data.get("drone_positions", []))


def load_geometry(workspace_root: Path, geometry_path: str | Path | None = None) -> SimulationGeometry:
    path = Path(geometry_path) if geometry_path else workspace_root / "tools/III-Drone-MCP/config/hca_full_pylon_setup_geometry.json"
    if not path.is_absolute():
        path = workspace_root / path
    return SimulationGeometry(path=path, data=json.loads(path.read_text(encoding="utf-8")))


def conductor_samples(conductor: dict[str, Any]) -> list[Point]:
    return [point_from_any(sample) for sample in conductor.get("samples", [])]


def all_conductor_samples(geometry: SimulationGeometry) -> list[Point]:
    samples: list[Point] = []
    for conductor in geometry.conductors:
        samples.extend(conductor_samples(conductor))
    return samples


def corridor_model(geometry: SimulationGeometry) -> dict[str, Any]:
    aggregate = geometry.aggregate
    origin = point_from_any(aggregate.get("start_average", {"x": 0.0, "y": 0.0, "z": 0.0}))
    end = point_from_any(aggregate.get("end_average", origin))
    span_axis = point_from_any(aggregate.get("span_axis_unit_xy", {"x": 1.0, "y": 0.0, "z": 0.0}))
    span_axis = normalize({"x": span_axis["x"], "y": span_axis["y"], "z": 0.0})
    lateral_axis = {"x": -span_axis["y"], "y": span_axis["x"], "z": 0.0}
    span_range = aggregate.get("span_projection_range_m", {"min": 0.0, "max": 0.0})
    samples = all_conductor_samples(geometry)
    lateral_values = [dot(sub(sample, origin), lateral_axis) for sample in samples] or [0.0]
    z_values = [sample["z"] for sample in samples] or [0.0]
    bbox = aggregate.get("bounding_box", {})
    return {
        "origin": origin,
        "center": mul(add(origin, end), 0.5),
        "span_axis": span_axis,
        "lateral_axis": lateral_axis,
        "span_range_m": {"min": float(span_range.get("min", 0.0)), "max": float(span_range.get("max", 0.0))},
        "lateral_range_m": {"min": min(lateral_values), "max": max(lateral_values)},
        "z_range_m": {"min": min(z_values), "max": max(z_values)},
        "bounding_box": bbox,
    }


def corridor_membership(geometry: SimulationGeometry, point: Point, margin_m: float = 0.5) -> dict[str, Any]:
    model = corridor_model(geometry)
    rel = sub(point, model["origin"])
    span = dot(rel, model["span_axis"])
    lateral = dot(rel, model["lateral_axis"])
    span_range = model["span_range_m"]
    lateral_range = model["lateral_range_m"]
    z_range = model["z_range_m"]
    inside_span = span_range["min"] - margin_m <= span <= span_range["max"] + margin_m
    inside_lateral = lateral_range["min"] - margin_m <= lateral <= lateral_range["max"] + margin_m
    inside_vertical = z_range["min"] - margin_m <= point["z"] <= z_range["max"] + margin_m
    return {
        "inside_powerline_corridor": bool(inside_span and inside_lateral),
        "inside_span": bool(inside_span),
        "inside_lateral": bool(inside_lateral),
        "inside_conductor_vertical_band": bool(inside_vertical),
        "span_coordinate_m": span,
        "lateral_coordinate_m": lateral,
        "vertical_coordinate_m": point["z"],
        "margin_m": margin_m,
        "corridor_model": model,
    }


def nearest_conductor(geometry: SimulationGeometry, point: Point) -> dict[str, Any]:
    best: dict[str, Any] | None = None
    for conductor in geometry.conductors:
        projection = polyline_projection(point, conductor_samples(conductor))
        candidate = {
            "id": conductor.get("id"),
            "distance_m": projection["distance_m"],
            "closest_point": projection["closest"],
            "segment_index": projection["segment_index"],
            "t": projection["t"],
        }
        if best is None or candidate["distance_m"] < best["distance_m"]:
            best = candidate
    return best or {"id": None, "distance_m": math.inf, "closest_point": None}


def compact_conductors(geometry: SimulationGeometry, include_samples: bool = False) -> list[dict[str, Any]]:
    result = []
    for conductor in geometry.conductors:
        item = {
            "id": conductor.get("id"),
            "frame_id": conductor.get("frame_id", geometry.frame_id),
            "start": conductor.get("start"),
            "end": conductor.get("end"),
            "bounding_box": conductor.get("bounding_box"),
            "polyline_length_m": conductor.get("polyline_length_m"),
            "sample_count": conductor.get("sample_count", len(conductor.get("samples", []))),
        }
        if include_samples:
            item["samples"] = conductor.get("samples", [])
        result.append(item)
    return result


def nearest_fixture(geometry: SimulationGeometry, point: Point) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for fixture in geometry.drone_positions:
        pose = fixture.get("pose")
        if not pose:
            continue
        candidate = dict(fixture)
        candidate["distance_m"] = distance(point, point_from_any(pose))
        if best is None or candidate["distance_m"] < best["distance_m"]:
            best = candidate
    return best


def visibility_state(
    geometry: SimulationGeometry,
    drone_pose: dict[str, float],
    *,
    max_range_m: float = 15.0,
    horizontal_fov_rad: float = 1.6,
    upward_cone_rad: float = 1.2,
) -> dict[str, Any]:
    origin = {"x": drone_pose["x"], "y": drone_pose["y"], "z": drone_pose["z"]}
    yaw = float(drone_pose.get("yaw", 0.0))
    visible = []
    for conductor in geometry.conductors:
        projection = polyline_projection(origin, conductor_samples(conductor))
        closest = projection["closest"]
        vector = sub(closest, origin)
        range_m = norm(vector)
        bearing = math.atan2(vector["y"], vector["x"]) - yaw
        bearing = math.atan2(math.sin(bearing), math.cos(bearing))
        horizontal_range = math.hypot(vector["x"], vector["y"])
        elevation = math.atan2(vector["z"], horizontal_range)
        angle_from_up = math.acos(max(-1.0, min(1.0, vector["z"] / range_m))) if range_m > 1e-9 else 0.0
        expected = (
            range_m <= max_range_m
            and abs(bearing) <= horizontal_fov_rad / 2.0
            and angle_from_up <= upward_cone_rad
            and vector["z"] >= 0.0
        )
        visible.append(
            {
                "id": conductor.get("id"),
                "expected_visible": bool(expected),
                "range_m": range_m,
                "bearing_rad": bearing,
                "elevation_rad": elevation,
                "angle_from_up_rad": angle_from_up,
                "closest_point": closest,
            }
        )
    fixture = nearest_fixture(geometry, origin)
    return {
        "frame_id": geometry.frame_id,
        "drone_pose": drone_pose,
        "assumptions": {
            "max_range_m": max_range_m,
            "horizontal_fov_rad": horizontal_fov_rad,
            "upward_cone_rad": upward_cone_rad,
            "occlusion_model": "none",
            "sensor_model": "simple upward cone plus horizontal bearing gate",
        },
        "conductors": visible,
        "expected_visible_conductor_ids": [item["id"] for item in visible if item["expected_visible"]],
        "nearest_fixture": (
            {
                "id": fixture.get("id"),
                "label": fixture.get("label"),
                "distance_m": fixture.get("distance_m"),
                "expected": fixture.get("expected", {}),
            }
            if fixture
            else None
        ),
    }


def decimate(items: list[Any], max_items: int) -> list[Any]:
    if max_items <= 0 or len(items) <= max_items:
        return items
    step = (len(items) - 1) / float(max_items - 1)
    return [items[round(index * step)] for index in range(max_items)]


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return path


def image_metadata(path: Path) -> dict[str, Any]:
    metadata = {"artifact_path": str(path), "size_bytes": path.stat().st_size if path.exists() else 0}
    try:
        from PIL import Image

        with Image.open(path) as image:
            metadata.update({"width": image.width, "height": image.height, "mode": image.mode, "bbox": image.getbbox()})
    except Exception as exc:
        metadata["image_metadata_error"] = str(exc)
    return metadata


def conductor_height_range(geometry: SimulationGeometry) -> dict[str, float]:
    samples = all_conductor_samples(geometry)
    if not samples:
        return {"min": math.nan, "max": math.nan}
    return {"min": min(sample["z"] for sample in samples), "max": max(sample["z"] for sample in samples)}
