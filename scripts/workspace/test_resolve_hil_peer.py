"""HIL peer selection keeps the reachable address across mDNS changes."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts.workspace import resolve_hil_peer


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def connect(self, _address):
        pass

    def getsockname(self):
        return "192.168.1.89", 12345


class ResolveHilPeerTests(unittest.TestCase):
    def test_owner_peer_wins_when_mdns_only_has_unreachable_direct_link(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / ".iii-hil-owner-0.env").write_text("pi_address=192.168.1.251\n")
            with mock.patch.object(resolve_hil_peer.socket, "getaddrinfo", return_value=[
                (2, 1, 6, "", ("10.42.0.15", 0))
            ]), mock.patch.object(resolve_hil_peer.socket, "create_connection", side_effect=self._connect), \
                 mock.patch.object(resolve_hil_peer.socket, "socket", return_value=_Connection()):
                self.assertEqual(resolve_hil_peer.resolve("iii.local", state), ("192.168.1.251", "192.168.1.89"))
            self.assertEqual(len(list(state.glob(".iii-hil-peer-*"))), 1)

    def test_cached_peer_survives_owner_removal_and_is_rechecked(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / ".iii-hil-owner-0.env").write_text("pi_address=192.168.1.251\n")
            with mock.patch.object(resolve_hil_peer.socket, "getaddrinfo", return_value=[]), \
                 mock.patch.object(resolve_hil_peer.socket, "create_connection", side_effect=self._connect), \
                 mock.patch.object(resolve_hil_peer.socket, "socket", return_value=_Connection()):
                self.assertIsNotNone(resolve_hil_peer.resolve("iii.local", state))
                (state / ".iii-hil-owner-0.env").unlink()
                self.assertEqual(resolve_hil_peer.resolve("iii.local", state), ("192.168.1.251", "192.168.1.89"))
            with mock.patch.object(resolve_hil_peer.socket, "getaddrinfo", return_value=[]), \
                 mock.patch.object(resolve_hil_peer.socket, "create_connection", side_effect=OSError("offline")):
                self.assertIsNone(resolve_hil_peer.resolve("iii.local", state))

    def test_explicit_other_host_does_not_inherit_canonical_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / ".iii-hil-owner-0.env").write_text("pi_address=192.168.1.251\n")
            with mock.patch.object(resolve_hil_peer.socket, "getaddrinfo", return_value=[]):
                self.assertIsNone(resolve_hil_peer.resolve("other.local", state))

    def test_failed_resolution_explains_each_rejected_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / ".iii-hil-owner-0.env").write_text("pi_address=192.168.1.251\n")
            diagnostics = []
            with mock.patch.object(resolve_hil_peer.socket, "getaddrinfo", side_effect=resolve_hil_peer.socket.gaierror(-2, "Name or service not known")), \
                 mock.patch.object(resolve_hil_peer.socket, "create_connection", side_effect=OSError(113, "No route to host")):
                self.assertIsNone(resolve_hil_peer.resolve("iii.local", state, diagnostics=diagnostics))
            self.assertEqual(len(diagnostics), 2)
            self.assertIn("name resolution for iii.local failed", diagnostics[0])
            self.assertIn("192.168.1.251: SSH port 22 unreachable", diagnostics[1])
            self.assertIn("No route to host", diagnostics[1])

    def test_cli_reports_failure_on_stderr_with_nonzero_status(self):
        with tempfile.TemporaryDirectory() as directory:
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(resolve_hil_peer.socket, "getaddrinfo", return_value=[]), \
                 contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = resolve_hil_peer.main(["resolve_hil_peer.py", "other.local", directory])
            self.assertEqual(status, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("HIL peer resolution for other.local found no reachable IPv4", stderr.getvalue())
            self.assertIn("no IPv4 candidates for other.local", stderr.getvalue())

    def test_cli_prints_only_the_selected_route_on_success(self):
        with tempfile.TemporaryDirectory() as directory:
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(resolve_hil_peer.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.251", 0))]), \
                 mock.patch.object(resolve_hil_peer.socket, "create_connection", side_effect=self._connect), \
                 mock.patch.object(resolve_hil_peer.socket, "socket", return_value=_Connection()), \
                 contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = resolve_hil_peer.main(["resolve_hil_peer.py", "other.local", directory])
            self.assertEqual(status, 0)
            self.assertEqual(stdout.getvalue(), "192.168.1.251 192.168.1.89\n")
            self.assertEqual(stderr.getvalue(), "")

    def test_setup_profile_surfaces_a_failed_resolution(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            env = {
                **{k: v for k, v in os.environ.items() if not k.startswith(("III_", "CYCLONEDDS_", "ROS_"))},
                "III_HIL_PI_ENDPOINT": "unresolvable-hil-peer.invalid",
                "III_HIL_PEER_STATE_DIR": directory,
            }
            result = subprocess.run(
                ["bash", "-c", 'source "$1"; printf "%s|%s" "${III_HIL_RESOLVED_PI_ADDRESS:-}" "$III_HIL_PI_ENDPOINT"',
                 "bash", str(root / "setup/setup_hil.bash")],
                env=env, text=True, capture_output=True, check=True, timeout=60,
            )
        self.assertEqual(result.stdout, "|unresolvable-hil-peer.invalid")
        self.assertIn("HIL peer resolution for unresolvable-hil-peer.invalid found no reachable IPv4", result.stderr)
        self.assertIn("HIL setup: no reachable Pi IPv4 was resolved", result.stderr)

    @staticmethod
    def _connect(address, **_kwargs):
        if address[0] == "10.42.0.15":
            raise OSError("unreachable direct link")
        return _Connection()


if __name__ == "__main__":
    unittest.main()
