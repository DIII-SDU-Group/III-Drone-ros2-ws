from __future__ import annotations

import json
from pathlib import Path
import subprocess

from iii_deployment.contracts import canonical_json, content_identity

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts/build/generate_px4_parameter_manifests.py"
REFERENCE = ROOT / "deployment/px4/reference-sitl-snapshot.json"
NETWORK_BASELINE = ROOT / "deployment/px4/network-baseline.json"
AIRFRAME = (
    ROOT
    / "PX4-Autopilot/ROMFS/px4fmu_common/init.d-posix/airframes/99999_gz_d4s_dc_drone"
)


def run_generator(inventory: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "python3",
            str(GENERATOR),
            "--inventory",
            str(inventory),
            "--airframe",
            str(AIRFRAME),
            "--network-baseline",
            str(NETWORK_BASELINE),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def test_decoded_reference_reproduces_both_release_manifests(tmp_path: Path) -> None:
    completed = run_generator(REFERENCE, tmp_path)
    assert completed.returncode == 0, completed.stdout
    for name in ("real.json", "sim.json", "reference-sitl-snapshot.json"):
        assert (tmp_path / name).read_bytes() == (
            ROOT / "deployment/px4" / name
        ).read_bytes()


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
