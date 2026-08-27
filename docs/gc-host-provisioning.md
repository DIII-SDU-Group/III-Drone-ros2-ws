# Ground-Control Host Provisioning

## Supported Boundary

`iii gc provision` converges the current graphical user on a normally installed
Ubuntu 22.04 or 24.04 x86_64 computer containing this workspace clone. It reports,
but never performs, Ubuntu installation, disk partitioning, full-disk encryption,
proprietary driver installation, or vendor-firmware updates. ARM64 operator hosts,
WSL, macOS, and Windows are rejected.

The host baseline has three independently reported managed categories:

- `operational`: Docker/Compose prerequisites, private host-user paths, the
  graphical-session units, mDNS tools, discovery, mirror, clock companion, and
  browser launcher;
- `application`: the ROS-free host-native companion/CLI environment and the
  transactional application-slot runtime; signed release bundles, not Ansible,
  install frontend/proxy images and QGroundControl binaries; and
- `development`: strict submodule policy, the repository-managed Ansible
  controller, offline/build caches, and the pinned ARM64 builder.

Everything outside the declared user paths is `unmanaged_user_state`: it is not
inventoried as drift and is not changed. The proxy/container boundary does not
install or import ROS, DDS, CycloneDDS, or MAVSDK.

## Controller Bootstrap And Retained Plan

The source wrapper recognizes `iii gc provision` and creates a content-versioned
controller beneath `~/.local/share/iii/controller/environments/`. It atomically
selects `controller/venv`; it does not make a devcontainer or repository-local
virtual environment authoritative. Old versioned environments are retained until
an explicit later cleanup policy removes them.

The stock-host seed uses Python's standard-library venv without `ensurepip`, then
authenticates pinned pip 26.2 before installing the Python 3.10 or 3.12
hash-locked controller graph. This avoids requiring `python3-venv` before the
retained host plan exists. The selected environment is built at its immutable
content address so generated entry-point shebangs never refer to a staging path.

Prime only the normal local sudo credential cache before applying. The password
is never accepted in CLI arguments, environment files, Ansible extra variables,
or retained operation records.

```bash
sudo -v
tools/III-Drone-CLI/bin/iii gc provision --dry-run --json
tools/III-Drone-CLI/bin/iii gc provision \
  --operation-id <retained-operation-id> --confirm --json
iii gc status --json
```

The retained plan binds the exact workspace, CLI, and GC branches/SHAs/scoped
state; all application and Ansible bytes; submodule lock; platform/user identity;
policy; controller executable; permissions; mutations; and offline/archive inputs.
Any change requires a fresh plan. Apply performs convergence, reauthenticates the
same plan, runs Ansible check mode, and fails unless the second run predicts zero
managed changes.

## Prepared-Offline Mode

An offline cache is an explicit directory containing canonical
`gc-offline-cache.json`. Its content identity covers each artifact byte, size,
relative path, supported platform, and role. These roles are all mandatory:

- `apt-packages`;
- `ansible-controller-wheelhouse`;
- `gc-runtime-wheelhouse`;
- `arm64-builder-image`.

The apt artifact is extracted into the local apt archive before an
`apt-get --no-download` transaction. Python uses `--no-index`. Release container
images are intentionally excluded: only a signed GC component bundle may import
them through the application slot manager. Cache identity contributes to the selected installed
environment identity, so a changed cache cannot reuse a stale success marker.
Bootstrap and convergence never use the network in prepared-offline mode.

Both Python wheelhouse archives are flat archives whose root contains every wheel
needed by the matching platform lock. The controller archive includes the
authenticated `pip-26.2-py3-none-any.whl`, the complete
`controller-requirements-py310.txt` or `controller-requirements-py312.txt` graph,
and exact `iii`/`iii-deployment` wheels. The GC runtime archive likewise contains
the complete matching `gc-runtime-requirements-py310.txt` or
`gc-runtime-requirements-py312.txt` graph plus exact `iii`,
`iii-drone-contracts`, `iii-drone-gc`, and `iii-deployment` wheels. Nested
wheelhouse directories are unsupported because the deliberately bounded
`--find-links` lookup is non-recursive.

Online local-project builds occur only from authenticated temporary copies; they
never create build metadata or wheels in the workspace clone. Offline mode never
builds source. The builder image must expose the planned builder-definition
SHA-256 label before the installed marker is
committed.

```bash
sudo -v
tools/III-Drone-CLI/bin/iii gc provision \
  --offline --offline-cache /media/III-FIELD-CACHE \
  --dry-run --json
tools/III-Drone-CLI/bin/iii gc provision \
  --offline --offline-cache /media/III-FIELD-CACHE \
  --operation-id <retained-operation-id> --confirm --json
```

The cache must match this computer's Ubuntu release. Cache creation/readiness is
owned by the field-preparation workflow; provisioning only consumes an exact,
already prepared cache.

## Transactional GC And QGroundControl Applications

A signed `gc` component bundle is the only application-install authority. Its
frontend and proxy images are built once, smoke-tested, and retained offline in
an OCI-layout archive carrying a Docker schema-v2 manifest. This deliberate media
type lets `skopeo copy --preserve-digests` import the exact recorded runtime
digest; conversion during field installation is rejected. Containers are
read-only, drop all capabilities, mount only the maintenance-drain marker, and
receive no Docker socket, QGC binary, host credential, `.iii` registry, signing
key, Ansible source, updater, or builder authority.

Stage without changing the selected application. Use `--protect-offline` when
the retained bundle is an operator-designated offline recovery set:

```bash
iii gc application stage --bundle /media/III-RELEASE/gc --protect-offline \
  --dry-run --json
iii gc application stage --bundle /media/III-RELEASE/gc --protect-offline \
  --operation-id <retained-operation-id> --confirm --json
iii gc application status --json
```

Activation reauthenticates the cached signature, every extracted payload byte,
both imported image digests, and the pinned QGC checksum before atomically
selecting the GC/QGC pair. Use one explicit safety source: `--disconnected`,
`--sim`, or a fresh authenticated `--safety-file` for a real aircraft. A real
target must be continuously landed, disarmed, owner-free, and fresh. The recovery
override requires a separate reason and the exact displayed warning; it cannot
waive a known armed, airborne, or active-owner state.

```bash
iii gc application activate --release-id <release-id> --disconnected --dry-run
iii gc application activate --release-id <release-id> --disconnected \
  --operation-id <retained-operation-id> --confirm
iii gc application rollback --disconnected --dry-run
iii gc application reconcile --dry-run
```

The browser proxy rejects new mutating HTTP requests with 503 and WebSocket
commands with retry-later code 1013 while the drain marker is active. A durable
journal restores the exact prior GC/QGC pair after process interruption, logout,
or reboot. Login runs reconciliation before starting the GC target. Activation
health failure rolls back immediately; cleanup failure after a healthy durable
commit is audited and deferred rather than reversing the working pair.

The cache protects the qualified anchor, active release, previous field release,
staged candidate, and explicitly protected offline sets. Only unprotected entries
are eligible under the 50-GiB limit, and pruning still preserves at least the
greater of 10 GiB or ten percent filesystem free space.

QGroundControl 5.0.8 is a checksum-pinned x86_64 AppImage in the same signed GC
bundle. The build proves the AppImage has no embedded update feed; release
transactions alone select its atomic content-addressed slot. The unit uses
`APPIMAGE_EXTRACT_AND_RUN=1`, so FUSE is not part of the host contract. QGC user
settings and logs remain outside slots in `~/.config/QGroundControl.org`,
`~/.local/share/QGroundControl`, and `~/Documents/QGroundControl`.

```bash
iii qgc status --json
iii qgc start --dry-run
iii qgc start --operation-id <retained-operation-id> --confirm
iii qgc stop --dry-run
```

Release activation stops QGC, takes an immutable backup of the exact existing
INI bytes (including the absence of a file), merges only the declared managed
keys, verifies the merge, switches the pinned application selector, and restores
the prior running/stopped state. Failure or interrupted-transaction reconciliation
restores both the prior QGC bytes and application pair. Local preferences,
geometry, paths, credentials, link definitions, and generated caches are never
release-owned settings. The managed baseline keeps public log/telemetry upload
disabled.

Explicit configuration workflows use the same retained-plan boundary:

```bash
iii qgc config apply --release-id <release-id> --qgc-version 5.0.8 \
  --profile real --dry-run --json
iii qgc config capture --release-id <release-id> --qgc-version 5.0.8 \
  --clean-exit --dry-run --json
iii qgc config diff --capture-id <capture-id> --json
iii qgc config promote --capture-id <capture-id> --key <managed-key> \
  --dry-run --json
iii qgc config verify-cache --cache-id <cache-id> --qgc-version 5.0.8 \
  --px4-firmware 1.16.1 --manifest-id <manifest-id> --json
```

Stopping the pinned QGC user unit also records a redacted immutable clean-exit
capture. Promotion fails for non-managed keys, unsafe upload values, sensitive
data, local preferences, host paths, geometry, and generated/cache data; it writes
only reviewed managed keys to the current normal feature branch and never commits
or pushes. `iii qgc config cache` stores generated ParamCache content under an
exact QGC/PX4/parameter-manifest compatibility identity instead of treating it as
hand-maintained policy.

`iii qgc` never starts or stops the GC application, and `iii gc` never starts or
stops QGC. Paired field deployment activates and health-checks GC first, then
submits the drone component. A GC failure leaves the drone untouched. If drone
activation fails, the new GC remains only when its declared contracts overlap the
restored drone; otherwise the exact prior GC release is restored as well.

## Persistent User State And Secrets

The role declares and preserves these owner-only roots:

- `~/.config/iii`: settings, machine identity, credentials, and key directories;
- `~/.local/state/iii`: record registry, GC state, and logs;
- `~/.local/share/iii`: installed runtime/controller state; and
- `~/.cache/iii`: disposable offline/build/controller caches.

The policy explicitly creates owner-only capture, registry, log, credential,
machine-identity, SSH-key, and signing-key directories. The workspace-local,
Git-ignored `.iii/` registry used by interactive CLI sessions is intentionally
outside convergence content: Ansible neither removes nor rewrites it. Login-scoped
companions use the explicit host registry at `~/.local/state/iii/registry`, so
their captures never depend on a container filesystem.

`gc-profile.env` and `ground-control-secrets.env` are created only when absent and
are never overwritten on reconvergence. The browser secret remains an independent
human secret. Runtime credentials and signing keys are enrolled through
`iii access`; the provisioning role never fabricates, copies, or archives them.
It creates a fresh owner-only machine identity and per-computer SSH key when they
are absent.

For a replacement computer, import the verified P2.T8 archive before enrollment:

```bash
sudo -v
tools/III-Drone-CLI/bin/iii gc provision \
  --replacement-archive /media/III-RECORDS/records.tar \
  --dry-run --json
```

Replacement mode refuses any existing machine identity, SSH private key, or
runtime credential. The portable archive is path-safe, integrity-checked, and
secret-scanned before apply. It restores only verified non-secret records/caches;
Ansible then creates fresh local identity/key material. Complete enrollment from
an already authorized computer and verify the new computer before revoking the
old one. Never copy the prior computer's private key or machine credential.

## Login, Logout, And Target Selection

`iii-gc.target` is wanted by and part of `graphical-session.target`; user lingering
is not enabled. Login starts and restarts the proxy, frontend, fixed-aircraft
discovery, configuration mirror, and clock companion. Logout stops those local
services through the graphical-session relationship. No unit sends a stop,
shutdown, or lifecycle request to the drone.

Automatic discovery is hard-bound to `iii.local`. The clock companion invokes the
authenticated receiver clock operation once when a newly present target reports
profile `real`; `sim` is explicitly recorded as skipped. Manual proxy endpoints
never trigger automatic clock synchronization or unattended mirror writes.

Neither a browser nor QGroundControl starts at login. The desktop launcher and
`iii gc open` explicitly open the local frontend. `iii gc start/stop/restart/status`
operate only the III frontend/proxy/discovery/mirror/clock/browser stack. Every
QGroundControl lifecycle/configuration operation remains under `iii qgc`.

```bash
iii gc start --dry-run
iii gc start --operation-id <id> --confirm
iii gc open --dry-run
iii gc open --operation-id <id> --confirm
iii gc status --json
iii gc stop --dry-run
```

The QGroundControl desktop launcher is likewise explicit and routes to the same
pinned host-native binary used for real and simulation operation.

Before field use, set the expected target profile/runtime/system IDs in the
preserved `~/.config/iii/gc-profile.env` only after positive identity review. The
unconfigured baseline can discover sim or real; simulation never enters the Pi
clock gate.

## Verification Boundary

Automated acceptance covers both supported Ubuntu releases, first convergence,
zero second-run drift, classified injected drift/repair, user-path and secret
permissions, unit topology, no automatic browser/QGC, fixed-host discovery,
real/sim clock behavior, offline cache tamper/platform rejection, and replacement
archive secret exclusion. A real graphical login/logout and replacement-computer
enrollment/import drill remain commissioning evidence on the actual laptops; no
container result is represented as that physical evidence. The application path
also has real Docker/BuildKit/skopeo digest-preservation checks, an Ubuntu native
AppImage launch probe, and a PX4 SITL-to-host UDP 14550 MAVLink probe; those do not
replace the final physical laptop login/reboot commissioning row.
