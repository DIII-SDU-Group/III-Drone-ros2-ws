"""HIL peer selection keeps the reachable address across mDNS changes."""

from pathlib import Path
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

    @staticmethod
    def _connect(address, **_kwargs):
        if address[0] == "10.42.0.15":
            raise OSError("unreachable direct link")
        return _Connection()


if __name__ == "__main__":
    unittest.main()
