from __future__ import annotations

import json
from pathlib import Path
import subprocess

from iii_deployment.contracts import canonical_json, content_identity

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts/build/generate_px4_parameter_manifests.py"
REFERENCE = ROOT / "deployment/px4/reference-sitl-snapshot.json"
COMMISSIONED_REFERENCE = ROOT / "deployment/px4/reference-commissioned-fmu-snapshot.json"
NETWORK_BASELINE = ROOT / "deployment/px4/network-baseline.json"
AIRFRAME = (
    ROOT
    / "PX4-Autopilot/ROMFS/px4fmu_common/init.d-posix/airframes/99999_gz_d4s_dc_drone"
)


def run_generator(
    inventory: Path, output: Path, *, commissioned_real: bool = False
) -> subprocess.CompletedProcess[str]:
    arguments = [
            "python3",
            str(GENERATOR),
            "--inventory",
            str(inventory),
            "--network-baseline",
            str(NETWORK_BASELINE),
            "--output",
            str(output),
        ]
    if commissioned_real:
        arguments.append("--commissioned-real")
    else:
        arguments.extend(["--airframe", str(AIRFRAME)])
    return subprocess.run(
        arguments,
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def test_decoded_reference_reproduces_both_release_manifests(tmp_path: Path) -> None:
    completed = run_generator(REFERENCE, tmp_path)
    assert completed.returncode == 0, completed.stdout
    for name in ("sim.json", "reference-sitl-snapshot.json"):
        assert (tmp_path / name).read_bytes() == (
            ROOT / "deployment/px4" / name
        ).read_bytes()
    commissioned = run_generator(
        COMMISSIONED_REFERENCE, tmp_path / "commissioned", commissioned_real=True
    )
    assert commissioned.returncode == 0, commissioned.stdout
    for name in ("real.json", "reference-commissioned-fmu-snapshot.json"):
        assert (tmp_path / "commissioned" / name).read_bytes() == (
            ROOT / "deployment/px4" / name
        ).read_bytes()


def test_accumulated_flight_time_is_preserved_across_releases(tmp_path: Path) -> None:
    value = json.loads(REFERENCE.read_text(encoding="utf-8"))
    for item in value["parameters"]:
        if item["name"] == "LND_FLIGHT_T_LO":
            item["value"] = 15_088_000
    value["snapshot_id"] = content_identity(
        {
            "profile": value["profile"],
            "target": value["target"],
            "parameter_count": value["parameter_count"],
            "parameters": value["parameters"],
        }
    )
    source = tmp_path / "flown-sitl.json"
    source.write_bytes(canonical_json(value) + b"\n")

    completed = run_generator(source, tmp_path / "output")

    assert completed.returncode == 0, completed.stdout
    manifest = json.loads((tmp_path / "output/sim.json").read_text(encoding="utf-8"))
    by_name = {item["name"]: item for item in manifest["parameters"]}
    for name in ("LND_FLIGHT_T_HI", "LND_FLIGHT_T_LO"):
        assert by_name[name]["classification"] == "calibration-identity"
        assert by_name[name]["enforcement"] == "preserve"
        assert by_name[name]["value"] is None


def test_generator_rejects_nonintegral_integer_after_identity_valid_decode(
    tmp_path: Path,
) -> None:
    value = json.loads(REFERENCE.read_text(encoding="utf-8"))
    integer = next(item for item in value["parameters"] if item["mav_type"] == "INT32")
    integer["value"] = -1.5
    value["snapshot_id"] = content_identity(
        {
            "profile": value["profile"],
            "target": value["target"],
            "parameter_count": value["parameter_count"],
            "parameters": value["parameters"],
        }
    )
    source = tmp_path / "unsafe.json"
    source.write_bytes(canonical_json(value) + b"\n")

    completed = run_generator(source, tmp_path / "output")

    assert completed.returncode != 0
    assert "complete identity-valid decoded PX4 snapshot" in completed.stdout
