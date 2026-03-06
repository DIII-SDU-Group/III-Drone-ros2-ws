# Ground Control And Operator Tools

## 1. Ground Control Package (`iii_drone_gc`)

### 1.1 Purpose
Provides an operator-facing GUI and ROS2 client layer to:
- monitor system/maneuver/perception/payload status
- invoke service-based commands
- manage runtime parameters

### 1.2 Main Components
- `gc_node.py`: ROS communications backbone.
- `gui.py`: Tkinter GUI implementation.

### 1.3 Data It Consumes
Subscriptions include:
- combined drone awareness
- current maneuver and target data
- trajectory path and reference mode
- battery/charging/gripper telemetry
- powerline map/state/status topics
- parameter events

### 1.4 Commands It Issues
Service clients include:
- gripper command
- PL mapper command
- powerline overview update
- configuration server APIs for parameter get/save/load/set

## 2. CLI And Supporting Tools

### 2.1 III-Drone-CLI
Installed from `tools/III-Drone-CLI` (submodule), used in post-start/install scripts.

Canonical system bringup path is via CLI, specifically `iii system boot`, which starts tmux-defined process layouts.

### 2.2 Workspace Scripts
Common tasks scripted in top-level `scripts/` and `setup/`:
- environment setup (dev/real/remote)
- package/executable discovery
- remote install and SSH prep
- post-create/post-start automation

### 2.3 Tmuxinator Integration
`setup/tmuxinator_config.bash` + `src/III-Drone-Core/tmuxinator/*` indicate standardized session layouts for dev/real and HITL-like workflows.

There are also top-level tmuxinator definitions under `tools/tmuxinator/` used by CLI workflows (for example dev/sim launch profiles).

## 3. Operator/Developer Workflow Pattern

Typical flow implied by scripts/docs:
1. source environment profile (`setup_dev.bash` or `setup_real.bash`)
2. ensure configuration and simulation assets installed
3. build workspace
4. start supervisory/system launch
5. start GUI for live operations and parameter adjustment

## 4. Practical Importance

Ground control is not only visualization; it is an active control and configuration interface. This makes it operationally critical and a potential safety/coordination surface.
