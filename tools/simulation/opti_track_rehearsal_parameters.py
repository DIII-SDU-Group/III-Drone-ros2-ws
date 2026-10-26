#!/usr/bin/env python3
"""Living `opti_track` parameters for an OptiTrack rehearsal.

A rehearsal qualifies the installed tracked default, not whatever values the
host's living configuration has retained. `prepare` sets the host's own
opti_track scope aside, seeds a fresh one from the installed default and sets
the relay's rigid-body ID (a boot-time constant the lab sets from the GUI).
`restore` puts the host's own scope back.

Usage: opti_track_rehearsal_parameters.py prepare <rigid_body_id> | restore
"""

import shutil
import sys
from pathlib import Path

import yaml

from iii_drone_configuration.schema_utils import (
    resolve_active_parameter_file,
    resolve_iii_config_dir,
    seed_runtime_configuration,
)

PROFILE = "opti_track"
SCOPE = (f"parameter_sets/{PROFILE}", f"profiles/{PROFILE}.yaml", f"state/{PROFILE}", f"shadows/{PROFILE}")


def remove_scope(root: Path) -> None:
    for relative in SCOPE:
        path = root / relative
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def prepare(rigid_body_id: int) -> None:
    root = Path(resolve_iii_config_dir())
    backup = root.parent / f"{root.name}.before-opti-track-rehearsal"
    if backup.exists():
        # A rehearsal's leftover scope; the host's own is already set aside.
        remove_scope(root)
    else:
        backup.mkdir(parents=True)
        for relative in SCOPE:
            if (root / relative).exists():
                (backup / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(root / relative, backup / relative)
    seed_runtime_configuration(PROFILE)
    path = Path(resolve_active_parameter_file(PROFILE))
    document = yaml.safe_load(path.read_text())
    document["/**"]["ros__parameters"]["/opti_track/pose_relay/rigid_body_id"] = rigid_body_id
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    print(f"installed default with rigid_body_id {rigid_body_id} in {path}")


def restore() -> None:
    root = Path(resolve_iii_config_dir())
    backup = root.parent / f"{root.name}.before-opti-track-rehearsal"
    if not backup.exists():
        return
    remove_scope(root)
    for relative in SCOPE:
        if (backup / relative).exists():
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(backup / relative, root / relative)
    shutil.rmtree(backup)


if __name__ == "__main__":
    if sys.argv[1:2] == ["prepare"] and len(sys.argv) == 3:
        prepare(int(sys.argv[2]))
    elif sys.argv[1:] == ["restore"]:
        restore()
    else:
        sys.exit(__doc__)
