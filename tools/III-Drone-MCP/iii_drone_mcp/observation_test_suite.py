from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys
import time
from typing import Any


EXPECTED_TOOLS = {
    "sim.geometry_state",
    "sim.visibility_state",
    "sim.trajectory_state",
    "sim.render_snapshot",
    "sim.render_snapshot_set",
    "sim.plot_state",
    "sim.observe_window",
    "sim.observe_active_goal",
    "sim.observation_timeline",
    "sim.perception_verdict",
    "operation.activate",
    "operation.start",
    "operation.start_fly_relative",
    "operation.start_fly_to_position",
    "operation.start_hover",
    "operation.goal_status",
    "operation.goal_feedback",
    "operation.goal_result",
    "operation.wait_goal",
    "operation.cancel_goal",
    "operation.cancel_all",
    "operation.active",
    "operation.safety_stop",
    "operation.list_goals",
    "operation.goal_registry_status",
    "operation.clear_completed_goals",
    "operation.prune_goals",
    "operation.discover_active_goals",
}

DEFAULT_PHASES = [
    "static",
    "geometry",
    "offline",
    "snapshots",
    "rendered-e2e",
    "headless-e2e",
    "failure-modes",
    "audit",
]


class TestFailure(RuntimeError):
    pass


class ObservationTestSuite:
    def __init__(self, *, workspace_root: Path, artifact_root: Path, keep_environment: bool = False) -> None:
        self.workspace_root = workspace_root
        self.mcp_root = workspace_root / "tools/III-Drone-MCP"
        self.artifact_root = artifact_root
        self.keep_environment = keep_environment
        self.summary: list[dict[str, Any]] = []

    def run(self, phases: list[str]) -> None:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        for phase in phases:
            started = time.monotonic()
            try:
                getattr(self, f"phase_{phase.replace('-', '_')}")()
                self.summary.append({"phase": phase, "success": True, "duration_sec": time.monotonic() - started})
                print(f"PASS {phase}")
            except Exception as exc:
                self.summary.append(
                    {
                        "phase": phase,
                        "success": False,
                        "duration_sec": time.monotonic() - started,
                        "message": str(exc),
                    }
                )
                self._write_summary()
                if not self.keep_environment:
                    self._best_effort_cleanup()
                raise
            finally:
                self._write_summary()

    def phase_static(self) -> None:
        for relative in [
            "iii_drone_mcp/agent_tools.py",
            "iii_drone_mcp/mcp_server.py",
            "iii_drone_mcp/mcp_call.py",
            "iii_drone_mcp/mcp_batch.py",
            "iii_drone_mcp/simulation_observation.py",
            "iii_drone_mcp/observation_test_suite.py",
        ]:
            py_compile.compile(str(self.mcp_root / relative), doraise=True)

        for relative in [
            "config/e2e_smoke_batch.json",
            "config/hca_full_pylon_setup_geometry.json",
            "config/offline_observation_tests.json",
            "config/rendered_observation_e2e.json",
            "config/headless_observation_e2e.json",
            "config/nonblocking_operation_e2e.json",
            "config/single_active_operation_policy_e2e.json",
            "config/safety_stop_e2e.json",
        ]:
            path = self.mcp_root / relative
            with path.open(encoding="utf-8") as handle:
                json.load(handle)

        from iii_drone_mcp.agent_tools import DroneAgentTools
        from iii_drone_mcp.mcp_call import tools_as_specs

        tools = DroneAgentTools(artifact_dir=self.artifact_root / "static")
        try:
            available = set(tools_as_specs(tools).keys())
        finally:
            tools.close()
        missing = sorted(EXPECTED_TOOLS - available)
        if missing:
            raise TestFailure(f"missing MCP tools: {', '.join(missing)}")
        self._write_json("static/tool_registry.json", {"expected": sorted(EXPECTED_TOOLS), "available": sorted(available)})

    def phase_geometry(self) -> None:
        from iii_drone_mcp.simulation_observation import (
            all_conductor_samples,
            conductor_samples,
            corridor_membership,
            corridor_model,
            load_geometry,
            nearest_conductor,
            visibility_state,
        )

        geometry = load_geometry(self.workspace_root)
        conductors = geometry.conductors
        self._require(len(conductors) == 4, "expected four conductors")
        self._require(all(len(conductor_samples(conductor)) >= 2 for conductor in conductors), "conductors need samples")
        self._require(len(all_conductor_samples(geometry)) >= 8, "aggregate conductor sample count too low")

        corridor = corridor_model(geometry)
        self._require(corridor["span_range_m"]["max"] > corridor["span_range_m"]["min"], "invalid span range")
        self._require(corridor["lateral_range_m"]["max"] > corridor["lateral_range_m"]["min"], "invalid lateral range")
        self._require(corridor["z_range_m"]["max"] > corridor["z_range_m"]["min"], "invalid z range")

        by_id = {item["id"]: item for item in geometry.drone_positions}
        in_corridor = by_id["mid_corridor_taken_off_conductors_visible"]["pose"]
        vertical_band = by_id["midspan_inside_powerline_corridor"]["pose"]
        outside = by_id["midspan_lateral_outside_north"]["pose"]

        self._require(corridor_membership(geometry, in_corridor)["inside_powerline_corridor"], "known in-corridor pose classified outside")
        self._require(corridor_membership(geometry, vertical_band)["inside_conductor_vertical_band"], "vertical-band pose not classified inside")
        self._require(not corridor_membership(geometry, outside)["inside_powerline_corridor"], "known outside pose classified inside")

        nearest = nearest_conductor(geometry, by_id["midspan_sensor_fov_lower_stack"]["pose"])
        self._require(nearest["id"] is not None, "nearest conductor missing")
        self._require(nearest["distance_m"] < 5.0, "nearest conductor distance unexpectedly large")
        self._require(all(key in nearest["closest_point"] for key in ("x", "y", "z")), "closest point incomplete")

        visible = visibility_state(geometry, in_corridor)
        self._require(visible["expected_visible_conductor_ids"], "expected visible conductors missing")
        yaw_away = dict(in_corridor)
        yaw_away["yaw"] = float(in_corridor.get("yaw", 0.0)) + 3.14159
        away = visibility_state(geometry, yaw_away)
        self._require(
            len(away["expected_visible_conductor_ids"]) <= len(visible["expected_visible_conductor_ids"]),
            "yaw-away visibility did not reduce or preserve visible conductor count",
        )
        short_range = visibility_state(geometry, in_corridor, max_range_m=0.1)
        self._require(not short_range["expected_visible_conductor_ids"], "range gate did not remove visible conductors")
        narrow_fov = visibility_state(geometry, in_corridor, horizontal_fov_rad=0.01)
        self._require(
            len(narrow_fov["expected_visible_conductor_ids"]) <= len(visible["expected_visible_conductor_ids"]),
            "narrow FOV increased visible conductor count",
        )
        self._write_json(
            "geometry/summary.json",
            {
                "corridor": corridor,
                "nearest": nearest,
                "visible": visible["expected_visible_conductor_ids"],
                "yaw_away_visible": away["expected_visible_conductor_ids"],
            },
        )

    def phase_offline(self) -> None:
        artifact_dir = self.artifact_root / "offline"
        outputs = self._run_batch(self.mcp_root / "config/offline_observation_tests.json", artifact_dir, continue_on_error=True)
        spec = json.loads((self.mcp_root / "config/offline_observation_tests.json").read_text(encoding="utf-8"))
        self._assert_expected_batch_results(outputs, spec)
        for filename in [
            "offline_observation_topdown.png",
            "offline_observation_side.png",
            "offline_observation_conductor_clearance.png",
            "offline_observe_window.json",
            "offline_observe_window_topdown.png",
            "offline_observe_window_side.png",
            "offline_observe_window_conductor_clearance.png",
        ]:
            self._require_file(artifact_dir / filename)

    def phase_snapshots(self) -> None:
        artifact_dir = self.artifact_root / "snapshots"
        outputs = self._run_batch(self.mcp_root / "config/rendered_observation_e2e.json", artifact_dir)
        self._assert_all_success(outputs)
        image_paths: dict[str, Path] = {}
        camera_poses: dict[str, dict[str, Any]] = {}
        for item in outputs:
            if item["tool"] not in {"sim.render_snapshot", "sim.render_snapshot_set"}:
                continue
            if item["tool"] == "sim.render_snapshot":
                image = item["data"]["image"]
                image_paths[item["data"]["view"]] = Path(image["artifact_path"])
                camera_poses[item["data"]["view"]] = item["data"]["camera_pose"]
                self._assert_image_metadata(image)
            else:
                for view, data in item["data"]["snapshots"].items():
                    self._assert_image_metadata(data["image"])
                    camera_poses[f"set_{view}"] = data["camera_pose"]
        for view in ["custom", "topdown", "follow_drone", "corridor", "target", "perception_fov"]:
            self._require(view in image_paths, f"missing snapshot view: {view}")
        self._require(camera_poses["topdown"] != camera_poses["follow_drone"], "topdown and follow camera poses are identical")
        self._require(self._file_sha256(image_paths["topdown"]) != self._file_sha256(image_paths["follow_drone"]), "topdown and follow images are identical")

    def phase_rendered_e2e(self) -> None:
        artifact_dir = self.artifact_root / "rendered_e2e"
        outputs = self._run_batch(self.mcp_root / "config/e2e_smoke_batch.json", artifact_dir)
        self._assert_all_success(outputs)
        by_tool = [item["tool"] for item in outputs]
        for tool in ["simulation", "system", "px4.health", "operation.activate", "operation.fly_relative", "sim.observe_window", "topic", "gazebo"]:
            self._require(tool in by_tool, f"rendered E2E missing tool result: {tool}")
        observe = next(item for item in outputs if item["tool"] == "sim.observe_window")
        verdict = observe["data"]["verdict"]
        self._require(verdict["success"], "rendered observation verdict failed")
        self._require(verdict["metrics"]["valid_pose_sample_count"] >= 5, "rendered observation sample count too low")
        self._require(verdict["metrics"]["nearest_conductor_distance_min_m"] is not None, "missing conductor clearance metric")
        for relative in [
            "custom_operation_observation.json",
            "custom_operation_observation_topdown.png",
            "custom_operation_observation_side.png",
            "custom_operation_observation_conductor_clearance.png",
            "rendered_external_snapshot.png",
            "powerline_mapper_powerline.yaml",
        ]:
            self._require_file(artifact_dir / relative)

    def phase_headless_e2e(self) -> None:
        artifact_dir = self.artifact_root / "headless_e2e"
        outputs = self._run_batch(self.mcp_root / "config/headless_observation_e2e.json", artifact_dir)
        self._assert_all_success(outputs)
        observe = next(item for item in outputs if item["tool"] == "sim.observe_window")
        self._require(observe["data"]["verdict"]["success"], "headless observation verdict failed")
        self._require(not observe["data"]["start_snapshots"], "headless observation unexpectedly captured start snapshots")
        self._require_file(artifact_dir / "headless_observe_window.json")

        snapshot = self._call_mcp(
            "sim.render_snapshot",
            {"view": "topdown", "filename": "headless_snapshot_classification.png", "timeout_sec": 5},
            artifact_dir,
            allow_failure=True,
        )
        classification = {
            "supported": bool(snapshot["success"]),
            "message": snapshot.get("message", ""),
            "data": snapshot.get("data"),
        }
        self._write_json("headless_e2e/headless_snapshot_classification.json", classification)
        self._call_mcp("simulation", {"command": "stop", "timeout_sec": 30}, artifact_dir, allow_failure=True)

    def phase_failure_modes(self) -> None:
        artifact_dir = self.artifact_root / "failure_modes"
        self._call_mcp("system", {"command": "shutdown", "timeout_sec": 60}, artifact_dir, allow_failure=True)
        self._call_mcp("simulation", {"command": "stop", "timeout_sec": 30}, artifact_dir, allow_failure=True)
        cases = [
            ("bad_geometry_path", "sim.geometry_state", {"geometry_path": "/tmp/does-not-exist.json"}),
            ("unknown_snapshot_view", "sim.render_snapshot", {"view": "unknown"}),
            (
                "no_tf_live_observe",
                "sim.observe_window",
                {"duration_sec": 0.2, "sample_period_sec": 0.1, "capture_snapshots": False, "min_sample_count": 2},
            ),
            (
                "wrong_corridor_expectation",
                "sim.observe_window",
                {
                    "capture_snapshots": False,
                    "expected_corridor": False,
                    "path_samples": [{"x": -0.03, "y": 0.0, "z": 1.45, "t": 0.0}, {"x": 0.17, "y": 0.0, "z": 1.45, "t": 1.0}],
                },
            ),
            (
                "too_high_clearance",
                "sim.observe_window",
                {
                    "capture_snapshots": False,
                    "min_conductor_clearance_m": 100.0,
                    "path_samples": [{"x": -0.03, "y": 0.0, "z": 1.45, "t": 0.0}, {"x": 0.17, "y": 0.0, "z": 1.45, "t": 1.0}],
                },
            ),
            ("operation_inactive", "operation.fly_relative", {"dx": 0.1, "tf_timeout_sec": 0.2, "timeout_sec": 2}),
            ("px4_stopped", "px4.health", {"timeout_sec": 1, "stable_sec": 0}),
            ("gazebo_stopped_snapshot", "sim.render_snapshot", {"view": "topdown", "timeout_sec": 2}),
        ]
        results = []
        for name, tool, arguments in cases:
            result = self._call_mcp(tool, arguments, artifact_dir, allow_failure=True)
            self._require(not result["success"], f"failure-mode case unexpectedly succeeded: {name}")
            self._require(result.get("message") or result.get("traceback") or result.get("data"), f"failure-mode case lacked diagnostic output: {name}")
            results.append({"name": name, "tool": tool, "message": result.get("message", ""), "success": result["success"]})
        self._write_json("failure_modes/summary.json", {"cases": results})

    def phase_audit(self) -> None:
        expected_files = [
            self.artifact_root / "offline/offline_observe_window.json",
            self.artifact_root / "rendered_e2e/custom_operation_observation.json",
            self.artifact_root / "rendered_e2e/rendered_external_snapshot.png",
            self.artifact_root / "rendered_e2e/powerline_mapper_powerline.yaml",
            self.artifact_root / "headless_e2e/headless_observe_window.json",
            self.artifact_root / "failure_modes/summary.json",
        ]
        for path in expected_files:
            self._require_file(path)
        for path in self.artifact_root.rglob("*.png"):
            self._require(path.stat().st_size > 0, f"empty png artifact: {path}")
        root_owned = [str(path) for path in self.artifact_root.rglob("*") if path.exists() and path.stat().st_uid == 0]
        self._require(not root_owned, f"root-owned artifacts found: {root_owned[:5]}")
        status_dir = self.artifact_root / "audit"
        simulation_status = self._call_mcp("simulation", {"command": "status", "timeout_sec": 10}, status_dir, allow_failure=True)
        stdout = (simulation_status.get("data") or {}).get("stdout", "")
        self._require("tmux_session: stopped" in stdout, "simulation still running after suite")
        self._write_json("audit/runtime_status.json", {"simulation": simulation_status})

    def _run_batch(self, path: Path, artifact_dir: Path, *, continue_on_error: bool = False) -> list[dict[str, Any]]:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "iii_drone_mcp.mcp_batch",
            str(path),
            "--artifact-dir",
            str(artifact_dir),
            "--per-call-timeout-sec",
            "420",
        ]
        if continue_on_error:
            command.append("--continue-on-error")
        result = self._run(command, timeout=900, check=not continue_on_error)
        output_path = artifact_dir / "batch_output.json"
        output_path.write_text(result.stdout, encoding="utf-8")
        try:
            outputs = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise TestFailure(f"batch did not emit JSON: {path}\nstdout={result.stdout}\nstderr={result.stderr}") from exc
        if not continue_on_error:
            self._assert_all_success(outputs)
        return outputs

    def _call_mcp(self, tool: str, arguments: dict[str, Any], artifact_dir: Path, *, allow_failure: bool = False) -> dict[str, Any]:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "iii_drone_mcp.mcp_call",
            tool,
            json.dumps(arguments),
            "--artifact-dir",
            str(artifact_dir),
            "--json",
        ]
        result = self._run(command, timeout=max(20, int(float(arguments.get("timeout_sec", 5))) + 20), check=False)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise TestFailure(f"MCP call did not emit JSON for {tool}: stdout={result.stdout} stderr={result.stderr}") from exc
        if not allow_failure and not payload["success"]:
            raise TestFailure(f"MCP call failed for {tool}: {payload.get('message')}")
        return payload

    def _run(self, command: list[str], *, timeout: int, check: bool = True) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.mcp_root) + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            command,
            cwd=self.workspace_root,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if check and completed.returncode != 0:
            raise TestFailure(
                "command failed:\n"
                f"  {' '.join(command)}\n"
                f"returncode={completed.returncode}\nstdout={completed.stdout}\nstderr={completed.stderr}"
            )
        return completed

    def _assert_expected_batch_results(self, outputs: list[dict[str, Any]], spec: list[dict[str, Any]]) -> None:
        self._require(len(outputs) == len(spec), "offline batch output length mismatch")
        for result, call in zip(outputs, spec):
            expected = bool(call.get("expected_success", True))
            self._require(bool(result["success"]) == expected, f"unexpected success for {result['tool']}: expected {expected}, got {result['success']}")

    @staticmethod
    def _assert_all_success(outputs: list[dict[str, Any]]) -> None:
        failed = [item for item in outputs if not item.get("success")]
        if failed:
            raise TestFailure(f"batch failures: {json.dumps(failed, indent=2, default=str)}")

    def _assert_image_metadata(self, image: dict[str, Any]) -> None:
        path = Path(image["artifact_path"])
        self._require_file(path)
        self._require(path.stat().st_size > 0, f"empty image artifact: {path}")
        self._require(int(image["width"]) > 0 and int(image["height"]) > 0, f"invalid image dimensions: {path}")
        self._require(bool(image.get("bbox")), f"blank image bbox: {path}")

    def _best_effort_cleanup(self) -> None:
        try:
            self._call_mcp("system", {"command": "shutdown", "timeout_sec": 60}, self.artifact_root / "cleanup", allow_failure=True)
        except Exception:
            pass
        try:
            self._call_mcp("simulation", {"command": "stop", "timeout_sec": 30}, self.artifact_root / "cleanup", allow_failure=True)
        except Exception:
            pass

    def _write_summary(self) -> None:
        self._write_json("summary.json", {"artifact_root": str(self.artifact_root), "summary": self.summary})

    def _write_json(self, relative: str, data: Any) -> Path:
        path = self.artifact_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return path

    @staticmethod
    def _file_sha256(path: Path) -> str:
        import hashlib

        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _require(condition: bool, message: str) -> None:
        if not condition:
            raise TestFailure(message)

    def _require_file(self, path: Path) -> None:
        self._require(path.exists(), f"missing artifact: {path}")
        self._require(path.stat().st_size > 0, f"empty artifact: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run III-Drone MCP observation tooling tests")
    parser.add_argument("--workspace-root", default=os.environ.get("III_DRONE_WORKSPACE_ROOT", "/home/iii/ws"))
    parser.add_argument("--artifact-root", default=None)
    parser.add_argument("--phase", action="append", choices=DEFAULT_PHASES + ["all"], help="Phase to run; can be repeated")
    parser.add_argument("--keep-environment", action="store_true", help="Do not try to stop system/simulation after a failed phase")
    parser.add_argument("--clean", action="store_true", help="Remove the artifact root before running")
    args = parser.parse_args()

    workspace_root = Path(args.workspace_root).resolve()
    run_id = time.strftime("%Y%m%d_%H%M%S")
    artifact_root = Path(args.artifact_root or Path("/tmp/iii_drone/mcp_observation_suite") / run_id).resolve()
    if args.clean and artifact_root.exists():
        shutil.rmtree(artifact_root)

    phases = args.phase or ["all"]
    if "all" in phases:
        phases = DEFAULT_PHASES

    suite = ObservationTestSuite(
        workspace_root=workspace_root,
        artifact_root=artifact_root,
        keep_environment=args.keep_environment,
    )
    try:
        suite.run(phases)
    except Exception as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"Artifacts: {artifact_root}")


if __name__ == "__main__":
    main()
