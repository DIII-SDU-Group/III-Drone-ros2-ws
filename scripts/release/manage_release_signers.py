#!/usr/bin/env python3
"""Generate and rotate public release trust without transporting private keys."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment" / "src"))

from iii_deployment.contracts import ContractError, ContractRegistry, canonical_json  # noqa: E402
from iii_deployment.signers import (  # noqa: E402
    add_trusted_signer,
    generate_signer,
    load_trusted_signers,
    revoke_trusted_signer,
    signer_proof,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate")
    generate.add_argument(
        "--authority",
        choices=("ci-qualified", "workstation-field", "release-status"),
        required=True,
    )
    generate.add_argument("--private-key", type=Path, required=True)
    generate.add_argument("--public-descriptor", type=Path, required=True)
    proof = commands.add_parser("prove")
    proof.add_argument("--private-key", type=Path, required=True)
    add = commands.add_parser("add")
    add.add_argument("--store", type=Path, required=True)
    add.add_argument("--public-descriptor", type=Path, required=True)
    add.add_argument("--proof", type=Path, required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--store", type=Path, required=True)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("--store", type=Path, required=True)
    revoke.add_argument("--signer-id", required=True)
    return parser


def _render(value: dict, as_json: bool) -> None:
    if as_json:
        print(canonical_json(value).decode("utf-8"))
        return
    if "signers" in value:
        for signer in value["signers"]:
            print(f"{signer['signer_id']} {signer['authority']} {signer['state']}")
    elif "proof" in value:
        print(canonical_json(value).decode("utf-8"))
    else:
        print(f"{value['signer_id']} {value['authority']}")


def main() -> int:
    args = _parser().parse_args()
    registry = ContractRegistry(ROOT / "deployment" / "schemas" / "v1")
    try:
        if args.command == "generate":
            value = generate_signer(
                args.private_key,
                args.public_descriptor,
                authority=args.authority,
                registry=registry,
                forbidden_roots=(ROOT,),
            )
        elif args.command == "prove":
            value = signer_proof(args.private_key)
        elif args.command == "add":
            try:
                proof = json.loads(args.proof.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ContractError(f"cannot load signer proof: {exc}") from exc
            if not isinstance(proof, dict):
                raise ContractError("signer proof must contain an object")
            value = add_trusted_signer(
                args.store, args.public_descriptor, proof, registry
            )
        elif args.command == "list":
            value = load_trusted_signers(args.store, registry)
        else:
            value = revoke_trusted_signer(args.store, args.signer_id, registry)
        _render(value, args.json)
        return 0
    except (ContractError, OSError) as exc:
        print(
            json.dumps({"outcome": "rejected", "error": str(exc)}, sort_keys=True)
            if args.json
            else f"REJECTED: {exc}",
            file=sys.stderr,
        )
        return 30


if __name__ == "__main__":
    raise SystemExit(main())
