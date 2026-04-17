# Findings, Risks, And Clarifications

## 1. Key Findings

Confirmed by maintainer clarification:
- Canonical bringup is III CLI driven (`iii system boot`) with tmux session bootstrapping.
- Supervisor then performs lifecycle/state orchestration on running nodes/wrappers.
- Sim profile uses a managed wrapper that launches simulated sensors (`sensors_sim_launch.yaml`).
- Real profile handles camera and mmWave as separate supervised nodes.

1. Launch composition mismatch in main launch:
- `iii_drone.launch.py` creates `control` launch include but currently does not add it to `launch_list` (commented out).
- If unchanged, control nodes may not start from the primary launch path.

2. Missing referenced launch file:
- Non-simulation branch in `iii_drone.launch.py` references `iii_drone_core/launch/sensors.launch.py`.
- That file is not present in `src/III-Drone-Core/launch/`.

3. Configuration file path model is environment-coupled:
- Most launch and runtime code assumes correctly prepared `$CONFIG_BASE_DIR/iii_drone` structure.
- Bringup can fail if install scripts were not run or env vars are incomplete.

4. Simulation stack references mixed naming eras:
- Documentation/scripts include both `gazebo-classic` and `gz/Garden` style references.
- Potential operator confusion if not standardized.

5. Mission specification currently looks test-oriented:
- Default `mission_specification.yaml` references `takeoff_test_tree.xml` and `up_down_test_tree.xml` sequence.
- This may not represent production mission behavior.

6. Submodule/version alignment is critical and tight:
- Many repositories are pinned to DIII v2.2-related branches/tags with local deviations.
- Inconsistent checkout can break ABI/API compatibility (especially interfaces + mission/core + PX4 libs).

## 2. Technical Risks

1. Runtime startup fragility:
- Due to layered env scripts, multiple launch entry points, and dependency on external config files.

2. High cross-package coupling:
- Interface changes can cascade across nearly all packages.

3. Runtime split-brain risk between CLI boot and direct launch usage:
- If users bypass CLI boot assumptions, supervisor lifecycle actions may fail against missing processes.

4. Potential dead paths:
- Unused or stale BT XML files and stale launch references may hide dormant assumptions.

## 3. Clarification Questions (Remaining)

1. Control launch inclusion:
- Is the commented-out control inclusion in `iii_drone.launch.py` intentional, or should control always be started from this launch file?

2. Missing `sensors.launch.py`:
- Should this file exist in `III-Drone-Core`, or has real-hardware sensor startup fully moved to supervision-managed wrappers?

3. Mission default:
- Is current `mission_specification.yaml` intentionally test-focused, or should we switch docs/code defaults to production cable workflow trees?

4. Simulation baseline:
- Which simulation stack is authoritative: Gazebo Garden (`gz`) only, or do you still support/expect Gazebo Classic launch paths?

5. Configuration source of truth:
- Should runtime parameter files be maintained in `src/III-Drone-Configuration/config/parameters/*` only, or in installed `$CONFIG_BASE_DIR/iii_drone/parameters/*` as authoritative copies?

## 4. Suggested Immediate Cleanup Targets

1. Resolve launch inconsistency (`control` include and missing `sensors.launch.py` reference).
2. Declare one canonical bringup workflow per mode (`sim`, `real`, `opti_track`).
3. Pin and document authoritative mission spec profile for non-test operation.
4. Add a startup validation command that checks env/config file presence before launch.
