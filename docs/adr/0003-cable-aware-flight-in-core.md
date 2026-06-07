# Cable-Aware Flight Belongs In Core

Cable-aware flight changes the autonomy and planning behavior of the drone, so it belongs in `III-Drone-Core` as a distinct maneuver type backed by trajectory-generator support rather than as an MCP, GUI, or Operations Controller shortcut. Operations and mission tooling may expose the resulting typed action, but the collision-aware planning algorithm and perception-dependent validation live in the core control/perception layer.
