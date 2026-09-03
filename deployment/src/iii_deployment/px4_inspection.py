"""Receiver-owned read-only PX4 release inspection over dedicated Ethernet MAVLink."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .contracts import ContractError, ContractRegistry, canonical_json, content_identity
from .px4_parameters import MavlinkParameterAdapter, PX4ParameterError, PX4ParameterStore
from .px4_release import (
    audit_release,
    load_dds_contract,
    load_firmware_spec,
    validate_release_inputs,
)
from .px4_network import load_network_baseline


class PX4ReleaseInspector:
    """Authenticate one staged release and compare its FMU without writing it."""

    def __init__(
        self,
        *,
        schema_root: Path,
        state_root: Path,
        endpoint: str = "udpin:0.0.0.0:14541",
        timeout: float = 30.0,
    ) -> None:
        self.schema_root = schema_root
        self.state_root = state_root
        self.endpoint = endpoint
        self.timeout = timeout

    @staticmethod
    def _json(path: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise ContractError(f"staged PX4 release input is missing or linked: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ContractError(f"staged PX4 release input is malformed: {path}")
        return value

    def _retain(
        self,
        audit: dict[str, Any],
        evidence: dict[str, Any] | None,
        registry: ContractRegistry,
    ) -> dict[str, Any]:
        registry.validate("px4-release-audit", audit)
        if evidence is not None:
            registry.validate("px4-activation-evidence", evidence)
        result = {"audit": audit, "activation_evidence": evidence}
        if self.state_root.is_symlink():
            raise ContractError("PX4 audit state root is linked")
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        path = self.state_root / f"{audit['audit_id']}.json"
        raw = canonical_json(result) + b"\n"
        if path.exists() or path.is_symlink():
            if path.is_symlink() or path.read_bytes() != raw:
                raise ContractError("retained PX4 audit identity collision")
            return result
        temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
        try:
            with temporary.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return result

    def audit(self, *, release_id: str, release_root: Path) -> dict[str, Any]:
        manifest = self._json(release_root / "release-manifest.json")
        if manifest.get("release_id") != release_id:
            raise ContractError("PX4 audit release identity differs from staged state")
        resources = release_root / "install/share/iii-deployment/px4"
        registry = ContractRegistry(self.schema_root)
        spec = load_firmware_spec(resources / "firmware.json", registry)
        dds = load_dds_contract(resources / "dds-topics.json", registry)
        network = load_network_baseline(
            resources / "network-baseline.json", schema_root=self.schema_root
        )
        parameters = self._json(resources / "real.json")
        registry.validate("px4-parameter-manifest", parameters)
        validate_release_inputs(
            spec=spec,
            dds=dds,
            network=network,
            parameters=parameters,
            registry=registry,
        )
        declared = manifest.get("px4", {})
        if (
            declared.get("spec_id") != spec["spec_id"]
            or declared.get("dds_topics_id") != dds["contract_id"]
            or declared.get("network_baseline_id") != network["baseline_id"]
            or declared.get("manifest_ids", {}).get("real") != parameters["manifest_id"]
        ):
            raise ContractError("staged PX4 resources differ from the signed release")
        adapter = MavlinkParameterAdapter(self.endpoint, timeout=self.timeout)
        try:
            status = dict(adapter.status())
        except PX4ParameterError:
            audit = audit_release(
                release_id=release_id,
                spec=spec,
                dds=dds,
                network=network,
                parameters=parameters,
                status=None,
                snapshot=None,
                comparison=None,
                provenance="receiver-px4-ethernet",
            )
            return self._retain(audit, None, registry)
        if (
            status.get("armed") is not False
            or status.get("firmware_version") != spec["version"]
            or status.get("firmware_commit") != spec["advertised_commit"]
        ):
            audit = audit_release(
                release_id=release_id,
                spec=spec,
                dds=dds,
                network=network,
                parameters=parameters,
                status=status,
                snapshot=None,
                comparison=None,
                provenance="receiver-px4-ethernet",
            )
            return self._retain(audit, None, registry)
        artifacts = {
            path: adapter.read_text_file(path)
            for path in (
                network["artifacts"]["net_cfg_path"],
                network["artifacts"]["extras_path"],
            )
        }
        store = PX4ParameterStore(
            manifest_paths={"real": resources / "real.json", "sim": resources / "sim.json"},
            state_root=self.state_root,
            schema_root=self.schema_root,
            adapter=adapter,
        )
        snapshot = store.pull("real", provenance="receiver-px4-ethernet")
        comparison = store.compare("real", snapshot["snapshot_id"])
        audit = audit_release(
            release_id=release_id,
            spec=spec,
            dds=dds,
            network=network,
            parameters=parameters,
            status=status,
            snapshot=snapshot,
            comparison=comparison,
            provenance="receiver-px4-ethernet",
            network_artifacts=artifacts,
        )
        evidence = {
            "schema": "iii.px4-activation-evidence/v1",
            "evidence_id": "0" * 64,
            "captured_at": snapshot["captured_at"],
            "release_id": release_id,
            "profile": "real",
            "manifest_id": comparison["manifest_id"],
            "snapshot": snapshot,
            "comparison": comparison,
            "healthy": audit["healthy"],
            "writes_performed": 0,
        }
        evidence["evidence_id"] = content_identity(
            {key: value for key, value in evidence.items() if key != "evidence_id"}
        )
        return self._retain(audit, evidence, registry)
