#!/usr/bin/env python3
"""Tests for the read-only HIL operation-registry scanner."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.workspace.hil_operation_registry import (
    RegistryScanError,
    resolve_operation_roots,
    scan_operation_roots,
)


MODULE_PATH = Path(__file__).with_name("hil_operation_registry.py")
CLI_ID = "iii-testop1"
RECON_ID = "recon-test-01"


def _compact(value: object, *, newline: bool = False) -> bytes:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (text + ("\n" if newline else "")).encode("utf-8")


def _identity(value: dict[str, object], field: str, *, newline: bool = False) -> str:
    unsigned = {key: item for key, item in value.items() if key != field}
    return hashlib.sha256(_compact(unsigned, newline=newline)).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_compact(value, newline=True))


def _cli_plan(identifier: str = CLI_ID) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "iii.cli-operation-plan/v1",
        "operation_id": identifier,
        "created_at": "2026-09-22T00:00:00Z",
        "command": "system boot",
        "argv": ["iii", "system", "boot"],
        "mutating": False,
        "context": {"target": None, "profile": "hil", "release_id": None},
    }
    value["plan_id"] = _identity(value, "plan_id")
    return value


def _cli_state(plan: dict[str, object], state: str = "completed") -> dict[str, object]:
    return {
        "schema": "iii.cli-operation-state/v1",
        "operation_id": plan["operation_id"],
        "plan_id": plan["plan_id"],
        "state": state,
        "attempt": 1,
        "updated_at": "2026-09-22T00:00:01Z",
        "exit_code": 0,
        "result_code": None,
        "evidence": [],
    }


def _write_cli(root: Path, *, state: str = "completed", identifier: str = CLI_ID) -> Path:
    plan = _cli_plan(identifier)
    directory = root / identifier
    _write_json(directory / "plan.json", plan)
    _write_json(directory / "state.json", _cli_state(plan, state))
    return directory


def _reconciliation_journal(*, phase: str = "complete", operation_id: str = RECON_ID) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "iii.configuration-reconciliation-journal/v1",
        "journal_id": "0" * 64,
        "operation_id": operation_id,
        "plan_id": "a" * 64,
        "initial_state_id": "b" * 64,
        "phase": phase,
        "completed_paths": ["parameter_sets/hil/default.yaml"],
        "state_id": "c" * 64,
    }
    value["journal_id"] = _identity(value, "journal_id", newline=True)
    return value


def _write_reconciliation(
    root: Path,
    *,
    phase: str = "complete",
    current: bool = True,
    operation_id: str = RECON_ID,
    review: bool = False,
    decisions: bool = False,
) -> Path:
    journal = _reconciliation_journal(phase=phase, operation_id=operation_id)
    suffix = f"-{journal['plan_id'][:16]}" if current else ""
    directory = root / f"{operation_id}{suffix}"
    _write_json(directory / "reconciliation-journal.json", journal)
    if review:
        value: dict[str, object] = {
            "schema": "iii.configuration-reintroduction-review/v1",
            "review_id": "0" * 64,
            "operation_id": operation_id,
            "plan_id": journal["plan_id"],
            "target_id": "target",
            "items": [],
        }
        value["review_id"] = _identity(value, "review_id", newline=True)
        _write_json(directory / "reconciliation-review.json", value)
        if decisions:
            decision: dict[str, object] = {
                "schema": "iii.configuration-reintroduction-decisions/v1",
                "decision_id": "0" * 64,
                "review_id": value["review_id"],
                "operation_id": operation_id,
                "plan_id": journal["plan_id"],
                "decisions": {},
            }
            decision["decision_id"] = _identity(decision, "decision_id", newline=True)
            _write_json(directory / "reconciliation-decisions.json", decision)
    return directory


class RegistryScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "operations"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def assertRefused(self, root: Path) -> None:
        with self.assertRaises(RegistryScanError):
            scan_operation_roots([root])

    def test_valid_completed_cli(self):
        _write_cli(self.root)
        result = scan_operation_roots([self.root])
        self.assertEqual(result["schema"], "hil-operation-registry-scan/v1")
        self.assertEqual([item["status"] for item in result["terminal"]], ["completed"])
        self.assertEqual(result["nonterminal"], [])

    def test_every_terminal_cli_state(self):
        states = ("completed", "warning", "rejected", "failed", "partial", "interrupted", "cancelled")
        for index, state in enumerate(states):
            _write_cli(self.root, state=state, identifier=f"iii-state-{index:02d}")
        result = scan_operation_roots([self.root])
        self.assertEqual({item["status"] for item in result["terminal"]}, set(states))

    def test_planned_and_running_are_nonterminal(self):
        _write_cli(self.root, state="planned", identifier="iii-planned")
        _write_cli(self.root, state="running", identifier="iii-running")
        result = scan_operation_roots([self.root])
        self.assertEqual({item["status"] for item in result["nonterminal"]}, {"planned", "running"})
        self.assertEqual(result["terminal"], [])

    def test_bad_plan_hash(self):
        directory = _write_cli(self.root)
        value = json.loads((directory / "plan.json").read_text())
        value["plan_id"] = "f" * 64
        _write_json(directory / "plan.json", value)
        self.assertRefused(self.root)

    def test_cli_path_and_id_mismatch(self):
        directory = _write_cli(self.root)
        value = json.loads((directory / "plan.json").read_text())
        value["operation_id"] = "iii-other1"
        value["plan_id"] = _identity(value, "plan_id")
        _write_json(directory / "plan.json", value)
        self.assertRefused(self.root)

    def test_missing_plan(self):
        directory = _write_cli(self.root)
        (directory / "plan.json").unlink()
        self.assertRefused(self.root)

    def test_missing_state(self):
        directory = _write_cli(self.root)
        (directory / "state.json").unlink()
        self.assertRefused(self.root)

    def test_valid_historical_reconciliation(self):
        _write_reconciliation(self.root, current=False)
        result = scan_operation_roots([self.root])
        self.assertEqual(result["terminal"][0]["kind"], "reconciliation")

    def test_valid_current_reconciliation(self):
        _write_reconciliation(self.root, current=True)
        result = scan_operation_roots([self.root])
        self.assertEqual(result["terminal"][0]["operation_id"], RECON_ID)

    def test_prepared_and_applying_are_nonterminal(self):
        _write_reconciliation(self.root, phase="prepared", operation_id="recon-prepared")
        _write_reconciliation(self.root, phase="applying", operation_id="recon-applying")
        result = scan_operation_roots([self.root])
        self.assertEqual({item["status"] for item in result["nonterminal"]}, {"prepared", "applying"})

    def test_invalid_journal_phase(self):
        directory = _write_reconciliation(self.root)
        value = json.loads((directory / "reconciliation-journal.json").read_text())
        value["phase"] = "unknown"
        value["journal_id"] = _identity(value, "journal_id", newline=True)
        _write_json(directory / "reconciliation-journal.json", value)
        self.assertRefused(self.root)

    def test_bad_journal_identity(self):
        directory = _write_reconciliation(self.root)
        value = json.loads((directory / "reconciliation-journal.json").read_text())
        value["journal_id"] = "f" * 64
        _write_json(directory / "reconciliation-journal.json", value)
        self.assertRefused(self.root)

    def test_unsafe_completed_paths_are_refused(self):
        directory = _write_reconciliation(self.root)
        for unsafe_path in ("../escape", "a//b", "a/./b", "a/../b"):
            with self.subTest(unsafe_path=unsafe_path):
                value = json.loads((directory / "reconciliation-journal.json").read_text())
                value["completed_paths"] = [unsafe_path]
                value["journal_id"] = _identity(value, "journal_id", newline=True)
                _write_json(directory / "reconciliation-journal.json", value)
                self.assertRefused(self.root)

    def test_journal_with_review_is_valid(self):
        _write_reconciliation(self.root, review=True)
        result = scan_operation_roots([self.root])
        self.assertEqual(result["terminal"][0]["status"], "complete")

    def test_review_without_journal_is_refused(self):
        directory = self.root / "review-only-01"
        value: dict[str, object] = {
            "schema": "iii.configuration-reintroduction-review/v1",
            "review_id": "0" * 64,
            "operation_id": "review-only-01",
            "plan_id": "a" * 64,
            "target_id": "target",
            "items": [],
        }
        value["review_id"] = _identity(value, "review_id", newline=True)
        _write_json(directory / "reconciliation-review.json", value)
        self.assertRefused(self.root)

    def test_decisions_without_review(self):
        directory = _write_reconciliation(self.root)
        journal = json.loads((directory / "reconciliation-journal.json").read_text())
        value: dict[str, object] = {
            "schema": "iii.configuration-reintroduction-decisions/v1",
            "decision_id": "0" * 64,
            "review_id": "d" * 64,
            "operation_id": RECON_ID,
            "plan_id": journal["plan_id"],
            "decisions": {},
        }
        value["decision_id"] = _identity(value, "decision_id", newline=True)
        _write_json(directory / "reconciliation-decisions.json", value)
        self.assertRefused(self.root)

    def test_mixed_schemas(self):
        directory = _write_cli(self.root)
        _write_json(directory / "reconciliation-journal.json", _reconciliation_journal())
        self.assertRefused(self.root)

    def test_malformed_json(self):
        directory = _write_cli(self.root)
        (directory / "state.json").write_text("{")
        self.assertRefused(self.root)

    def test_symlink_root(self):
        target = Path(self.tempdir.name) / "real"
        target.mkdir()
        link = Path(self.tempdir.name) / "link"
        link.symlink_to(target, target_is_directory=True)
        self.assertRefused(link)

    def test_broken_symlink_root_is_refused(self):
        link = Path(self.tempdir.name) / "broken-link"
        link.symlink_to(Path(self.tempdir.name) / "does-not-exist", target_is_directory=True)
        self.assertRefused(link)

    def test_child_symlink(self):
        self.root.mkdir(parents=True)
        target = Path(self.tempdir.name) / "target"
        target.mkdir()
        (self.root / "iii-linked1").symlink_to(target, target_is_directory=True)
        self.assertRefused(self.root)

    def test_nonhidden_top_level_file(self):
        self.root.mkdir(parents=True)
        (self.root / "unexpected.json").write_text("{}")
        self.assertRefused(self.root)

    def test_hidden_registry_lock(self):
        self.root.mkdir(parents=True)
        (self.root / ".registry.lock").write_text("lock")
        result = scan_operation_roots([self.root])
        self.assertEqual(result["terminal"], [])
        self.assertEqual(result["nonterminal"], [])

    def test_missing_root_is_empty(self):
        result = scan_operation_roots([self.root])
        self.assertEqual(result["roots"], [str(self.root.absolute())])

    def test_deduplicated_default_roots(self):
        home = Path(self.tempdir.name) / "home"
        env = {"HOME": str(home)}
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        try:
            self.assertEqual(resolve_operation_roots(env), [home / ".local" / "state" / "iii" / "operations"])
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home

    def test_distinct_cli_and_configuration_roots_union(self):
        env = {
            "III_OPERATION_STATE_DIR": str(Path(self.tempdir.name) / "cli"),
            "III_OPERATIONS_ROOT": str(Path(self.tempdir.name) / "config"),
        }
        self.assertEqual(resolve_operation_roots(env), [Path(env["III_OPERATION_STATE_DIR"]), Path(env["III_OPERATIONS_ROOT"])])

    def test_cli_exit_0_for_terminal(self):
        _write_cli(self.root)
        completed = subprocess.run(
            [sys.executable, "-B", str(MODULE_PATH)],
            env={**os.environ, "III_OPERATION_STATE_DIR": str(self.root), "III_OPERATIONS_ROOT": str(self.root)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["nonterminal"], [])

    def test_cli_exit_3_for_nonterminal(self):
        _write_cli(self.root, state="running")
        completed = subprocess.run(
            [sys.executable, "-B", str(MODULE_PATH)],
            env={**os.environ, "III_OPERATION_STATE_DIR": str(self.root), "III_OPERATIONS_ROOT": str(self.root)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertEqual(len(json.loads(completed.stdout)["nonterminal"]), 1)

    def test_cli_exit_2_for_refusal(self):
        self.root.mkdir(parents=True)
        (self.root / "bad").write_text("unsafe")
        completed = subprocess.run(
            [sys.executable, "-B", str(MODULE_PATH)],
            env={**os.environ, "III_OPERATION_STATE_DIR": str(self.root), "III_OPERATIONS_ROOT": str(self.root)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("registry scan refused:", completed.stderr)
        self.assertEqual(completed.stdout, "")


if __name__ == "__main__":
    unittest.main()
