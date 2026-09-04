"""Receiver-only adapter for the Configuration-owned reconciliation engine."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Any

from .contracts import ContractError


class ReceiverConfigurationReconciler:
    """Plan against an immutable checkpoint; mutate only a private staged copy."""

    def __init__(
        self,
        *,
        releases_root: Path,
        checkpoints_root: Path,
        staging_root: Path,
        operations_root: Path,
        active_state_root: Path | None = None,
        target_id: str,
        runtime_profile: str,
    ) -> None:
        self.releases_root = releases_root.absolute()
        self.checkpoints_root = checkpoints_root.absolute()
        self.staging_root = staging_root.absolute()
        self.operations_root = operations_root.absolute()
        self.active_state_root = (
            active_state_root.absolute() if active_state_root is not None else None
        )
        self.target_id = target_id
        self.runtime_profile = runtime_profile
        if runtime_profile not in {"real", "opti_track"}:
            raise ContractError(
                "receiver configuration reconciliation requires an aircraft profile"
            )

    @staticmethod
    def _api():
        try:
            import iii_drone_configuration as configuration
        except ImportError as exc:
            raise ContractError(
                "receiver lacks the pinned iii-drone-configuration reconciliation package"
            ) from exc
        return configuration

    def _checkpoint(self, checkpoint_id: str) -> Path:
        if len(checkpoint_id) != 64 or any(
            character not in "0123456789abcdef" for character in checkpoint_id
        ):
            raise ContractError("configuration checkpoint identity is malformed")
        path = self.checkpoints_root / checkpoint_id
        if path.parent != self.checkpoints_root:
            raise ContractError("configuration checkpoint path escapes its fixed root")
        return path

    def _contract_root(self, release_id: str) -> Path:
        path = (
            self.releases_root
            / release_id
            / "install/iii_drone_configuration/share/iii_drone_configuration/configuration_contract"
        )
        if path.is_symlink() or not path.is_dir():
            raise ContractError(
                "staged release lacks its immutable configuration contract"
            )
        return path

    def _inputs(
        self, *, release_id: str, source_checkpoint_id: str
    ) -> tuple[Path, Path, Path, Path, dict[str, Any]]:
        api = self._api()
        source = self._checkpoint(source_checkpoint_id)
        source_manifest = api.verify_configuration_checkpoint(source)
        if (
            source_manifest.get("target_id") != self.target_id
            or source_manifest.get("profile") != self.runtime_profile
        ):
            raise ContractError(
                "source configuration checkpoint targets another aircraft or profile"
            )
        old_manifest_id = source_manifest.get("configuration_manifest_id")
        old_release_id = source_manifest.get("release_id")
        if (
            not isinstance(old_manifest_id, str)
            or len(old_manifest_id) != 64
            or not isinstance(old_release_id, str)
            or not old_release_id
        ):
            raise ContractError(
                "source configuration checkpoint lacks release/manifest provenance"
            )
        retained = source / "contracts" / old_manifest_id
        old_root = (
            retained
            if retained.is_dir() and not retained.is_symlink()
            else self._contract_root(old_release_id)
        )
        state = source
        if self.active_state_root is not None and self.active_state_root.exists():
            active = self.active_state_root.resolve(strict=True)
            if active.is_symlink() or not active.is_dir():
                raise ContractError("active configuration state is unsafe")
            state = active
        return source, state, old_root, self._contract_root(release_id), source_manifest

    def preflight(
        self,
        *,
        operation_id: str,
        release_id: str,
        source_checkpoint_id: str,
        decisions: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        api = self._api()
        try:
            source, state, old_root, new_root, source_manifest = self._inputs(
                release_id=release_id, source_checkpoint_id=source_checkpoint_id
            )
            plan = api.plan_reconciliation(
                old_immutable_root=old_root,
                new_immutable_root=new_root,
                writable_state_root=state,
                operations_root=self.operations_root,
                operation_id=operation_id,
                runtime_profile=self.runtime_profile,
                target_id=self.target_id,
                old_release_id=source_manifest["release_id"],
                new_release_id=release_id,
                mode="receiver-staged",
                purpose="activation",
            )
        except api.ReconciliationError as exc:
            raise ContractError(
                f"configuration reconciliation contract rejected: {exc}"
            ) from exc
        normalized_decisions = dict(decisions or {})
        if plan.review_required and not normalized_decisions:
            return {
                "schema": "iii.receiver-configuration-reconciliation-preflight/v1",
                "ready": False,
                "source_checkpoint_id": source_checkpoint_id,
                "result_checkpoint_id": None,
                "reconciliation_plan": plan.as_dict(),
                "rejection_reasons": [
                    "configuration key reintroduction requires a bound explicit review"
                ],
                "review_items": [dict(item) for item in plan.review_items],
                "decisions": {},
                "writes_performed": 0,
            }
        api.validate_reintroduction_decisions(plan, normalized_decisions)
        predicted = api.plan_reconciled_checkpoint(
            plan,
            source_checkpoint=source,
            source_state_root=state,
            checkpoint_root=self.checkpoints_root,
            decisions=normalized_decisions,
        )
        return {
            "schema": "iii.receiver-configuration-reconciliation-preflight/v1",
            "ready": True,
            "source_checkpoint_id": source_checkpoint_id,
            "result_checkpoint_id": predicted["checkpoint_id"],
            "reconciliation_plan": plan.as_dict(),
            "checkpoint_plan": predicted,
            "rejection_reasons": [],
            "review_items": [],
            "decisions": normalized_decisions,
            "writes_performed": 0,
        }

    @staticmethod
    def _remove_private_stage(path: Path, *, operation_id: str, target_id: str) -> None:
        if not path.exists():
            return
        marker = path / "state/.iii-reconciliation-stage.json"
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(
                "existing configuration stage cannot be authenticated for recovery"
            ) from exc
        if value != {
            "schema": "iii.configuration-reconciliation-stage/v1",
            "operation_id": operation_id,
            "target_id": target_id,
        }:
            raise ContractError(
                "existing configuration stage belongs to another operation or target"
            )
        for item in sorted(path.rglob("*"), reverse=True):
            if not item.is_symlink():
                item.chmod(0o700 if item.is_dir() else 0o600)
        path.chmod(0o700)
        shutil.rmtree(path)

    def apply(
        self,
        *,
        operation_id: str,
        release_id: str,
        source_checkpoint_id: str,
        decisions: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        api = self._api()
        preflight = self.preflight(
            operation_id=operation_id,
            release_id=release_id,
            source_checkpoint_id=source_checkpoint_id,
            decisions=decisions,
        )
        if not preflight["ready"]:
            raise ContractError(
                "configuration reconciliation preflight is unresolved: "
                + "; ".join(preflight["rejection_reasons"])
            )
        source, state, old_root, new_root, source_manifest = self._inputs(
            release_id=release_id, source_checkpoint_id=source_checkpoint_id
        )
        stage = self.staging_root / operation_id
        if stage.parent != self.staging_root:
            raise ContractError("configuration stage escapes its fixed root")
        self._remove_private_stage(
            stage, operation_id=operation_id, target_id=self.target_id
        )
        staged_state = api.materialize_receiver_stage(
            source_checkpoint=source,
            source_state_root=state,
            stage_root=stage,
            operation_id=operation_id,
            target_id=self.target_id,
        )
        try:
            plan = api.plan_reconciliation(
                old_immutable_root=old_root,
                new_immutable_root=new_root,
                writable_state_root=staged_state,
                operations_root=self.operations_root,
                operation_id=operation_id,
                runtime_profile=self.runtime_profile,
                target_id=self.target_id,
                old_release_id=source_manifest["release_id"],
                new_release_id=release_id,
                mode="receiver-staged",
                purpose="activation",
            )
            result = api.execute_reconciliation(plan, decisions=dict(decisions or {}))
            if result.status != "complete":
                raise ContractError(
                    f"staged configuration reconciliation ended {result.status}"
                )
            sealed = api.seal_configuration_checkpoint(
                writable_state_root=staged_state,
                checkpoint_root=self.checkpoints_root,
                target_id=self.target_id,
                runtime_profile=self.runtime_profile,
                schema_version=plan.new_schema_version,
                release_id=release_id,
                manifest_id=plan.new_manifest_id,
            )
            if sealed["checkpoint_id"] != preflight["result_checkpoint_id"]:
                raise ContractError(
                    "materialized configuration checkpoint differs from retained preflight"
                )
            return {
                "schema": "iii.receiver-configuration-reconciliation-result/v1",
                "source_checkpoint_id": source_checkpoint_id,
                "result_checkpoint_id": sealed["checkpoint_id"],
                "plan_id": plan.plan_id,
                "sets": [item.as_dict() for item in result.sets],
                "changed_paths": list(result.changed_paths),
                "checkpoint": sealed,
                "only_staged_copy_mutated": True,
            }
        finally:
            if stage.exists():
                self._remove_private_stage(
                    stage, operation_id=operation_id, target_id=self.target_id
                )
