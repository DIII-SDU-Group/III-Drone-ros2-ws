# Build And Environments

The workspace uses the devcontainer as the development OS image. Onboard runtime is native Linux with ROS 2 processes owned by the III daemon. The devcontainer boots systemd and runs the III daemon as a system service so development commands exercise the same runtime ownership model.

Container images remain useful for:
- development (`Dockerfile.dev` + devcontainer)
- dependency/bootstrap reference (`Dockerfile`)
- production ARM64 cross-compilation (`Dockerfile.cc`)

## 1. Build System

Primary build system: `colcon` with workspace defaults (`defaults.yaml`).

Observed default behavior:
- Base path: `src`
- Skip regex: `example_*`

Typical build command pattern in scripts:
- `colcon build --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`

## 2. Environment Profiles (`setup/*.bash`)

The workspace defines explicit runtime modes via shell profiles:

1. `setup_dev.bash`
- Sets `SIMULATION=true`
- Sets `III_SYSTEM_PROFILE=sim`
- Loads paths, remote settings, log levels, ROS middleware variables
- Sets `COLCON_HOME` to workspace

2. `setup_real.bash`
- Intended for native deployment/runtime on the target platform
- Sets `SIMULATION=false`
- Sets `III_SYSTEM_PROFILE=real`
- Sources the installed ROS/workspace setup expected on the target OS

3. `setup_remote.bash`
- Remote tooling profile for deployment/SSH workflow. Remote runtime-control
  commands use `iii-runtime-api` with `III_RUNTIME_API_URL` and
  `III_RUNTIME_API_CLI_TOKEN`; SSH remains for deploy/sync/admin tasks.

Shared env and path conventions:
- `CONFIG_BASE_DIR`
- `NODE_MANAGEMENT_CONFIG_DIR`
- `III_SYSTEM_RUNTIME_DIR`
- `III_SYSTEM_DAEMON_SOCKET`
- `III_SYSTEM_DAEMON_LOG`
- `RMW_IMPLEMENTATION=rmw_cyclonedds_cpp`

Mission specifications and behavior trees are installed, content-addressed
package assets. Runtime selection uses mission catalog IDs and does not depend on
source-tree path environment variables.

## 3. Development Container

`.devcontainer/devcontainer.json` points to `Dockerfile.dev` and mounts:
- workspace into `/home/iii/ws`
- X11 sockets/authority for GUI apps
- docker socket (docker-outside-of-docker)
- host network/IPC and privileged mode
- GPU runtime settings (NVIDIA)

`.dockerignore` keeps generated build outputs, PX4 checkouts/builds, logs, runtime state, and VCS metadata out of the image build context. Those files are still available in the running devcontainer through the workspace bind mount.

The devcontainer runs container-local systemd as PID 1. VS Code terminals still use the `iii` user through `remoteUser`, while the container process itself starts as root so systemd can run normally.

Post hooks:
- `.devcontainer/post_create.sh`
- `.devcontainer/post_start.sh`

`post_start.sh` builds the workspace, installs `tools/systemd/iii-system-daemon.service` into `/etc/systemd/system`, enables it, and restarts it.

## 4. Dockerfile Layers

1. `Dockerfile.dev`
- ROS Jazzy desktop-full base
- configures apt retries/timeouts for transient mirror failures
- rewrites Ubuntu archive/security apt sources to HTTPS while leaving ROS package sources on HTTP
- installs stable OS, ROS, and development tooling in an early apt layer using `--no-install-recommends`
- installs Python requirements
- installs QGroundControl AppImage + dev tools
- installs GUI/simulation operator packages, the runtime API service
  dependencies, and workspace ROS/runtime package dependencies in late apt
  layers so package additions do not invalidate the expensive stable layers

2. `Dockerfile`
- Runtime/base image with ROS Jazzy base
- workspace and CLI installation reference
- not the primary onboard process-supervision boundary

3. `Dockerfile.cc`
- Reproducible amd64-to-arm64 cross-compilation image
- digest-pinned Ubuntu 24.04 builder and ROS Jazzy target seed
- snapshot-pinned GCC 13.3 toolchain and generated, aircraft-independent sysroot

### Canonical ARM64 target

`deployment/targets/v1/raspberry-pi-5-noble-arm64.json` is the single,
content-addressed target definition. It fixes Raspberry Pi 5, Ubuntu 24.04
Noble, AArch64, ROS Jazzy, CPython 3.12/cp312, glibc 2.39, and GCC 13.3. Both
OCI inputs and every cross-builder package are immutable inputs. The target
sysroot comes from the pinned ARM64 ROS image; copying `/`, `/home`, package
state, logs, or configuration from an aircraft is forbidden.

The Q93 Ansible baseline owns Ubuntu, ROS, system Python, platform/hardware
libraries, systemd, udev, firmware, and drivers. A release owns only the III
install tree, private release libraries, compatible cp312 wheels, missions,
and its release environment. Normal deployment must not invoke a package
manager or replace host-owned ABI components.

Run the executable compatibility proof with:

```bash
PYTHONPATH=deployment/src python3 scripts/build/run_target_abi_probe.py
```

The command compiles an AArch64 binary with the pinned cross-compiler, executes
it in the pinned ARM64 target image, validates OS/ROS/Python/libc/compiler ABI,
and fails before any transfer or activation when the release target differs.

### Dirty source capture

`deployment/source-policy.json` declares the workspace and ten editable III
repositories, relevant workspace source roots, explicit sensitive/generated/
dataset exclusions, and the dependency-complete GC/drone impact graph. Capture
a field-development candidate before building it:

```bash
PYTHONPATH=deployment/src python3 scripts/release/capture_source_snapshot.py \
  --output /private/release/source-snapshot.json \
  --report /private/release/source-provenance.md
```

The snapshot hashes current tracked file contents (including deletions),
relevant non-ignored untracked source, every governed repository, and the
dependency lock. Its identity is content-based rather than commit-based, so
the same bytes produce the same identity. Ignored build/log trees, datasets,
unrelated files, and untracked secrets are omitted explicitly; tracked secrets,
unsafe links, unmerged indexes, missing repositories, and unclassified artifact
impact fail closed. The Markdown report is mandatory provenance for a field-
development release. A caller requesting components must pass all inferred
components; omitting either side of a shared-contract change is rejected.

### Cached ARM64 release build

The production ARM64 builder is an offboard-only workflow. It permits only a
local Docker transport, uses an aircraft-independent immutable sysroot, and
never runs SSH, package-management, or build commands on the drone. A normal
build first materializes the committed, hash-locked cp312/ARM64 wheelhouse
without dependency resolution:

```bash
wheelhouse="$(mktemp -d /tmp/iii-arm64-wheelhouse.XXXXXX)"
PYTHONPATH=deployment/src python3 scripts/build/materialize_arm64_wheelhouse.py \
  --wheelhouse "$wheelhouse" \
  --lock deployment/python-wheel-lock.json
```

Updating the wheel lock is a separate, reviewable maintenance operation. It
uses the digest-pinned resolver image and exact versions in
`deployment/python/requirements.in`:

```bash
candidate="$(mktemp -d /tmp/iii-arm64-wheel-candidate.XXXXXX)"
PYTHONPATH=deployment/src python3 scripts/build/resolve_arm64_wheels.py \
  --requirements deployment/python/requirements.in \
  --wheelhouse "$candidate/wheels" \
  --lock "$candidate/python-wheel-lock.json"
```

Review both the candidate lock and its wheel hashes before replacing the
committed lock. The normal build does not resolve versions and rejects missing,
additional, incompatible, or hash-mismatched wheels.

Capture the exact live source state immediately before building, then use
persistent private cache and output directories outside the checkout:

```bash
evidence="$(mktemp -d /tmp/iii-arm64-source.XXXXXX)"
cache="/private/iii-build-cache/arm64"
output="/private/iii-releases/candidate"

PYTHONPATH=deployment/src python3 scripts/release/capture_source_snapshot.py \
  --output "$evidence/source-snapshot.json" \
  --report "$evidence/source-provenance.md" \
  --component drone --component gc

PYTHONPATH=deployment/src python3 scripts/build/build_arm64_release.py \
  --snapshot "$evidence/source-snapshot.json" \
  --component drone --component gc \
  --cache "$cache" \
  --output "$output" \
  --wheelhouse "$wheelhouse" \
  --wheel-lock deployment/python-wheel-lock.json
```

The builder recaptures live source and rejects a stale snapshot. Package keys
select impacted III packages and downstream dependants; ccache survives safe
package-build invalidation. The output is a non-symlinked isolated colcon tree
under `install/<package>`, a plain `python/cp312/site-packages` tree, installed
runtime assets, and `bin/iii-release-env`. It contains no source, build, log, or
escaping symlink tree. Python imports and every ELF dependency are checked in
the pinned ARM64 target image with `--network none`; builder/sysroot paths,
unresolved libraries, invalid RUNPATHs, and undeclared host libraries fail the
build. Only a release that passes every check receives `build-record.json` and
is atomically renamed to the requested output path. A failed partial directory
is diagnostic evidence and is never packageable as a complete release.

## 5. Entrypoints

- `entrypoint_dev.sh`: source ROS + workspace install if exists.
- `entrypoint_real.sh`: source native ROS Jazzy and the atomically activated release.
- `entrypoint_cc.sh`: expose the immutable ARM64 Jazzy sysroot to build tools.

## 6. Dependency Installation Strategy

Dependency sources:
- stable apt dependencies are listed directly in the Dockerfiles
- workspace ROS/runtime apt dependencies are listed directly in late Dockerfile layers
- Python dependencies via top-level `requirements.txt`
- package-level CMake dependencies and Python package setup
- workspace post-create script runs PX4's setup script for simulation tooling with the NuttX firmware toolchain disabled, installs Gazebo assets, then runs `rosdep install --from-paths src --ignore-src -y`

## 7. Operational Tooling

Workspace scripts provide utility for:
- package/executable discovery
- remote install/setup
- devcontainer startup behavior
- docker compose builds
- GUI v2 full-suite and sim E2E smoke verification

Operational bringup typically uses III CLI commands after environment profile sourcing, rather than relying on a single direct launch file.

Development-host operation is exposed through the workspace-root `./iii-dev`
bridge. It discovers the associated devcontainer, sources the development
profile inside it, and delegates to the existing simulation launcher and III
CLI. See [`host-development-commands.md`](host-development-commands.md).

On the real drone, workspace-owned Ansible assets under `deployment/` install the
native `systemd` units and stable launchers. Inside the devcontainer, the workspace
installs the development unit automatically and `iii system boot` uses
`systemctl start iii-system-daemon.service`.

Runtime ownership is:

- native `systemd` owns the III daemon onboard and in the devcontainer
- the III daemon owns ROS launch processes and daemon-managed services
- `iii-runtime-api` runs on the runtime host as the GUI v2/remote CLI network
  control plane
- the GC proxy/frontend run on the ground-control computer and do not require
  ROS, DDS, MAVSDK, or runtime Python packages
- PX4 hardware, PX4 SITL/Gazebo, and QGroundControl are external to III supervision

GUI v2 compose entrypoints:

```bash
docker compose -f src/III-Drone-GC/docker-compose.dev.yml config
docker compose -f src/III-Drone-GC/docker-compose.prod.yml config
III_GC_FRONTEND_PORT=5174 scripts/workspace/gui_v2_sim_e2e_smoke.py --start-compose
```

The smoke runner and its calibrated fixture resolver are simulation-only. Real
inspection startup and manual data acquisition follow the authoritative
[`field-inspection-operations.md`](field-inspection-operations.md) procedure.

## 8. Build/Runtime Observations

- Build graph includes external fetched dependencies (notably `yaml-cpp` via CMake `FetchContent` in config and mission packages).
- Runtime heavily depends on correct environment variable initialization before launch.
- Configuration files under `CONFIG_BASE_DIR/iii_drone` are first-class runtime dependencies, not optional.
