# Configuration System

## 1. Purpose

`iii_drone_configuration` provides centralized parameter governance for the whole stack:
- file-backed parameter definitions
- runtime declaration and distribution
- validation and constraints
- persistence and profile switching

## 2. Key Components

1. `configuration_server_node.py`
- Main configuration authority node.
- Provides service API for declare/get/save/load/set operations.
- Handles active parameter file selection with simulation awareness.

2. `parameter_handler.py`
- Parses YAML into structured parameter entries.
- Validates types, ranges, options, and expression-based constraints.
- Detects changed parameters when loading new files.

3. `configurator` abstractions (Python and C++)
- Client-side utilities to declare/get parameters by bundle.
- Used extensively by C++ nodes through `Configurator<T>` patterns.

4. `configuration_client_node.py`
- Client utility (text UI style) for interacting with config services.

## 3. File Model

### 3.1 Runtime ROS Params (`config/ros_params_real.yaml`, `config/ros_params_sim.yaml`)
Contain startup and runtime parameter values for real and simulation modes, including support parameters such as:
- `parameters_path_postfix`
- `default_parameter_file`
- `sim_parameter_file`
- `parameter_snapshots_path_postfix`
- `default_snapshot_file`
- `sim_snapshot_file`
- `use_sim_time`

### 3.2 Schema Manifest (`config/parameters/parameter_manifest.yaml`)
The schema manifest stores managed parameter definitions with:
- `type`
- `value`
- optional: `constant`, `min`, `max`, `options`

### 3.3 Snapshot Files (`$CONFIG_BASE_DIR/iii_drone/parameter_snapshots/*.yaml`)
Saved runtime snapshots use the normal ROS parameter-file format and are managed by the optional configuration server.

## 4. Runtime Selection Logic

Configuration server chooses parameter file by `SIMULATION` env:
- `SIMULATION=false` -> `default_parameter_file`
- `SIMULATION=true` -> `sim_parameter_file`

Paths are resolved under:
- `$CONFIG_BASE_DIR/iii_drone/...`

## 5. Service Contract Role

Configuration server services are used by:
- core nodes during configuration
- mission and control nodes via configurator access
- supervision/GC tools for live parameter management and profile switching

High-value services in operations:
- `load_parameters` (load snapshot)
- `save_parameters` (persist current)
- `set_parameter_from_gc` (operator intervention)

## 6. Validation Semantics

`ParameterHandler` enforces:
- strict parameter naming rules
- declared type/value consistency
- range checks and options checks
- cross-parameter expression references (e.g., min/max based on other values)

Implication:
- Parameter files encode both values and constraints, not only flat config values.

## 7. Operational Importance

This subsystem is foundational. Failures here can block bringup of nearly all dependent nodes because configurator lookups and parameter declarations are deep in startup paths.
