#!/usr/bin/env python3
"""GUI v2 sim end-to-end smoke runner.

The default mode is read-only: it proves the GC stack can discover/select a sim
runtime API, authenticate through the proxy, and read all operator domains.
Mutating flight and workflow commands require explicit flags.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, urlunsplit


DEFAULT_READ_ENDPOINTS = [
    ("session", "/proxy/session"),
    ("runtime-status", "/proxy/runtime/status"),
    ("system-health", "/proxy/system/health"),
    ("subsystems-health", "/proxy/subsystems/health"),
    ("vehicle-status", "/proxy/vehicle/status"),
    ("control-status", "/proxy/control/status"),
    ("mission-status", "/proxy/mission/status"),
    ("operations-status", "/proxy/operations/status"),
    ("payload-status", "/proxy/payload/status"),
    ("perception-status", "/proxy/perception/status"),
    ("powerline-status", "/proxy/powerline/status"),
    ("configuration-status", "/proxy/configuration/status"),
    ("map-state", "/proxy/map/state"),
    ("rosbag-status", "/proxy/rosbag/status"),
    ("logs-sources", "/proxy/logs/sources"),
    ("commands-handlers", "/proxy/commands/handlers"),
    ("events-recent", "/proxy/events/recent"),
]

MUTATING_WORKFLOW_COMMANDS = [
    ("runtime-boot", "runtime.boot", {"profile": "sim"}),
    ("runtime-start", "runtime.start", {}),
    ("payload-gripper-open", "payload.gripper.open", {}),
    ("payload-gripper-close", "payload.gripper.close", {}),
    ("perception-pl-mapper-start", "perception.pl_mapper.start", {}),
    ("perception-pl-mapper-pause", "perception.pl_mapper.pause", {}),
    ("perception-pl-mapper-freeze", "perception.pl_mapper.freeze", {}),
    ("perception-pl-mapper-stop", "perception.pl_mapper.stop", {}),
    ("powerline-overview-update", "powerline.overview.update", {"timeout_s": 5}),
    (
        "configuration-list-snapshots",
        "configuration.snapshot.list",
        {},
    ),
    (
        "rosbag-list",
        "rosbag.list",
        {},
    ),
    (
        "custom-operation-validate",
        "custom_operation.validate",
        {
            "operation": "hover",
            "arguments": {
                "duration_s": 1.0,
                "sustain": False,
            },
        },
    ),
    (
        "custom-operation-hover-start",
        "custom_operation.hover.start",
        {
            "operation": "hover",
            "arguments": {
                "duration_s": 1.0,
                "sustain": False,
            },
        },
    ),
    ("custom-operation-cancel", "custom_operation.cancel", {}),
]

FLIGHT_COMMANDS = [
    ("px4-arm", "px4.arm", {}),
    ("px4-takeoff", "px4.takeoff", {"altitude_m": 1.5}),
    ("px4-hold", "px4.hold", {}),
    ("px4-land", "px4.land", {}),
]


class SmokeFailure(RuntimeError):
    pass


class SmokeRunner:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.workspace = Path(args.workspace).resolve()
        self.artifacts = Path(args.artifacts_dir).resolve() / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.step_index = 0
        self.summary: dict[str, Any] = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "artifacts_dir": str(self.artifacts),
            "runtime_url": args.runtime_url,
            "proxy_url": args.proxy_url,
            "frontend_url": args.frontend_url,
            "steps": [],
            "mutating_workflows": bool(args.run_mutating_workflows),
            "flight_commands": bool(args.run_flight_commands),
        }

    def run(self) -> int:
        compose_started = False
        try:
            if self.args.start_compose:
                self.compose_up()
                compose_started = True
            self.wait_for_url(f"{self.args.runtime_url}/identity", "runtime identity")
            self.wait_for_url(f"{self.args.proxy_url}/identity", "GC proxy identity")
            if self.args.frontend_url:
                self.wait_for_url(f"{self.args.frontend_url}/", "frontend")

            runtime_identity = self.http_json("GET", f"{self.args.runtime_url}/identity", step_name="runtime-identity")
            self.summary["runtime_identity"] = runtime_identity
            if runtime_identity.get("profile") != "sim":
                raise SmokeFailure(f"expected sim runtime profile, got {runtime_identity.get('profile')!r}")

            self.http_json("GET", f"{self.args.proxy_url}/identity", step_name="proxy-identity")
            self.http_json("GET", f"{self.args.proxy_url}/runtime/discovery?timeout_s=2", step_name="runtime-discovery")
            endpoint = self.http_json(
                "POST",
                f"{self.args.proxy_url}/runtime/discovery/manual",
                {"base_url": self.args.runtime_url, "runtime_name": "local-sim"},
                step_name="manual-runtime-endpoint",
            )
            endpoint_id = endpoint["endpoint_id"]
            self.http_json(
                "POST",
                f"{self.args.proxy_url}/runtime/targets/validate",
                {"endpoint_id": endpoint_id},
                step_name="validate-runtime-target",
            )
            self.http_json(
                "POST",
                f"{self.args.proxy_url}/runtime/target/select",
                {"endpoint_id": endpoint_id},
                step_name="select-runtime-target",
            )
            self.http_json("GET", f"{self.args.proxy_url}/proxy/identity", step_name="proxied-runtime-identity")

            login = self.http_json(
                "POST",
                f"{self.args.proxy_url}/proxy/session/login",
                {"password": self.args.password, "client_label": "gui-v2-sim-e2e-smoke"},
                step_name="session-login",
            )
            token = login["session_token"]
            headers = {"Authorization": f"Bearer {token}"}
            self.summary["session_acquired"] = True

            for name, path in DEFAULT_READ_ENDPOINTS:
                self.http_json("GET", f"{self.args.proxy_url}{path}", headers=headers, step_name=name)

            if self.args.run_mutating_workflows:
                self.run_mutating_workflows(headers)
            if self.args.run_flight_commands:
                if not self.args.run_mutating_workflows:
                    raise SmokeFailure("--run-flight-commands requires --run-mutating-workflows")
                self.run_flight_commands(headers)

            self.http_json("POST", f"{self.args.proxy_url}/proxy/session/logout", headers=headers, step_name="session-logout")
            self.summary["completed_at"] = datetime.now(timezone.utc).isoformat()
            self.summary["status"] = "passed"
            return 0
        except Exception as exc:
            self.summary["completed_at"] = datetime.now(timezone.utc).isoformat()
            self.summary["status"] = "failed"
            self.summary["failure"] = str(exc)
            print(f"GUI v2 sim E2E smoke failed: {exc}", file=sys.stderr)
            print(f"Artifacts: {self.artifacts}", file=sys.stderr)
            return 1
        finally:
            if compose_started:
                self.capture_compose_logs()
                if not self.args.keep_compose:
                    self.compose_down()
            self.write_summary()

    def run_mutating_workflows(self, headers: dict[str, str]) -> None:
        for name, command_id, parameters in MUTATING_WORKFLOW_COMMANDS:
            self.dispatch_command(name, command_id, parameters, headers)

    def run_flight_commands(self, headers: dict[str, str]) -> None:
        for name, command_id, parameters in FLIGHT_COMMANDS:
            self.dispatch_command(name, command_id, parameters, headers)

    def dispatch_command(self, name: str, command_id: str, parameters: dict[str, Any], headers: dict[str, str]) -> dict:
        request_id = f"smoke-{self.step_index + 1:02d}-{name}"
        payload = {
            "request_id": request_id,
            "client_label": "gui-v2-sim-e2e-smoke",
            "command_id": command_id,
            "parameters": parameters,
        }
        result = self.http_json(
            "POST",
            f"{self.args.proxy_url}/proxy/commands/actions/start",
            payload,
            headers=headers,
            step_name=name,
        )
        if not result.get("accepted"):
            raise SmokeFailure(f"{command_id} rejected: {json.dumps(result.get('rejection') or result, sort_keys=True)}")
        return result

    def wait_for_url(self, url: str, label: str) -> None:
        deadline = time.monotonic() + self.args.timeout_s
        last_error = ""
        while time.monotonic() < deadline:
            try:
                self.http_status("GET", url)
                return
            except Exception as exc:
                last_error = str(exc)
                time.sleep(1)
        raise SmokeFailure(f"timed out waiting for {label} at {url}: {last_error}")

    def http_status(self, method: str, url: str) -> int:
        request = Request(url, method=method, headers={"Accept": "*/*"})
        try:
            with urlopen(request, timeout=self.args.http_timeout_s) as response:
                return response.status
        except HTTPError as exc:
            raise SmokeFailure(f"{method} {url} returned HTTP {exc.code}") from exc
        except URLError as exc:
            raise SmokeFailure(f"{method} {url} failed: {exc.reason}") from exc

    def http_json(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        step_name: str,
        record: bool = True,
    ) -> dict:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = {"Accept": "application/json"}
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        request = Request(url, data=data, method=method, headers=request_headers)
        try:
            with urlopen(request, timeout=self.args.http_timeout_s) as response:
                raw = response.read()
                status = response.status
        except HTTPError as exc:
            raw = exc.read()
            self.record_http_step(step_name, method, url, exc.code, raw, body, record)
            raise SmokeFailure(f"{method} {url} returned HTTP {exc.code}: {raw.decode('utf-8', errors='replace')}") from exc
        except URLError as exc:
            raise SmokeFailure(f"{method} {url} failed: {exc.reason}") from exc

        self.record_http_step(step_name, method, url, status, raw, body, record)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise SmokeFailure(f"{method} {url} returned non-JSON body") from exc

    def record_http_step(
        self,
        name: str,
        method: str,
        url: str,
        status: int,
        raw: bytes,
        request_body: dict[str, Any] | None,
        record: bool,
    ) -> None:
        if not record:
            return
        self.step_index += 1
        safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name)
        path = self.artifacts / f"{self.step_index:02d}-{safe_name}.json"
        try:
            response_body: Any = json.loads(raw.decode("utf-8")) if raw else None
        except json.JSONDecodeError:
            response_body = raw.decode("utf-8", errors="replace")
        payload = {
            "name": name,
            "method": method,
            "url": url,
            "status": status,
            "request": request_body,
            "response": response_body,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        self.summary["steps"].append({"name": name, "status": status, "artifact": str(path)})
        print(f"[{self.step_index:02d}] {name}: HTTP {status}")

    def compose_up(self) -> None:
        env = os.environ.copy()
        frontend_port = port_from_url(self.args.frontend_url, default="5174")
        env.setdefault("III_GC_FRONTEND_PORT", frontend_port)
        env.setdefault("III_GC_PROXY_PUBLIC_URL", self.args.proxy_url)
        cors_origins = merge_csv_values(
            env.get("III_GC_PROXY_CORS_ORIGINS", ""),
            localhost_origin_aliases(self.args.frontend_url),
        )
        env["III_GC_PROXY_CORS_ORIGINS"] = ",".join(cors_origins)
        cmd = [
            "docker",
            "compose",
            "-p",
            self.args.compose_project,
            "-f",
            self.args.compose_file,
            "up",
            "-d",
            "--build",
        ]
        self.run_subprocess(cmd, env=env, name="compose-up")

    def compose_down(self) -> None:
        cmd = [
            "docker",
            "compose",
            "-p",
            self.args.compose_project,
            "-f",
            self.args.compose_file,
            "down",
            "--remove-orphans",
        ]
        self.run_subprocess(cmd, name="compose-down", check=False)

    def capture_compose_logs(self) -> None:
        cmd = [
            "docker",
            "compose",
            "-p",
            self.args.compose_project,
            "-f",
            self.args.compose_file,
            "logs",
            "--no-color",
        ]
        result = subprocess.run(cmd, cwd=self.workspace, text=True, capture_output=True, check=False)
        (self.artifacts / "compose.log").write_text(result.stdout + result.stderr, encoding="utf-8")

    def run_subprocess(self, cmd: list[str], *, name: str, env: dict[str, str] | None = None, check: bool = True) -> None:
        artifact = self.artifacts / f"{name}.log"
        print(f"{name}: running {' '.join(cmd)}", flush=True)
        with artifact.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                cmd,
                cwd=self.workspace,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
            returncode = process.wait()
        if check and returncode != 0:
            raise SmokeFailure(f"{name} failed with exit {returncode}; see {artifact}")

    def write_summary(self) -> None:
        (self.artifacts / "summary.json").write_text(json.dumps(self.summary, indent=2, sort_keys=True), encoding="utf-8")
        latest = Path(self.args.artifacts_dir).resolve() / "latest"
        try:
            if latest.exists() or latest.is_symlink():
                latest.unlink()
            latest.symlink_to(self.artifacts, target_is_directory=True)
        except OSError:
            pass


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    workspace = Path(__file__).resolve().parents[2]
    parser.add_argument("--workspace", default=str(workspace), help="Workspace root.")
    parser.add_argument("--runtime-url", default=os.environ.get("III_RUNTIME_API_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--proxy-url", default=os.environ.get("III_GC_PROXY_URL", "http://127.0.0.1:8780"))
    parser.add_argument("--frontend-url", default=os.environ.get("III_GC_FRONTEND_URL", "http://127.0.0.1:5174"))
    parser.add_argument("--password", default=os.environ.get("III_RUNTIME_API_BROWSER_PASSWORD", "dev-password"))
    parser.add_argument("--artifacts-dir", default=os.environ.get("III_GUI_V2_E2E_ARTIFACTS", "log/gui-v2-sim-e2e-smoke"))
    parser.add_argument("--timeout-s", type=float, default=float(os.environ.get("III_GUI_V2_E2E_TIMEOUT_SEC", "60")))
    parser.add_argument("--http-timeout-s", type=float, default=10.0)
    parser.add_argument("--start-compose", action="store_true", help="Start the GC compose stack before running checks.")
    parser.add_argument("--keep-compose", action="store_true", help="Leave compose services running after the smoke.")
    parser.add_argument("--compose-file", default="src/III-Drone-GC/docker-compose.prod.yml")
    parser.add_argument("--compose-project", default=os.environ.get("III_GUI_V2_E2E_COMPOSE_PROJECT", "iii-gc-e2e-smoke"))
    parser.add_argument("--run-mutating-workflows", action="store_true", help="Run sim-only runtime, payload, perception, configuration, rosbag, and operation commands.")
    parser.add_argument("--run-flight-commands", action="store_true", help="Run sim-only arm, takeoff, hold, and land commands. Requires --run-mutating-workflows.")
    return parser.parse_args(argv)


def port_from_url(url: str | None, *, default: str) -> str:
    if not url:
        return default
    parsed = urlsplit(url)
    if parsed.port is not None:
        return str(parsed.port)
    if parsed.scheme == "https":
        return "443"
    if parsed.scheme == "http":
        return "80"
    return default


def localhost_origin_aliases(url: str | None) -> list[str]:
    if not url:
        return ["http://127.0.0.1:5174", "http://localhost:5174"]
    parsed = urlsplit(url)
    scheme = parsed.scheme or "http"
    port = port_from_url(url, default="5174")
    hostname = parsed.hostname or "127.0.0.1"
    hosts = [hostname]
    if hostname == "127.0.0.1":
        hosts.append("localhost")
    elif hostname == "localhost":
        hosts.append("127.0.0.1")
    origins: list[str] = []
    for host in hosts:
        netloc = f"{host}:{port}"
        origin = urlunsplit((scheme, netloc, "", "", ""))
        if origin not in origins:
            origins.append(origin)
    return origins


def merge_csv_values(existing: str, required: list[str]) -> list[str]:
    values: list[str] = []
    for value in [*(item.strip() for item in existing.split(",") if item.strip()), *required]:
        if value not in values:
            values.append(value)
    return values


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    return SmokeRunner(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
