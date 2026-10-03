#!/usr/bin/env python3
"""Offline source-contract tests for the HIL perception-seam wrapper."""

from __future__ import annotations

from pathlib import Path
from contextlib import nullcontext, redirect_stdout
import io
import re
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


WRAPPER = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "simulation"
    / "run_hil_perception_seam_probe.sh"
)


class RetainedOperationPreflightContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = WRAPPER.read_text(encoding="utf-8")
        start = cls.source.index('WS_RETAINED_OPERATIONS_PATH="')
        end = cls.source.index('if ! pi_ros "test ! -e', start)
        cls.block = cls.source[start:end]

    def test_embedded_scanner_is_removed(self):
        self.assertNotIn("RETAINED_OPERATION_SCAN=", self.source)

    def test_helper_constant_and_workstation_mount_invocation(self):
        self.assertIn(
            'OPERATION_REGISTRY_HELPER="${WORKSPACE_ROOT}/scripts/workspace/hil_operation_registry.py"',
            self.source,
        )
        self.assertIn(
            "export WORKSPACE_DIR='${WS_ROOT_IN_CONTAINER}'; python3 -B '${WS_ROOT_IN_CONTAINER}/scripts/workspace/hil_operation_registry.py'",
            self.block,
        )
        self.assertIn("ws_ros", self.block)

    def test_pi_streams_local_helper_to_stdin(self):
        self.assertIn(
            'pi_ros "python3 -B -" <"${OPERATION_REGISTRY_HELPER}"',
            self.block,
        )

    def test_exact_scan_artifact_names_exist(self):
        for filename in (
            '"${ARTIFACT_DIR}/retained_operations_workstation.json"',
            '"${ARTIFACT_DIR}/retained_operations_pi.json"',
            '"${ARTIFACT_DIR}/retained_operations_preflight.json"',
            '"${ARTIFACT_DIR}/logs/retained_operations_workstation.log"',
            '"${ARTIFACT_DIR}/logs/retained_operations_pi.log"',
        ):
            self.assertIn(filename, self.block)

    def test_only_scanner_exit_codes_zero_and_three_are_accepted(self):
        self.assertRegex(
            self.block,
            r"WS_RETAINED_OPERATIONS_RC\s*!=\s*0\s*&&\s*WS_RETAINED_OPERATIONS_RC\s*!=\s*3",
        )
        self.assertRegex(
            self.block,
            r"PI_RETAINED_OPERATIONS_RC\s*!=\s*0\s*&&\s*PI_RETAINED_OPERATIONS_RC\s*!=\s*3",
        )

    def test_host_specific_failure_messages_remain(self):
        self.assertIn('refuse "workstation retained-operation preflight failed"', self.block)
        self.assertIn('refuse "Pi retained-operation preflight failed"', self.block)

    def test_combined_schema_and_nonterminal_refusal_remain(self):
        self.assertIn('"schema": "hil-operation-registry-preflight/v1"', self.block)
        self.assertIn('refuse "another planned or running retained operation exists"', self.block)

    def test_preflight_block_does_not_delete_or_cancel_operations(self):
        lowered = self.block.lower()
        self.assertNotIn("cancel", lowered)
        self.assertNotIn("delete", lowered)
        self.assertIsNone(re.search(r"\brm\b", lowered))

    def test_all_report_writes_use_the_renderer(self):
        self.assertIn(
            'REPORT_RENDERER="${WORKSPACE_ROOT}/scripts/workspace/hil_perception_seam_report.py"',
            self.source,
        )
        self.assertIn("render_report initial", self.source)
        self.assertIn("render_report exit", self.source)
        self.assertIn("render_report final", self.source)
        self.assertNotRegex(
            self.source,
            r"cat\s+>[^\n]*REPORT\.md[^\n]*<<-?\s*['\"]?EOF",
        )

    def test_report_renderer_receives_dynamic_fields_as_quoted_arguments(self):
        function_start = self.source.index("render_report() {")
        function_end = self.source.index("}\n\npython3 -", function_start)
        function = self.source[function_start:function_end]
        for argument in (
            '--output="${REPORT_PATH}"',
            '--run-id="${RUN_ID}"',
            '--status="${FINAL_STATUS}"',
            '--reason="${STATUS_REASON}"',
            '--classification="${ANALYSIS_CLASSIFICATION:-not-run}"',
        ):
            self.assertIn(argument, function)

    def test_report_renderer_uses_single_argument_form_for_phase(self):
        function_start = self.source.index("render_report() {")
        function_end = self.source.index("}\n\npython3 -", function_start)
        function = self.source[function_start:function_end]
        self.assertIn('--phase="${phase}"', function)

    def test_selected_host_defaults_to_hostname_and_accepts_override(self):
        self.assertIn('PI_HOST="${III_HIL_PI_ENDPOINT:-${III_HIL_PI_ADDRESS:-iii.local}}"', self.source)
        self.assertIn("--host|--pi-host)", self.source)
        self.assertNotIn('PI_ADDRESS="${III_HIL_PI_ADDRESS:-10.42.0.15}"', self.source)

    def test_route_resolver_skips_stale_first_dns_address(self):
        match = re.search(r'HIL_ROUTE=.*?<<\'PY\'\n(.*?)\nPY', self.source, re.S)
        self.assertIsNotNone(match)
        resolver = match.group(1)
        candidates = ["10.42.0.15", "192.168.1.251"]
        route_calls = []
        connect_calls = []

        def getaddrinfo(*_args):
            return [(None, None, None, None, (candidate, 0)) for candidate in candidates]

        def route_run(command, **_kwargs):
            peer = command[-1]
            route_calls.append(peer)
            return subprocess.CompletedProcess(
                command, 0, stdout=f"{peer} dev eth0 src 192.168.1.10\n", stderr=""
            )

        def connect(address, timeout):
            connect_calls.append((address, timeout))
            if address[0] == "10.42.0.15":
                raise OSError("unreachable stale address")
            return nullcontext()

        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["resolver", "pi.example", ""]),
            patch.object(socket, "getaddrinfo", getaddrinfo),
            patch.object(socket, "create_connection", connect),
            patch.object(subprocess, "run", route_run),
            redirect_stdout(output),
        ):
            exec(compile(resolver, "<probe-route-resolver>", "exec"), {"__name__": "__main__"})

        self.assertEqual(output.getvalue().strip(), "192.168.1.251 192.168.1.10")
        self.assertEqual(route_calls, candidates)
        self.assertEqual(
            connect_calls,
            [(('10.42.0.15', 22), 0.75), (('192.168.1.251', 22), 0.75)],
        )

    def test_selected_host_reaches_every_outbound_transport(self):
        self.assertIn('"${PI_USER}@${PI_HOST}"', self.source)
        self.assertIn('"${PI_USER}@${PI_HOST}:${REMOTE_ARTIFACT}/."', self.source)
        self.assertIn('"${WORKSPACE_ROOT}/tools/simulation/launch_hil_workstation.sh" --host "${PI_HOST}"', self.source)
        # The resolved Pi address reaches the launcher's DDS peer configuration.
        self.assertIn('export III_HIL_PI_ENDPOINT="${PI_HOST}" III_HIL_PI_ADDRESS="${PI_ADDRESS}"', self.source)

    def test_manifest_records_selected_host_and_resolved_route(self):
        self.assertIn('"pi_host": sys.argv[3]', self.source)
        self.assertIn('"pi_resolved_ipv4": sys.argv[4]', self.source)
        self.assertIn('"workstation_source_ipv4": sys.argv[5]', self.source)
        self.assertIn('"${PI_HOST}" "${PI_ADDRESS}" "${WORKSTATION_ADDRESS}"', self.source)
        self.assertIn("<<'PY'", self.source)

    def test_manifest_serializes_host_as_json_data(self):
        marker = 'python3 - "${MANIFEST_PATH}" "${RUN_ID}" "${PI_HOST}" "${PI_ADDRESS}" "${WORKSTATION_ADDRESS}" "${ARTIFACT_DIR}" <<\'PY\'\n'
        start = self.source.index(marker) + len(marker)
        end = self.source.index("\nPY\nrender_report initial", start)
        manifest_code = self.source[start:end]
        hostile_host = 'pi.example"; __import__("os").system("touch injected") #'
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    manifest_code,
                    str(manifest_path),
                    "run-id",
                    hostile_host,
                    "192.0.2.40",
                    "192.0.2.10",
                    directory,
                ],
                check=True,
            )
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(payload["pi_host"], hostile_host)
            self.assertFalse((Path(directory) / "injected").exists())

    def test_workstation_override_must_match_selected_pi_route(self):
        self.assertIn(
            '"${WORKSTATION_ADDRESS}" != "${ROUTE_WORKSTATION_ADDRESS}"',
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
