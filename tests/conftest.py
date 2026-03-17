import os
import sys
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]

for relative_path in [
    "tools/III-Drone-CLI",
    "src/III-Drone-Core",
    "src/III-Drone-Configuration",
    "src/III-Drone-GC",
    "src/III-Drone-Supervision",
]:
    candidate = WORKSPACE_ROOT / relative_path
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault("CLI_CONFIGURATION", "host")
