# Scripts Layout

Scripts support local development, simulation, and ordinary Git maintenance.
There is no release-bundle, signing, receiver, or deployment-policy script
surface. For a Pi, use `iii host provision`, `iii deploy dev`, and regular SSH.

- `scripts/workspace/`: local development and test helpers.
- `scripts/git/`: ordinary repository helpers and submodule lock maintenance.
- `scripts/remote/`: retired compatibility shims only.

PX4 is built with its native build commands in `PX4-Autopilot/`; ROS packages
are built with the normal workspace `colcon` commands.
