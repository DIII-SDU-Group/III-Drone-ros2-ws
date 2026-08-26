"""Emit a deterministic machine-readable Ansible recap for III orchestration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from ansible.plugins.callback import CallbackBase


DOCUMENTATION = r"""
name: iii_result
type: aggregate
short_description: write III host-convergence recap JSON
version_added: "2.17"
description:
  - Writes per-host recap counters to the owner-controlled path in
    C(III_ANSIBLE_RESULT_PATH).
requirements:
  - The output path must not already be a symbolic link.
"""


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "iii_result"
    CALLBACK_NEEDS_ENABLED = True

    def v2_playbook_on_stats(self, stats: object) -> None:
        destination = os.environ.get("III_ANSIBLE_RESULT_PATH")
        if not destination:
            return
        path = Path(destination)
        if path.is_symlink():
            self._display.error("III Ansible result path is a symbolic link")
            return
        hosts = {}
        for host in sorted(stats.processed):  # type: ignore[attr-defined]
            summary = stats.summarize(host)  # type: ignore[attr-defined]
            hosts[host] = {
                key: int(summary.get(key, 0))
                for key in ("ok", "changed", "failures", "unreachable", "skipped", "rescued", "ignored")
            }
        result = {
            "schema": "iii.ansible-run-result/v1",
            "check_mode": os.environ.get("III_ANSIBLE_CHECK_MODE") == "1",
            "hosts": hosts,
            "totals": {
                key: sum(host[key] for host in hosts.values())
                for key in ("ok", "changed", "failures", "unreachable", "skipped", "rescued", "ignored")
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                json.dump(result, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            Path(temporary_name).unlink(missing_ok=True)
