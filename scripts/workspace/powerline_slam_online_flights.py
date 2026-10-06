#!/usr/bin/env python3
"""Fly the powerline SLAM corridor flights with the powerline_slam backend consuming the live streams.

The flight exercise of ``powerline_slam_flights.py`` (same catalog, same legs, same maneuver-controller fly-to
commands) on the canonical system booted with ``/perception/processing_stack: powerline_slam``.  Nothing is recorded
and nothing reads the backend's output: this driver only moves the simulated aircraft.

Differences from the recording driver:

* start order: ``custom_operation`` with its dependencies (the PX4 bridge, the maneuver controller and the mission
  executor, which fly the commanded legs), then ``powerline_slam``.  On the powerline_slam processing stack none of
  them has a lifecycle or data dependency on the backend, and the legacy mapper they would read is not instantiated;
* no legacy mapper is started or sampled, no ground segment and no bag;
* every leg command is announced on ``/perception/powerline_slam/nominal_command`` (``StringStamped`` JSON, latched):
  ``intent`` names the source time, 0.5 s ahead, at which the command will be sent, ``accepted`` follows once the
  maneuver controller accepted it, and ``end`` names the source time at which the exercise ends.  These are the backend's live nominal mission prior
  (it carries the command only: target pose in the Gazebo world ENU design frame and the source time of issuing it).

With ``--traversal-epochs`` (TRAVERSAL_EPOCH_v1 of the backend) every flight is one traversal instance: ``--flights``
may repeat a direction, instance ``NN_<direction>`` has its own directory, and after the flight's post-roll -- its last
prescribed maneuver has completed and the aircraft holds -- a ``traversal_complete`` event is published on the command
topic (its source time is the simulation time of publication; it names nothing but the instance).  The next flight
starts when the harness names the instance in ``--between-flights-file``; this driver never reads the backend.

Per flight the run directory holds ``flight_plan.json``, ``mission_phase_evidence.json``, ``trajectory.json``,
``command_events.json`` and ``verification.json``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import perception_dataset_flights as dataset  # noqa: E402
import powerline_slam_flights as flights  # noqa: E402

COMMAND_TOPIC = "/perception/powerline_slam/nominal_command"
MAP_FRAME = "WO002_SIM_DESIGN_ENU"
R_ENU_FROM_NED = ((0.0, 1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, -1.0))
R_FLU_FROM_FRD = ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0))
# The flight path first (PX4 bridge, maneuver controller, mission executor, external flight mode), then the backend,
# so the backend activates when every runtime input is already flowing.
COMMAND_LEAD_NS = 500_000_000      # an intent or end event names a source time this far ahead of its publication
SCOPED_ENTITIES = ("configuration_server", "custom_operation", "powerline_slam")
DEFAULT_OUTPUT_ROOT = flights.WORKSPACE_ROOT / "runtime" / "powerline_slam_online"


def matmul(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def command_event(kind: str, source_time_ns: int, leg: dict[str, Any] | None = None) -> dict[str, Any]:
    """One command event; the pose convention is that of powerline_slam_runtime_inputs.build_mission_priors."""
    event: dict[str, Any] = {"kind": kind, "leg": None if leg is None else leg["name"], "source_time_ns": int(source_time_ns)}
    if leg is not None:
        command = leg["planned_command"]
        c, s = math.cos(float(command["yaw_rad"])), math.sin(float(command["yaw_rad"]))
        ned_from_frd = ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))
        event.update(map_frame_id=MAP_FRAME, commanded_position_map_m=[float(v) for v in command["target_world_m"]],
                     rotation_map_from_drone=matmul(matmul(R_ENU_FROM_NED, ned_from_frd), R_FLU_FROM_FRD))
    return event


class CommandPublisher:
    """Latched publisher of the exercise's command events, on a node of its own."""

    def __init__(self) -> None:
        import rclpy
        from iii_drone_interfaces.msg import StringStamped
        from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

        self._message = StringStamped
        self._node = rclpy.create_node("powerline_slam_exercise_commands")
        self._publisher = self._node.create_publisher(StringStamped, COMMAND_TOPIC, QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL,
            history=HistoryPolicy.KEEP_LAST, depth=8))
        self.events: list[dict[str, Any]] = []

    def publish(self, event: dict[str, Any]) -> None:
        message = self._message()
        message.stamp.sec, message.stamp.nanosec = divmod(int(event["source_time_ns"]), 1_000_000_000)
        message.data = json.dumps(event)
        self._publisher.publish(message)
        self.events.append({**event, "published_wall_time": flights.utc_now(), "published_monotonic_s": time.monotonic()})

    def close(self) -> None:
        self._node.destroy_node()


class OnlineRunner(flights.CorridorRunner):
    """The corridor exercise for a system running the powerline_slam backend: commands announced, nothing recorded."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.skip_backend = bool(kwargs.pop("skip_backend", False))
        self.traversal_epochs = bool(kwargs.pop("traversal_epochs", False))
        self.backend_first_s = float(kwargs.pop("backend_first_s", 0.0))
        super().__init__(*args, **kwargs)
        self.commands: CommandPublisher | None = None
        self._tf: tuple | None = None               # (tools node, buffer, listener) of the trajectory log
        self._leaked_tf_listeners_released = 0

    def ensure_ready(self) -> None:
        status = self.tools.simulation("status")
        stdout = str((status.data or {}).get("stdout", ""))
        running = next((line.split(":", 1)[1].strip() for line in stdout.splitlines()
                        if line.startswith("px4_sim_model_running:")), "none")
        if running != flights.SIM_MODEL:
            command = "restart" if running != "none" else "start"
            self.require(self.tools.simulation(command, headless=self.headless, sim_model=flights.SIM_MODEL,
                                               ready_timeout_sec=240), f"{command} simulation with {flights.SIM_MODEL}")
        self.clock = flights.SimulationClock({})
        self.commands = CommandPublisher()
        system = self.tools.system("status")
        if not system.success or "booted: false" in str((system.data or {}).get("stdout", "")).lower():
            self.require(self.tools.system("boot", timeout_sec=180), "boot canonical system")
        order = list(SCOPED_ENTITIES)
        if self.backend_first_s > 0:
            # start-up test: the backend is activated before the PX4 bridge and the flight path exist, and has this long
            # on its own (the accepted behaviour is to fail closed: STARTUP_TIMEOUT)
            order = ["configuration_server", "powerline_slam", "custom_operation"]
        for entity in order:
            if entity == "custom_operation" and self.backend_first_s > 0:
                time.sleep(self.backend_first_s)
            if entity == "powerline_slam" and self.skip_backend:      # simulation-only baseline: the backend stays down,
                entity = "sim_assets"                                 # its sensor bridges (and /clock) still run
            self.require(self.tools.system("start", entity_id=entity, include_dependencies=True, timeout_sec=300),
                         f"start {entity} with its dependencies")
        state = self.require(self.tools.px4("status", timeout_sec=20), "read PX4 status")
        if not bool(state.get("in_air")):
            if not bool(state.get("armed")):
                self.require(self.tools.px4("arm", timeout_sec=75, postcondition_timeout_sec=30, health_stable_sec=2.0),
                             "PX4 arm")
            self.require(self.tools.px4("takeoff", timeout_sec=90, postcondition_timeout_sec=60, min_altitude_m=1.5),
                         "PX4 takeoff")
        self.px4_with_retries("hold", "stabilize PX4 in HOLD", timeout_sec=20)
        time.sleep(2.0)
        self.activate_custom_operation_with_recovery()
        for attempt in range(3):                    # the tools' node may not have discovered the server yet
            try:
                current = self.require(self.tools.configuration("get_yaml", timeout_sec=20), "snapshot configuration")
                break
            except RuntimeError as exc:
                if attempt == 2:
                    raise
                self.rebuild_tools(f"snapshot configuration: {exc}")
                time.sleep(3.0)
        parsed = self.yaml.safe_load(str(current.get("yaml", ""))) or {}
        for parameter_name in dataset.MOTION_PARAMETER_NAMES.values():
            value = dataset._nested_parameter_value(parsed, parameter_name)
            if value is None:
                raise RuntimeError(f"motion parameter missing from configuration snapshot: {parameter_name}")
            self._original_motion[parameter_name] = value
        flights.write_json(self.run_dir / "runtime/original_motion_configuration.json", self._original_motion)
        self.apply_motion_profile(dataset.MotionProfile(**self.catalog["motion_profile"]))

    def start_pl_mapper_with_retries(self, context: str) -> None:
        """The legacy mapper is not part of the powerline_slam processing stack."""

    def _world_drone_pose(self) -> dict[str, Any]:
        """The world->drone transform from one transform listener kept for the whole run.

        The tools' own lookup creates a new listener on every call and never releases it; over a run of several
        traversals the accumulated /tf subscriptions slow this process, the transform broadcasters and with them the
        whole simulation.  This lookup only reads; it does not change what is flown."""
        import rclpy
        from rclpy.time import Time
        from tf2_ros import Buffer, TransformException, TransformListener
        node = self.tools.node
        if self._tf is None or self._tf[0] is not node:          # first use, or the tools were rebuilt on a new node
            buffer = Buffer()
            self._tf = (node, buffer, TransformListener(buffer, node, spin_thread=False))
        buffer = self._tf[1]
        error: Exception | None = None
        for _ in range(40):                                       # take what has arrived, then read the newest transform
            rclpy.spin_once(node, timeout_sec=0.0)
        for _ in range(20):
            try:
                transform = buffer.lookup_transform("world", "drone", Time())
                t, r = transform.transform.translation, transform.transform.rotation
                yaw = math.atan2(2.0 * (r.w * r.z + r.x * r.y), 1.0 - 2.0 * (r.y * r.y + r.z * r.z))
                return {"x": t.x, "y": t.y, "z": t.z, "yaw": yaw}
            except TransformException as exc:
                error = exc
                rclpy.spin_once(node, timeout_sec=0.1)
        raise TimeoutError(f"no world->drone transform: {error}")

    def _release_leaked_tf_listeners(self) -> int:
        """Destroy the /tf subscriptions that the tools' own lookups left on their node (all but this run's listener)."""
        node = self.tools.node
        mine = set() if self._tf is None or self._tf[0] is not node else {self._tf[2].tf_sub, self._tf[2].tf_static_sub}
        leaked = [sub for sub in list(node.subscriptions) if sub.topic_name in ("/tf", "/tf_static") and sub not in mine]
        for sub in leaked:
            node.destroy_subscription(sub)
        return len(leaked)

    def sample(self, samples: list[dict[str, Any]], *, phase: str, target_index: int | None) -> None:
        assert self.clock is not None
        try:                                        # the trajectory log is evidence only: never fatal to the exercise
            pose = self._world_drone_pose()
        except Exception as exc:  # noqa: BLE001
            pose = {"pose_error": str(exc)[:200]}
        samples.append({"t": time.time(), "source_time_ns": self.clock.now_ns(), "phase": phase,
                        "target_index": target_index, **pose})

    def on_leg_command(self, kind: str, leg: dict[str, Any], begin_ns: int) -> int:
        """Announce the leg command.  The intent names a source time COMMAND_LEAD_NS ahead and the command is sent
        when the simulation reaches it, so the announcement arrives before any sensor data of that instant."""
        assert self.commands is not None and self.clock is not None
        if kind == "intent":
            try:
                self._leaked_tf_listeners_released += self._release_leaked_tf_listeners()
            except Exception:  # noqa: BLE001 - housekeeping only
                pass
            begin_ns = self.clock.now_ns() + COMMAND_LEAD_NS
            self.commands.publish(command_event(kind, begin_ns, leg))
            self.clock.sleep_until_ns(begin_ns)
        else:
            self.commands.publish(command_event(kind, begin_ns, leg))
        return begin_ns

    def run_flight(self, direction: str, flight_dir: Path, instance: str | None = None) -> dict[str, Any]:
        assert self.clock is not None and self.commands is not None
        started_at = flights.utc_now()
        staging = self.resolve_waypoint(dataset.Waypoint(flights.STAGING_FIXTURE, "staging", hold_sec=1.0))
        mapping = staging["fixture_resolution"]["live_mapping"]
        if not self._geometry_mapped_to_live_ros:
            import dataclasses
            mapped = dataset.map_geometry_data_to_live_ros(self.geometry.data, mapping)
            self.geometry = dataclasses.replace(self.geometry, data=mapped)
            self._geometry_mapped_to_live_ros = True
        plan = flights.plan_flight(self.catalog, direction, mapping)
        flights.write_json(flight_dir / "flight_plan.json", {"direction": direction, "live_mapping": mapping,
                                                             "staging": staging, "legs": plan})
        self.activate_custom_operation_with_recovery()
        self.reposition({**staging, "z": plan[0]["live_target"]["z"]})
        samples: list[dict[str, Any]] = []
        begins: list[int] = []
        self._leg_attempts = {}
        first_event = len(self.commands.events)
        try:
            self.clock.sleep_until_ns(self.clock.now_ns() + int(flights.PRE_ROLL_SEC * 1e9))
            for index, leg in enumerate(plan):
                begins.append(self.fly_leg(samples, leg, index))
            end_ns = self.clock.now_ns() + COMMAND_LEAD_NS
            self.commands.publish(command_event("end", end_ns))
            self.clock.sleep_until_ns(end_ns + int(flights.POST_ROLL_SEC * 1e9))
            if self.traversal_epochs:                   # the last prescribed maneuver has completed: the traversal is over
                self.commands.publish({"kind": "traversal_complete", "leg": None, "source_time_ns": self.clock.now_ns(),
                                       "traversal": instance or direction})
        except Exception:
            try:                                    # the exercise ended here: no prior stays in force
                self.commands.publish(command_event("end", self.clock.now_ns() + COMMAND_LEAD_NS))
            finally:
                self.safe_recover()
            raise
        evidence = flights.mission_phase_evidence(direction, plan, begins, end_ns, self.catalog_sha256)
        flights.write_json(flight_dir / "mission_phase_evidence.json", evidence)
        flights.write_json(flight_dir / "trajectory.json", {"samples": samples})
        flights.write_json(flight_dir / "command_events.json", {"topic": COMMAND_TOPIC,
                                                                "events": self.commands.events[first_event:]})
        result = {"status": "passed", "direction": direction, "sim_model": flights.SIM_MODEL, "started_at": started_at,
                  "completed_at": flights.utc_now(), "leg_count": len(plan), "source_time_span_ns": [begins[0], end_ns],
                  "leg_command_attempts": dict(self._leg_attempts), "tools_rebuilds": list(self.tools_rebuilds),
                  "scoped_entities": list(SCOPED_ENTITIES), "recorded": False, "instance": instance,
                  "leaked_tf_listeners_released": self._leaked_tf_listeners_released,
                  "traversal_complete_source_time_ns": next((e["source_time_ns"] for e in reversed(self.commands.events)
                                                             if e["kind"] == "traversal_complete"), None) if self.traversal_epochs else None}
        flights.write_json(flight_dir / "verification.json", result)
        return result

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self.commands is not None:
                self.commands.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--flights", nargs="+", default=["a_to_b", "b_to_a"])
    parser.add_argument("--run-id", default=dt.datetime.now().strftime("powerline_slam_online_%Y%m%d_%H%M%S"))
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--geometry", type=Path, default=dataset.DEFAULT_GEOMETRY_PATH)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--keep-running", action="store_true")
    parser.add_argument("--skip-backend", action="store_true",
                        help="do not start powerline_slam: the same exercise as a simulation-only baseline")
    parser.add_argument("--backend-first-s", type=float, default=0.0,
                        help="start-up test: start powerline_slam before the flight path and wait this long before starting it")
    parser.add_argument("--traversal-epochs", action="store_true",
                        help="one traversal instance per flight (directions may repeat); publish traversal_complete after each")
    parser.add_argument("--between-flights-file", type=Path, default=None,
                        help="after each flight, wait until this file names the flight (a harness scores it first)")
    args = parser.parse_args(argv)
    catalog = flights.load_catalog()
    unknown = [name for name in args.flights if name not in catalog["flights"]]
    if unknown:
        raise SystemExit(f"unknown flights: {unknown}; known: {sorted(catalog['flights'])}")
    # traversal instances: NN_<direction>, so a direction may be flown repeatedly in one run
    instances = [(f"{i + 1:02d}_{d}" if args.traversal_epochs else d, d) for i, d in enumerate(args.flights)]
    run_dir = args.output_root / args.run_id
    if run_dir.exists():
        raise SystemExit(f"run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    manifest: dict[str, Any] = {"schema": "iii.powerline-slam-online-flight-run/v1", "run_id": args.run_id,
                                "created_at": flights.utc_now(), "sim_model": flights.SIM_MODEL,
                                "catalog": {"path": str(flights.CATALOG_PATH), "sha256": flights.sha256_file(flights.CATALOG_PATH)},
                                "command_topic": COMMAND_TOPIC, "scoped_entities": list(SCOPED_ENTITIES),
                                "traversal_epochs": bool(args.traversal_epochs), "instances": [name for name, _ in instances],
                                "flights": {}}
    flights.write_json(run_dir / "run_manifest.json", manifest)
    runner = OnlineRunner(run_dir, args.geometry, catalog, headless=args.headless, keep_running=args.keep_running,
                          skip_backend=args.skip_backend, traversal_epochs=args.traversal_epochs,
                          backend_first_s=args.backend_first_s)
    failures = []
    try:
        runner.ensure_ready()
        flights.write_json(run_dir / "ready.json", {"ready_at": flights.utc_now()})
        for name, direction in instances:
            flight_dir = run_dir / name
            try:
                manifest["flights"][name] = runner.run_flight(direction, flight_dir, name)
            except Exception as exc:  # noqa: BLE001 - recorded per flight, the run continues
                manifest["flights"][name] = {"status": "failed", "direction": direction, "error": str(exc)}
                flights.write_json(flight_dir / "failure.json", {"failed_at": flights.utc_now(), "error": str(exc)})
                failures.append(f"{name}: {exc}")
            flights.write_json(run_dir / "run_manifest.json", manifest)
            flights.write_json(run_dir / f"{name}.done", {"at": flights.utc_now()})
            if args.between_flights_file is not None:
                deadline = time.monotonic() + 900.0
                while time.monotonic() < deadline:
                    if args.between_flights_file.exists() and name in args.between_flights_file.read_text().split():
                        break
                    time.sleep(0.5)
    finally:
        try:
            runner.close()
        except Exception as exc:  # noqa: BLE001
            manifest.setdefault("close_errors", []).append(str(exc))
    manifest["completed_at"] = flights.utc_now()
    manifest["status"] = "flown" if not failures else "failed"
    flights.write_json(run_dir / "run_manifest.json", manifest)
    for failure in failures:
        print(f"FAILED: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
