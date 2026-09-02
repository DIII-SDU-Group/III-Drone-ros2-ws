"""Release-owned PX4 Ethernet network and dual-transport baseline."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .contracts import ContractError, ContractRegistry, canonical_json, content_identity


class PX4NetworkBaselineError(RuntimeError):
    """The PX4 network baseline is unauthenticated or internally inconsistent."""


def render_net_cfg(baseline: Mapping[str, Any]) -> bytes:
    """Render the exact PX4 ``/fs/microsd/net.cfg`` payload."""

    network = baseline["network"]
    return (
        f"DEVICE={network['device']}\n"
        f"BOOTPROTO={network['boot_protocol']}\n"
        f"NETMASK={network['netmask']}\n"
        f"IPADDR={network['px4_address']}\n"
        f"ROUTER={network['router']}\n"
        f"DNS={network['dns']}\n"
    ).encode("ascii")


def render_extras(baseline: Mapping[str, Any]) -> bytes:
    """Render startup commands that share Ethernet without serial-owner clashes."""

    mavlink = baseline["transports"]["mavlink"]
    dds = baseline["transports"]["uxrce_dds"]
    ftp = " -x" if mavlink["ftp_enabled"] else ""
    return (
        "set +e\n"
        f"mavlink start{ftp} -u {mavlink['local_port']} "
        f"-o {mavlink['remote_port']} -t {mavlink['remote_address']} "
        f"-m {mavlink['mode']} -r {mavlink['max_rate_bytes_s']}\n"
        f"uxrce_dds_client start -t udp -p {dds['agent_port']} "
        f"-h {dds['agent_address']}\n"
        "set -e\n"
    ).encode("ascii")


def load_network_baseline(path: Path, *, schema_root: Path) -> dict[str, Any]:
    """Load and authenticate the canonical baseline plus its rendered artifacts."""

    if path.is_symlink() or not path.is_file():
        raise PX4NetworkBaselineError("PX4 network baseline is missing or linked")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PX4NetworkBaselineError(
            f"cannot read PX4 network baseline: {exc}"
        ) from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise PX4NetworkBaselineError("PX4 network baseline is not canonical JSON")
    validate_network_baseline(value, ContractRegistry(schema_root))
    return value


def validate_network_baseline(
    value: Mapping[str, Any], registry: ContractRegistry
) -> None:
    """Authenticate an already-loaded PX4 network baseline."""

    try:
        registry.validate("px4-network-baseline", value)
    except ContractError as exc:
        raise PX4NetworkBaselineError(str(exc)) from exc
    expected_id = content_identity(
        {key: item for key, item in value.items() if key != "baseline_id"}
    )
    if value["baseline_id"] != expected_id:
        raise PX4NetworkBaselineError("PX4 network baseline identity mismatch")
    parameters = value["parameter_requirements"]
    if parameters["MAV_2_CONFIG"] != 0 or parameters["UXRCE_DDS_CFG"] != 0:
        raise PX4NetworkBaselineError("automatic PX4 Ethernet owners must be disabled")
    artifacts = value["artifacts"]
    observed = {
        "net_cfg_sha256": hashlib.sha256(render_net_cfg(value)).hexdigest(),
        "extras_sha256": hashlib.sha256(render_extras(value)).hexdigest(),
    }
    if any(artifacts[name] != digest for name, digest in observed.items()):
        raise PX4NetworkBaselineError("PX4 network baseline artifact hash mismatch")
