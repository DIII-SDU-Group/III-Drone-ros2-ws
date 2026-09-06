"""Exact PX4 source, DDS, firmware-build, and live-audit contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import os
from typing import Any, Callable, Mapping, Sequence

import yaml

from .contracts import ContractError, ContractRegistry, canonical_json, content_identity
from .px4_network import validate_network_baseline
from .px4_network import render_extras, render_net_cfg


class PX4ReleaseError(ContractError):
    """The PX4 companion release cannot be authenticated."""


def sitl_parameter_source_identity(
    snapshot: Mapping[str, Any],
    *,
    airframe_sha256: str | None,
    network_baseline_id: str,
    px4_commit: str,
) -> str:
    """Bind a generated SITL inventory to every classification input."""

    return content_identity(
        {
            "reference_snapshot_id": snapshot["snapshot_id"],
            "reference_snapshot_sha256": hashlib.sha256(
                canonical_json(snapshot) + b"\n"
            ).hexdigest(),
            "airframe_sha256": airframe_sha256,
            "classification_contract": "iii.px4-parameter-classification/v2",
            "network_baseline_id": network_baseline_id,
            "px4_commit": px4_commit,
        }
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PX4ReleaseError(f"PX4 contract is missing or linked: {path}")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PX4ReleaseError(f"cannot read PX4 contract {path}: {exc}") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise PX4ReleaseError(f"PX4 contract is not canonical JSON: {path}")
    return value


def normalized_dds_topics(path: Path, *, firmware_commit: str) -> dict[str, Any]:
    """Convert PX4's YAML generator input into a stable order-independent contract."""

    if not re.fullmatch(r"[a-f0-9]{40}", firmware_commit):
        raise PX4ReleaseError("PX4 DDS contract requires a full Git commit")
    if path.is_symlink() or not path.is_file():
        raise PX4ReleaseError("PX4 DDS source is missing or linked")
    try:
        source = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PX4ReleaseError(f"cannot parse PX4 DDS source: {exc}") from exc
    if not isinstance(source, dict) or set(source) != {
        "publications",
        "subscriptions",
        "subscriptions_multi",
    }:
        raise PX4ReleaseError("PX4 DDS source has an unsupported top-level shape")
    normalized: dict[str, list[dict[str, str]]] = {}
    for group in ("publications", "subscriptions", "subscriptions_multi"):
        rows = source[group] or []
        if not isinstance(rows, list):
            raise PX4ReleaseError(f"PX4 DDS {group} must be a list")
        result: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"topic", "type"}:
                raise PX4ReleaseError(f"PX4 DDS {group} entry is malformed")
            if not all(isinstance(row[key], str) for key in ("topic", "type")):
                raise PX4ReleaseError(f"PX4 DDS {group} entry is not textual")
            result.append({"topic": row["topic"], "type": row["type"]})
        result.sort(key=lambda item: (item["topic"], item["type"]))
        if len({(item["topic"], item["type"]) for item in result}) != len(result):
            raise PX4ReleaseError(f"PX4 DDS {group} contains duplicates")
        normalized[group] = result
    body: dict[str, Any] = {
        "schema": "iii.px4-dds-topics/v1",
        "firmware_commit": firmware_commit,
        "source": {
            "path": "src/modules/uxrce_dds_client/dds_topics.yaml",
            "sha256": _sha256(path),
        },
        **normalized,
    }
    return {"contract_id": content_identity(body), **body}


def load_dds_contract(path: Path, registry: ContractRegistry) -> dict[str, Any]:
    value = _canonical(path)
    registry.validate("px4-dds-topics", value)
    expected = content_identity({key: item for key, item in value.items() if key != "contract_id"})
    if value["contract_id"] != expected:
        raise PX4ReleaseError("PX4 DDS contract identity mismatch")
    return value


def load_firmware_spec(path: Path, registry: ContractRegistry) -> dict[str, Any]:
    value = _canonical(path)
    registry.validate("px4-firmware-spec", value)
    expected = content_identity({key: item for key, item in value.items() if key != "spec_id"})
    if value["spec_id"] != expected:
        raise PX4ReleaseError("PX4 firmware specification identity mismatch")
    if value["advertised_commit"] != value["git_commit"][:10]:
        raise PX4ReleaseError("PX4 advertised commit is not the exact source prefix")
    if value["build"]["command"] != ["make", value["board"]["target"]]:
        raise PX4ReleaseError("PX4 build command differs from the fixed board target")
    return value


def validate_release_inputs(
    *,
    spec: Mapping[str, Any],
    dds: Mapping[str, Any],
    network: Mapping[str, Any],
    parameters: Mapping[str, Any],
    registry: ContractRegistry,
) -> None:
    validate_network_baseline(network, registry)
    registry.validate("px4-parameter-manifest", parameters)
    profile = parameters.get("profile")
    if (
        dds["firmware_commit"] != spec["git_commit"]
        or dds["contract_id"] != spec["dds_topics_id"]
        or network["baseline_id"] != spec["network_baseline_id"]
        or profile not in {"real", "sim"}
        or (
            profile == "real"
            and parameters["manifest_id"] != spec["parameter_manifest_id"]
        )
        or parameters["firmware"]["reference_commit"] != spec["git_commit"]
        or parameters["firmware"]["reference_version"] != spec["version"]
        or network["firmware"]["reference_commit"] != spec["git_commit"]
    ):
        raise PX4ReleaseError("PX4 firmware inputs are not bound to one exact release")


def _git_output(root: Path, *argv: str) -> str:
    try:
        return subprocess.run(
            ["git", "-c", f"safe.directory={root}", "-C", str(root), *argv],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise PX4ReleaseError(f"PX4 Git inspection failed: {(exc.stderr or '').strip()}") from exc


def px4_source_identity(root: Path, spec: Mapping[str, Any]) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise PX4ReleaseError("PX4 source root is missing or linked")
    commit = _git_output(root, "rev-parse", "HEAD")
    if commit != spec["git_commit"]:
        raise PX4ReleaseError("PX4 source commit differs from the firmware specification")
    dirty = _git_output(root, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise PX4ReleaseError("PX4 source or nested submodule is modified")
    submodules = _git_output(root, "submodule", "status", "--recursive")
    # A leading '-' is Git's authenticated recorded commit for an unpopulated
    # optional nested dependency. Only commit drift ('+') or conflicts ('U')
    # make the source non-reproducible; the build initializes what its target uses.
    if any(line[:1] in {"+", "U"} for line in submodules.splitlines() if line):
        raise PX4ReleaseError("PX4 nested submodules are not at their recorded commits")
    normalized_submodules = "\n".join(
        line[1:] for line in submodules.splitlines() if line
    )
    return {
        "git_commit": commit,
        "git_describe": _git_output(root, "describe", "--always", "--tags", "--dirty"),
        "submodules_sha256": hashlib.sha256(
            (normalized_submodules + "\n").encode()
        ).hexdigest(),
    }


def _compiler_identity() -> dict[str, str]:
    try:
        version = subprocess.run(
            ["arm-none-eabi-gcc", "-dumpfullversion", "-dumpversion"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PX4ReleaseError("the pinned PX4 ARM compiler is unavailable") from exc
    executable = shutil.which("arm-none-eabi-gcc")
    if executable is None:
        raise PX4ReleaseError("the pinned PX4 ARM compiler is unavailable")
    return {
        "compiler": "arm-none-eabi-gcc",
        "compiler_version": version,
        "compiler_sha256": _sha256(Path(executable).resolve()),
    }


def _firmware_metadata(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PX4ReleaseError("PX4 firmware artifact is missing or linked")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise PX4ReleaseError(f"PX4 firmware artifact is malformed: {exc}") from exc
    if not isinstance(value, dict):
        raise PX4ReleaseError("PX4 firmware artifact metadata is malformed")
    return value


def build_firmware(
    *,
    source_root: Path,
    spec: Mapping[str, Any],
    dds: Mapping[str, Any],
    cache_root: Path,
    runner: Callable[..., Any] = subprocess.run,
) -> tuple[Path, dict[str, Any]]:
    """Build once per authenticated source/toolchain contract, then verify every hit."""

    source = px4_source_identity(source_root, spec)
    dds_source = source_root / spec["source"]["dds_topics"]
    observed_dds = normalized_dds_topics(dds_source, firmware_commit=spec["git_commit"])
    if observed_dds != dds:
        raise PX4ReleaseError("PX4 DDS source differs from the normalized release contract")
    toolchain = _compiler_identity()
    cache_key = content_identity(
        {
            "spec_id": spec["spec_id"],
            "source": source,
            "toolchain": toolchain,
        }
    )
    cache = cache_root.expanduser().resolve() / cache_key
    artifact = cache / spec["build"]["artifact"]
    cache_hit = artifact.is_file() and not artifact.is_symlink()
    if not cache_hit:
        runner(
            list(spec["build"]["command"]),
            cwd=source_root,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        built = source_root / "build" / spec["board"]["target"] / spec["build"]["artifact"]
        if built.is_symlink() or not built.is_file():
            raise PX4ReleaseError("PX4 build completed without the declared firmware")
        cache.mkdir(parents=True, exist_ok=True)
        temporary = cache / (artifact.name + ".tmp")
        shutil.copyfile(built, temporary)
        temporary.replace(artifact)
    metadata = _firmware_metadata(artifact)
    git_identity = str(metadata.get("git_identity", "")).lower()
    if (
        metadata.get("magic") != "PX4FWv1"
        or metadata.get("board_id") != spec["board"]["board_id"]
        or git_identity != source["git_describe"].lower()
    ):
        raise PX4ReleaseError("PX4 firmware metadata differs from the exact release")
    body = {
        "schema": "iii.px4-firmware-build/v1",
        "spec_id": spec["spec_id"],
        "cache_key": cache_key,
        "firmware": {
            "filename": artifact.name,
            "sha256": _sha256(artifact),
            "bytes": artifact.stat().st_size,
            "magic": metadata["magic"],
            "board_id": metadata["board_id"],
            "git_identity": git_identity,
        },
        "source": {**source, "dds_topics_id": dds["contract_id"]},
        "toolchain": toolchain,
        "cache_hit": cache_hit,
    }
    return artifact, {"build_id": content_identity({key: item for key, item in body.items() if key != "cache_hit"}), **body}


def audit_release(
    *,
    release_id: str,
    spec: Mapping[str, Any],
    dds: Mapping[str, Any],
    network: Mapping[str, Any],
    parameters: Mapping[str, Any],
    status: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None,
    comparison: Mapping[str, Any] | None,
    provenance: str,
    network_artifacts: Mapping[str, bytes] | None = None,
    require_network_artifacts: bool = True,
) -> dict[str, Any]:
    """Classify a zero-write FMU observation against one exact PX4 release."""

    findings: list[dict[str, Any]] = []
    if status is None or status.get("connected") is not True:
        findings.append({"code": "PX4_UNREACHABLE", "detail": "No MAVLink heartbeat arrived over the dedicated Pi-PX4 Ethernet link."})
    else:
        if status.get("armed") is not False:
            findings.append({"code": "PX4_ARMED", "detail": "PX4 must be disarmed for a complete release audit."})
        if status.get("firmware_version") != spec["version"]:
            findings.append({"code": "PX4_VERSION_MISMATCH", "expected": spec["version"], "observed": status.get("firmware_version")})
        if status.get("firmware_commit") != spec["advertised_commit"]:
            findings.append({"code": "PX4_COMMIT_MISMATCH", "expected": spec["advertised_commit"], "observed": status.get("firmware_commit")})
    identity_failures = {
        "PX4_UNREACHABLE",
        "PX4_ARMED",
        "PX4_VERSION_MISMATCH",
        "PX4_COMMIT_MISMATCH",
    }
    if status is not None and not any(item["code"] in identity_failures for item in findings):
        if snapshot is None or snapshot.get("complete") is not True:
            findings.append({"code": "PX4_PARAMETER_INVENTORY_INCOMPLETE", "detail": "The complete marker-bound parameter inventory was not received."})
        elif (
            comparison is None
            or comparison.get("manifest_id") != parameters["manifest_id"]
            or comparison.get("required_match") is not True
        ):
            # The generator marks calibration/identity values as preserved rather
            # than drift. Every release-owned default in both mutable classes must
            # otherwise match for release pairing.
            findings.append({"code": "PX4_PARAMETER_MISMATCH", "detail": "PX4 parameters differ from the release-owned complete manifest."})
        elif any(
            comparison.get("drift", {}).get(group, [])
            for group in ("release-required", "operator-tunable")
        ):
            findings.append({"code": "PX4_PARAMETER_MISMATCH", "detail": "PX4 parameters differ from the release-owned complete manifest."})
        # The physical-aircraft profile binds the FMU's Ethernet startup owners
        # to the release network baseline.  SITL/HIL deliberately has no such
        # binding: its split-workstation endpoints are process launch settings,
        # not persistent FMU network configuration.
        if parameters.get("network_baseline_id") is not None:
            required = {
                item["name"]: item["value"]
                for item in parameters["parameters"]
                if item["name"] in network["parameter_requirements"]
            }
            observed = {
                item["name"]: item["value"]
                for item in (snapshot or {}).get("parameters", [])
            }
            if any(
                observed.get(name) != expected
                for name, expected in required.items()
            ):
                findings.append({"code": "PX4_NETWORK_MISMATCH", "detail": "Release-owned PX4 Ethernet parameter owners differ."})
        if require_network_artifacts and network_artifacts is None:
            findings.append({"code": "PX4_NETWORK_ARTIFACTS_UNVERIFIED", "detail": "PX4 SD network/startup files were not authenticated."})
        elif require_network_artifacts:
            expected_artifacts = {
                network["artifacts"]["net_cfg_path"]: network["artifacts"]["net_cfg_sha256"],
                network["artifacts"]["extras_path"]: network["artifacts"]["extras_sha256"],
            }
            if set(network_artifacts) != set(expected_artifacts) or any(
                hashlib.sha256(network_artifacts[path]).hexdigest() != digest
                for path, digest in expected_artifacts.items()
            ):
                findings.append({"code": "PX4_NETWORK_ARTIFACT_MISMATCH", "detail": "PX4 SD net.cfg or extras.txt differs from the release."})
    exact_firmware = not any(
        item["code"] in {"PX4_UNREACHABLE", "PX4_VERSION_MISMATCH", "PX4_COMMIT_MISMATCH"}
        for item in findings
    )
    if not exact_firmware:
        findings.append(
            {
                "code": "PX4_DDS_TOPIC_CONTRACT_UNPROVEN",
                "detail": "The normalized compile-time uXRCE-DDS topic contract cannot be proven without the exact paired firmware commit.",
            }
        )
    body = {
        "schema": "iii.px4-release-audit/v1",
        "release_id": release_id,
        "spec_id": spec["spec_id"],
        "provenance": provenance,
        "status": dict(status) if status is not None else None,
        "snapshot_id": snapshot.get("snapshot_id") if snapshot else None,
        "parameter_manifest_id": parameters["manifest_id"],
        "network_baseline_id": network["baseline_id"],
        "dds_topics_id": dds["contract_id"],
        "dds_topics_observation": "proven-by-exact-firmware-commit" if exact_firmware else "not-proven",
        "findings": findings,
        "healthy": not findings,
        "writes_performed": 0,
    }
    return {"audit_id": content_identity(body), **body}


def prepare_release_media(
    *,
    destination: Path,
    firmware_path: Path,
    build_record_path: Path,
    resource_root: Path,
    schema_root: Path,
) -> dict[str, Any]:
    """Materialize one self-verifying, manual PX4 update package atomically."""

    registry = ContractRegistry(schema_root)
    spec = load_firmware_spec(resource_root / "firmware.json", registry)
    dds = load_dds_contract(resource_root / "dds-topics.json", registry)
    network = _canonical(resource_root / "network-baseline.json")
    parameters = _canonical(resource_root / "real.json")
    validate_release_inputs(
        spec=spec, dds=dds, network=network, parameters=parameters, registry=registry
    )
    build = _canonical(build_record_path)
    registry.validate("px4-firmware-build", build)
    expected_build_id = content_identity(
        {key: value for key, value in build.items() if key not in {"build_id", "cache_hit"}}
    )
    if (
        build["build_id"] != expected_build_id
        or build["spec_id"] != spec["spec_id"]
        or build["source"]["git_commit"] != spec["git_commit"]
        or firmware_path.is_symlink()
        or not firmware_path.is_file()
        or firmware_path.name != build["firmware"]["filename"]
        or _sha256(firmware_path) != build["firmware"]["sha256"]
        or firmware_path.stat().st_size != build["firmware"]["bytes"]
    ):
        raise PX4ReleaseError("PX4 release media inputs differ from the build record")
    destination = destination.expanduser().resolve()
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.partial")
    if destination.exists() or destination.is_symlink() or temporary.exists():
        raise PX4ReleaseError("PX4 release destination already exists or is unsafe")
    try:
        (temporary / "microSD/etc").mkdir(parents=True)
        (temporary / "firmware").mkdir()
        (temporary / "parameters").mkdir()
        shutil.copyfile(firmware_path, temporary / "firmware" / firmware_path.name)
        (temporary / "microSD/net.cfg").write_bytes(render_net_cfg(network))
        # PX4 consumes net.cfg during startup and it is not reliably retained
        # by the MAVFTP server.  Preserve an immutable, release-owned copy for
        # post-boot Ethernet attestation.
        (temporary / "microSD/etc/iii-network-baseline.cfg").write_bytes(
            render_net_cfg(network)
        )
        (temporary / "microSD/etc/extras.txt").write_bytes(render_extras(network))
        rows = ["# Onboard parameters for PX4", "# MAV ID\tCOMPONENT ID\tPARAM NAME\tVALUE\tTYPE"]
        mav_types = {"INT32": 6, "REAL32": 9}
        for parameter in parameters["parameters"]:
            if parameter["value"] is not None:
                rows.append(
                    f"1\t1\t{parameter['name']}\t{parameter['value']}\t{mav_types[parameter['mav_type']]}"
                )
        (temporary / "parameters/real.params").write_text("\n".join(rows) + "\n", encoding="ascii")
        body = {
            "schema": "iii.px4-release-media/v1",
            "spec_id": spec["spec_id"],
            "version": spec["version"],
            "git_commit": spec["git_commit"],
            "advertised_commit": spec["advertised_commit"],
            "board_target": spec["board"]["target"],
            "build_id": build["build_id"],
            "dds_topics_id": dds["contract_id"],
            "network_baseline_id": network["baseline_id"],
            "parameter_manifest_id": parameters["manifest_id"],
            "files": {
                "firmware": {"path": f"firmware/{firmware_path.name}", "sha256": _sha256(firmware_path)},
                "net_cfg": {"path": "microSD/net.cfg", "sha256": hashlib.sha256(render_net_cfg(network)).hexdigest()},
                "extras": {"path": "microSD/etc/extras.txt", "sha256": hashlib.sha256(render_extras(network)).hexdigest()},
                "parameters": {"path": "parameters/real.params", "sha256": _sha256(temporary / "parameters/real.params")},
            },
            "writes_performed": 0,
        }
        package = {"media_id": content_identity(body), **body}
        registry.validate("px4-release-media", package)
        (temporary / "release.json").write_bytes(canonical_json(package) + b"\n")
        instructions = (
            "PX4 RELEASE PROCEDURE\n\n"
            "1. Remove propellers and keep the vehicle disarmed.\n"
            "2. Connect PX4 by USB and use the USB release workflow to flash firmware/" + firmware_path.name + ".\n"
            "3. Use the same USB MAVLink session to apply parameters/real.params and write microSD/net.cfg, microSD/etc/iii-network-baseline.cfg, and microSD/etc/extras.txt.\n"
            "4. Reboot PX4, verify its disarmed firmware identity and the Pi-PX4 14541/UDP link, then reconnect the built-in Pi Ethernet port to PX4.\n"
            "5. Rerun the III deployment. The Pi stage is idempotent and the receiver will repeat the zero-write PX4 audit.\n"
            "6. If USB maintenance is unavailable, the microSD files remain a recovery fallback; do not create a second normal release path.\n"
        )
        (temporary / "README.txt").write_text(instructions, encoding="ascii")
        temporary.replace(destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return package
