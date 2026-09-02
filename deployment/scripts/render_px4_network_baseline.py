#!/usr/bin/env python3
"""Render the release-owned PX4 SD-card network files without overwriting drift."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.contracts import canonical_json  # noqa: E402
from iii_deployment.px4_network import (  # noqa: E402
    load_network_baseline,
    render_extras,
    render_net_cfg,
)


def _write_exact(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise RuntimeError(f"refusing to overwrite drifted PX4 artifact: {path}")
        return
    path.write_bytes(payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--baseline", default=ROOT / "deployment/px4/network-baseline.json", type=Path
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    baseline = load_network_baseline(
        args.baseline, schema_root=ROOT / "deployment/schemas/v1"
    )
    artifacts = {
        "net.cfg": render_net_cfg(baseline),
        "etc/extras.txt": render_extras(baseline),
    }
    for relative, payload in artifacts.items():
        _write_exact(args.output / relative, payload)
    receipt = {
        "schema": "iii.px4-network-render/v1",
        "baseline_id": baseline["baseline_id"],
        "artifacts": {
            relative: hashlib.sha256(payload).hexdigest()
            for relative, payload in sorted(artifacts.items())
        },
    }
    sys.stdout.buffer.write(canonical_json(receipt) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
