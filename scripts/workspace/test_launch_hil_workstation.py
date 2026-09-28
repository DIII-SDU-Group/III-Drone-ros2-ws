from pathlib import Path
import os
import subprocess
import time

import pytest


LAUNCHER = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "simulation"
    / "launch_hil_workstation.sh"
)


def launcher_function(source: str, name: str, next_name: str) -> str:
    return f"{name}() {{" + source.split(f"{name}() {{", 1)[1].split(
        f"\n{next_name}() {{", 1
    )[0]


def test_split_host_hil_assigns_sitl_a_distinct_xrce_client_key() -> None:
    """The physical PX4 owns key 1; simultaneous SITL must not replace it."""
    source = LAUNCHER.read_text(encoding="utf-8")

    assert 'PX4_DDS_CLIENT_KEY="${III_HIL_PX4_DDS_CLIENT_KEY:-2}"' in source
    assert '$0 == "param set UXRCE_DDS_KEY $((px4_instance+1))"' in source
    assert 'print "param set UXRCE_DDS_KEY " dds_client_key' in source


def test_split_host_hil_does_not_require_an_ssh_security_override() -> None:
    """The normal HIL topology intentionally keeps the physical PX4 online."""
    source = LAUNCHER.read_text(encoding="utf-8")

    assert "require_exclusive_px4_source" not in source
    assert "III_HIL_ALLOW_SITL_WITH_PHYSICAL_PX4" not in source


def test_split_host_hil_uses_a_dedicated_pi_agent_for_sitl() -> None:
    """All ROS remains on Pi while the two PX4 clients use separate agents."""
    source = LAUNCHER.read_text(encoding="utf-8")

    assert 'PX4_AGENT_ADDRESS_U32_OVERRIDE="${III_HIL_PX4_AGENT_ADDRESS_U32:-}"' in source
    assert "px4_agent_address_u32()" in source
    assert 'XRCE_PORT="${III_HIL_XRCE_PORT:-8890}"' in source
    assert "start_xrce_agent" not in source
    assert "FASTDDS_DEFAULT_PROFILES_FILE" not in source


def test_hil_viewer_status_recognizes_tmux_panes_and_prefers_live_duplicate(tmp_path: Path) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    function = launcher_function(source, "gazebo_gui_process_running", "run_simulation_launcher")
    proc_dir = tmp_path / "proc" / "4242"
    proc_dir.mkdir(parents=True)
    (proc_dir / "cmdline").write_bytes(
        b"/usr/bin/ruby\0/opt/ros/jazzy/opt/gz_tools_vendor/bin/gz\0sim\0-g\0"
    )
    script = r'''
set -u
SIM_SESSION=iii_hil_sim
PROC_ROOT="${TEST_PROC_ROOT}"
session_exists() { return 0; }
tmux_command() {
    local format="${*: -1}"
    if [[ "${format}" == $'#{pane_index}\t#{pane_dead}\t#{pane_title}\t#{pane_current_command}\t#{pane_pid}' ]]; then
        printf '%b' "${MOCK_PANES}"
    else
        # tmux leaves backslash-t literal when it appears in -F.
        printf '1\\t1\\tGazebo GUI\\tbash\\t4242\n2\\t0\\tGazebo GUI\\truby\\t4242\n'
    fi
}
'''
    for panes, expected in (
        ("1\t1\tGazebo GUI\tbash\t4242\n2\t0\tGazebo GUI\truby\t4242\n", "running"),
        ("1\t1\tGazebo GUI\tbash\t4242\n2\t0\tGazebo GUI\tbash\t4242\n", "starting"),
        ("1\t1\tGazebo GUI\tbash\t4242\n", "dead"),
        ("", "missing"),
    ):
        result = subprocess.run(
            ["bash", "-c", script + function + "\ngazebo_viewer_state\n"],
            env={**os.environ, "MOCK_PANES": panes, "TEST_PROC_ROOT": str(tmp_path / "proc")},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected
    (proc_dir / "cmdline").write_bytes(b"/usr/bin/sleep\0100\0")
    result = subprocess.run(
        ["bash", "-c", script + function + "\ngazebo_viewer_state\n"],
        env={**os.environ, "MOCK_PANES": "2\t0\tGazebo GUI\tsleep\t4242\n", "TEST_PROC_ROOT": str(tmp_path / "proc")},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "starting"


def test_hil_viewer_repair_passes_its_gazebo_partition_to_sim_launcher(tmp_path: Path) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    function = launcher_function(source, "run_simulation_launcher", "report_owner_conflict")
    sim_launcher = tmp_path / "sim-launcher"
    sim_launcher.write_text(
        '#!/bin/sh\nprintf "%s|%s|%s\\n" "$GZ_PARTITION" "$III_SIM_TOOLS_SESSION" "$*"\n',
        encoding="utf-8",
    )
    sim_launcher.chmod(0o755)
    script = (
        "set -u\n"
        "HIL_RENDERED=1\nSIM_SESSION=iii_hil_sim\nWORKSPACE_ROOT=/home/iii/ws\n"
        "PX4_INSTANCE=0\nPX4_BUILD_DIR=/tmp/build\nGZ_PARTITION=iii_hil_test_0\n"
        f"SIM_LAUNCHER={sim_launcher}\n"
    )
    result = subprocess.run(
        ["bash", "-c", script + function + "\nrun_simulation_launcher\n"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "iii_hil_test_0|iii_hil_sim|--no-attach --rendered"


def test_reused_owner_render_mode_update_preserves_identity(tmp_path: Path) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    function = launcher_function(source, "update_owner_render_mode", "owner_render_mode")
    owner = tmp_path / "owner.env"
    owner.write_text(
        "pid=42\nstart_ticks=77\nrendered=0\nstarted_utc=original\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\nHIL_OWNER_RECORD={owner}\nHIL_RENDERED=1\n"
         + function + "\nupdate_owner_render_mode\n"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert owner.read_text(encoding="utf-8") == (
        "pid=42\nstart_ticks=77\nrendered=1\nstarted_utc=original\n"
    )


def test_hil_lifecycle_enforces_one_live_canonical_px4_owner() -> None:
    """Start/status/stop must use live process, socket, and ownership evidence."""
    source = LAUNCHER.read_text(encoding="utf-8")

    assert "host_lifecycle_preflight()" in source
    assert "canonical_endpoint_pids()" in source
    assert "canonical_px4_records()" in source
    assert "proc_start_ticks()" in source
    assert "wait_for_single_px4_owner()" in source
    assert "terminate_managed_px4()" in source
    assert "flock -x" in source
    assert 'canonical_xrce_endpoint_ownership:' in source
    assert 'conflicting_px4_owners:' in source
    assert 'Canonical HIL stop failed verification' in source
    assert '"${PX4_BUILD_DIR}/bin/px4 (deleted)"' in source
    assert "host_container_owner_matches" in source
    assert 'tmux has-session -t "=${session}"' in source
    assert 'cmdline_sha256=' in source


def test_coordinated_restart_can_start_local_adapters_before_pi_graph() -> None:
    """Local adapters precede the Pi-owned SITL agent's FMU heartbeat."""
    source = LAUNCHER.read_text(encoding="utf-8")
    local_ready = launcher_function(source, "adapters_locally_ready", "adapters_ready")

    assert "adapters_locally_ready" in source
    assert "III_HIL_DEFER_PI_GRAPH_READINESS" not in source
    assert "/fmu/out/vehicle_status_v1" not in source
    assert "/drone_frame_broadcaster/is_alive" not in local_ready
    assert "tf2_echo drone cable_gripper" in local_ready
    assert "tf2_echo drone mmwave" in local_ready


def test_hil_adapter_readiness_requires_forwarded_camera_frames() -> None:
    """A discoverable but stalled camera relay must be restarted on start."""
    source = LAUNCHER.read_text(encoding="utf-8")

    assert "ros2 topic echo /sensor/cable_camera/image_raw" in source
    assert "--qos-reliability best_effort" in source
    assert 'session_exists "${ADAPTER_SESSION}" && ! adapters_locally_ready' in source
    readiness = launcher_function(source, "adapters_ready", "gazebo_owner")
    assert "/drone_frame_broadcaster/is_alive" not in readiness
    assert "tf2_echo drone cable_gripper" in readiness


@pytest.mark.parametrize(("fail_marker", "expected"), [("", 0), ("tf_gripper", 1)])
def test_local_adapter_readiness_requires_workstation_static_tf(
    tmp_path: Path, fail_marker: str, expected: int
) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    function = launcher_function(source, "adapters_locally_ready", "adapters_ready")
    script = r'''
set -u
WORKSPACE_ROOT=/tmp
ADAPTER_SESSION=mock
adapter_panes_healthy() { return 0; }
ros_environment() { printf '%s' 'mock-ros-environment'; }
session_user_command() {
    local command="$*" marker=unknown
    case "${command}" in
        *lifecycle*) marker=lifecycle ;;
        *"/clock"*) marker=clock ;;
        *"tf2_echo drone cable_gripper"*) marker=tf_gripper ;;
        *"tf2_echo drone mmwave"*) marker=tf_mmwave ;;
        *cable_camera*) marker=camera ;;
    esac
    printf '%s\n' "${marker}" >>"${MOCK_LOG}"
    [[ "${MOCK_FAIL_MARKER:-}" != "${marker}" ]]
}
'''
    log = tmp_path / "local-readiness.log"
    result = subprocess.run(
        ["bash", "-c", script + function + "\nadapters_locally_ready\n"],
        env=dict(os.environ, MOCK_LOG=str(log), MOCK_FAIL_MARKER=fail_marker),
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected, result.stderr
    markers = log.read_text(encoding="utf-8").splitlines()
    assert sorted(markers) == ["camera", "clock", "lifecycle", "tf_gripper", "tf_mmwave"]


def test_owned_px4_reuse_recovers_missing_adapters_before_success(tmp_path: Path) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    ensure = launcher_function(source, "ensure_adapters_ready", "gazebo_owner")
    start = launcher_function(source, "start", "stop")
    script = r'''
set -u
WORKSPACE_ROOT=/tmp
PI_ADDRESS=10.42.0.15
XRCE_PORT=8890
SIM_SESSION=sim
ADAPTER_SESSION=adapters
ADAPTER_READINESS_CONFIRMED=0
HIL_RENDERED=1
MOCK_LOG="$MOCK_LOG"
require_standard_link() { :; }
resolve_pi_address() { printf '10.42.0.15\n'; }
canonical_endpoint_pids() { printf '42\n'; }
canonical_px4_records() { printf '42\t77\t1\tpresent\tpx4\n'; }
owned_px4_record() { printf '%s\n' "$1"; }
sim_session_healthy() { return 0; }
gazebo_owner() { [[ "$1" == inspect ]]; }
run_simulation_launcher() { echo ui >>"$MOCK_LOG"; }
update_owner_render_mode() { echo owner-mode >>"$MOCK_LOG"; }
session_exists() {
    [[ "$1" == "$SIM_SESSION" ]] && return 0
    [[ "$1" == "$ADAPTER_SESSION" && -e "$MOCK_ADAPTER" ]]
}
tmux_command() { echo "$*" >>"$MOCK_LOG"; [[ "$*" == *kill-session* ]] && : >"$MOCK_KILLED"; }
start_adapters() { echo adapters-start >>"$MOCK_LOG"; : >"$MOCK_ADAPTER"; }
adapters_locally_ready() { return 1; }
adapters_ready() { echo adapters-ready >>"$MOCK_LOG"; return 0; }
print_status() { echo status >>"$MOCK_LOG"; return 0; }
sleep() { :; }
'''
    log = tmp_path / "reuse.log"
    adapter = tmp_path / "adapter"
    result = subprocess.run(
        ["bash", "-c", script + ensure + "\n" + start + "\nstart\n"],
        env=dict(os.environ, MOCK_LOG=str(log), MOCK_ADAPTER=str(adapter)),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "ui", "owner-mode", "adapters-start", "adapters-ready", "status"
    ]


def test_owned_px4_reuse_fails_when_adapter_readiness_never_completes(
    tmp_path: Path,
) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    ensure = launcher_function(source, "ensure_adapters_ready", "gazebo_owner")
    start = launcher_function(source, "start", "stop")
    script = r'''
set -u
WORKSPACE_ROOT=/tmp
PI_ADDRESS=10.42.0.15
XRCE_PORT=8890
SIM_SESSION=sim
ADAPTER_SESSION=adapters
ADAPTER_READINESS_CONFIRMED=0
HIL_RENDERED=1
require_standard_link() { :; }
resolve_pi_address() { printf '10.42.0.15\n'; }
canonical_endpoint_pids() { printf '42\n'; }
canonical_px4_records() { printf '42\t77\t1\tpresent\tpx4\n'; }
owned_px4_record() { printf '%s\n' "$1"; }
sim_session_healthy() { return 0; }
gazebo_owner() { [[ "$1" == inspect ]]; }
run_simulation_launcher() { :; }
update_owner_render_mode() { :; }
session_exists() { [[ "$1" == "$SIM_SESSION" || -e "$MOCK_ADAPTER" ]]; }
adapters_locally_ready() { return 1; }
tmux_command() { : >"$MOCK_KILLED"; rm -f "$MOCK_ADAPTER"; }
start_adapters() { : >"$MOCK_ADAPTER"; }
adapters_ready() { return 1; }
print_status() { return 1; }
sleep() { :; }
'''
    adapter = tmp_path / "adapter"
    adapter.touch()
    result = subprocess.run(
        ["bash", "-c", script + ensure + "\n" + start + "\nstart\n"],
        env=dict(
            os.environ,
            MOCK_ADAPTER=str(adapter),
            MOCK_KILLED=str(tmp_path / "killed"),
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert (tmp_path / "killed").exists()


def test_hil_adapter_readiness_fanout_reaps_all_local_probes(
    tmp_path: Path,
) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    function = launcher_function(source, "adapters_ready", "gazebo_owner")
    script = r'''
set -u
WORKSPACE_ROOT=/tmp
ADAPTER_SESSION=mock
ADAPTER_READINESS_CONFIRMED=0
adapter_panes_healthy() { return 0; }
ros_environment() { printf '%s' 'mock-ros-environment'; }
    session_user_command() {
        local command="$*" marker=unknown
        case "${command}" in
            *lifecycle*) marker=lifecycle ;;
            *"/clock"*) marker=clock ;;
            *"tf2_echo drone cable_gripper"*) marker=tf_gripper ;;
            *"tf2_echo drone mmwave"*) marker=tf_mmwave ;;
            *cable_camera*) marker=camera ;;
        esac
    printf 'start:%s\n' "${marker}" >>"${MOCK_LOG}"
    sleep 0.15
    printf 'done:%s\n' "${marker}" >>"${MOCK_LOG}"
    [[ "${MOCK_FAIL_MARKER:-}" != "${marker}" ]]
}
'''

    def run_probe(*, fail_marker: str = ""):
        log = tmp_path / "readiness-probes.log"
        env = dict(
            os.environ,
            MOCK_LOG=str(log),
            MOCK_FAIL_MARKER=fail_marker,
        )
        started = time.monotonic()
        result = subprocess.run(
            ["bash", "-c", script + function + "\nadapters_ready\n"],
            env=env,
            capture_output=True,
            text=True,
        )
        elapsed = time.monotonic() - started
        lines = log.read_text(encoding="utf-8").splitlines()
        log.unlink(missing_ok=True)
        return result, elapsed, lines

    success, elapsed, lines = run_probe()
    assert success.returncode == 0, success.stderr
    assert elapsed < 0.5
    assert sorted(line.removeprefix("done:") for line in lines if line.startswith("done:")) == [
        "camera", "clock", "lifecycle", "tf_gripper", "tf_mmwave"
    ]

    failed, _, failed_lines = run_probe(fail_marker="tf_gripper")
    assert failed.returncode == 1
    assert sorted(line.removeprefix("done:") for line in failed_lines if line.startswith("done:")) == [
        "camera", "clock", "lifecycle", "tf_gripper", "tf_mmwave"
    ]

def test_hil_workstation_disables_competing_world_to_drone_transform() -> None:
    """The Pi publishes dynamic PX4 TF; workstation HIL publishes only statics."""
    source = LAUNCHER.read_text(encoding="utf-8")
    simulation_tf_launch = (
        LAUNCHER.parents[2]
        / "src"
        / "III-Drone-Simulation"
        / "launch"
        / "tf_sim.launch.py"
    ).read_text(encoding="utf-8")

    assert "use_ground_truth_odometry:=true publish_world_to_drone:=false" in source
    assert "tf2_echo drone cable_gripper" in source
    assert 'executable="static_transform_publisher"' in simulation_tf_launch


@pytest.mark.parametrize(
    ("topics", "services", "service_exit", "expected"),
    [
        ("", "", "0", 0),
        ("/world/existing/clock", "", "0", 1),
        ("/world/existing/stats", "", "0", 1),
        ("", "/world/existing/scene/info", "0", 1),
        ("", "", "1", 1),
    ],
)
def test_hil_rejects_discovered_worlds_before_start(
    topics: str, services: str, service_exit: str, expected: int,
) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    function = "require_empty_gazebo_partition() {" + source.split(
        "require_empty_gazebo_partition() {", 1
    )[1].split("\nprint_status() {", 1)[0]
    script = '''
GZ_PARTITION=test_partition
session_user_command() {
    case "$*" in
        *"gz topic -l") printf '%s\\n' "$MOCK_TOPICS" ;;
        *"gz service -l") printf '%s\\n' "$MOCK_SERVICES"; return "$MOCK_SERVICE_EXIT" ;;
        *) return 99 ;;
    esac
}
'''
    result = subprocess.run(
        ["bash", "-c", script + function + "\nrequire_empty_gazebo_partition\n"],
        env=dict(os.environ, MOCK_TOPICS=topics, MOCK_SERVICES=services,
                 MOCK_SERVICE_EXIT=service_exit),
        capture_output=True, text=True,
    )
    assert result.returncode == expected, result.stderr


def test_deleted_px4_executable_remains_visible_as_a_stop_candidate(
    tmp_path: Path,
) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    function = launcher_function(source, "host_px4_records", "owner_record_value")
    proc = tmp_path / "proc"
    process = proc / "123"
    process.mkdir(parents=True)
    (process / "comm").write_text("px4\n", encoding="utf-8")
    (process / "cmdline").write_bytes(
        b"px4\0-s\0/tmp/iii-hil/px4-rcS-0\0-i\0" + b"0\0"
    )
    stat_fields = ["123", "(px4)", "S", *(["0"] * 18), "777"]
    (process / "stat").write_text(" ".join(stat_fields), encoding="utf-8")
    build_dir = tmp_path / "build"
    (process / "exe").symlink_to(f"{build_dir}/bin/px4 (deleted)")
    script = r'''
host_endpoint_pids() { printf '%s\n' 999; }
'''
    result = subprocess.run(
        ["bash", "-c", script + function + "\nhost_px4_records\n"],
        env=dict(
            os.environ,
            PROC_ROOT=str(proc),
            PX4_BUILD_DIR=str(build_dir),
            PX4_INSTANCE="0",
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    fields = result.stdout.rstrip().split("\t", 4)
    assert fields[:4] == ["123", "777", "0", "deleted"]
    assert "/tmp/iii-hil/px4-rcS-0" in fields[4]


def test_host_px4_records_accepts_recorded_container_executable_path(
    tmp_path: Path,
) -> None:
    """Host discovery must retain PX4 whose /proc/exe uses the container path."""
    source = LAUNCHER.read_text(encoding="utf-8")
    function = launcher_function(source, "host_px4_records", "owner_record_value")
    proc = tmp_path / "proc"
    process = proc / "321"
    process.mkdir(parents=True)
    (process / "comm").write_text("px4\n", encoding="utf-8")
    (process / "cmdline").write_bytes(
        b"px4\0-s\0/tmp/iii-hil/px4-rcS-0\0-i\0" + b"0\0"
    )
    stat_fields = ["321", "(px4)", "S", *( ["0"] * 18), "888"]
    (process / "stat").write_text(" ".join(stat_fields), encoding="utf-8")
    host_executable = tmp_path / "host-build" / "bin" / "px4"
    container_executable = "/home/iii/ws/PX4-Autopilot/build/px4_sitl_default/bin/px4"
    (process / "exe").symlink_to(container_executable)
    script = r'''
host_endpoint_pids() { printf '%s\n' 321; }
owner_record_value() {
    [[ "$1" == executable ]] && printf '%s\n' "$MOCK_OWNER_EXECUTABLE"
}
'''
    result = subprocess.run(
        ["bash", "-c", script + function + "\nhost_px4_records\n"],
        env=dict(
            os.environ,
            PROC_ROOT=str(proc),
            PX4_BUILD_DIR=str(host_executable.parent.parent),
            PX4_INSTANCE="0",
            MOCK_OWNER_EXECUTABLE=container_executable,
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    fields = result.stdout.rstrip().split("\t", 4)
    assert fields[:4] == ["321", "888", "1", "present"]
    assert "/tmp/iii-hil/px4-rcS-0" in fields[4]


def test_px4_record_classifiers_reuse_the_supplied_endpoint_snapshot(
    tmp_path: Path,
) -> None:
    """Ownership classification must not rediscover sockets after a snapshot."""
    source = LAUNCHER.read_text(encoding="utf-8")
    host = launcher_function(source, "host_px4_records", "owner_record_value")
    canonical = launcher_function(source, "canonical_px4_records", "managed_px4_pid")
    script = r'''
calls="$MOCK_CALLS"
host_endpoint_pids() { printf 'host-discovery\n' >>"$calls"; printf '999\n'; }
canonical_endpoint_pids() { printf 'canonical-discovery\n' >>"$calls"; printf '999\n'; }
owner_record_value() { return 1; }
proc_cmdline() { return 1; }
proc_start_ticks() { printf '1\n'; }
mkdir -p "$PROC_ROOT"
host_px4_records 999
canonical_px4_records 999
[[ ! -s "$calls" ]]
host_px4_records >/dev/null
canonical_px4_records >/dev/null
[[ "$(wc -l <"$calls")" == 2 ]]
'''
    result = subprocess.run(
        ["bash", "-c", host + "\n" + canonical + "\n" + script],
        env=dict(
            os.environ,
            PROC_ROOT=str(tmp_path / "proc"),
            MOCK_CALLS=str(tmp_path / "calls"),
            PX4_BUILD_DIR=str(tmp_path / "build"),
            PX4_INSTANCE="0",
            PX4_STARTUP_SCRIPT="/tmp/rcS",
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_host_preflight_allows_owned_deleted_px4_stop_and_preserves_foreign_endpoint(
) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    functions = launcher_function(
        source, "host_report_unrelated_endpoint_occupants", "host_lifecycle_preflight"
    )
    functions += "\n" + launcher_function(
        source, "host_lifecycle_preflight", "session_user_command"
    ).split("\nhost_lifecycle_preflight || exit $?", 1)[0]
    functions += "\n" + launcher_function(source, "select_workstation_route", "host_endpoint_pids")
    common = r'''
PI_ADDRESS=10.42.0.15
XRCE_PORT=8890
III_HIL_NO_CONTAINER_REEXEC=
host_resolve_pi_address() { printf '%s\n' 10.42.0.15; }
host_px4_records() { printf '123\t777\t0\tdeleted\tpx4 -s /tmp/px4-rcS-0 -i 0 etc\n'; }
host_endpoint_pids() { printf '%s\n' 999; }
host_owner_record_matches() {
    [[ "$2" == "1" ]] || return 1
    printf '%s\n' 123
}
host_report_conflict() { printf '%s\n' conflict >&2; }
'''
    stop = subprocess.run(
        ["bash", "-c", common + functions + "\nselect_workstation_route() { :; }\nACTION=stop\nhost_lifecycle_preflight\n"],
        capture_output=True,
        text=True,
    )
    assert stop.returncode == 0, stop.stderr
    assert "Preserving unrelated canonical endpoint occupant PID=999" in stop.stderr

    start = subprocess.run(
        ["bash", "-c", common + functions + "\nselect_workstation_route() { :; }\nACTION=start\nhost_lifecycle_preflight\n"],
        capture_output=True,
        text=True,
    )
    assert start.returncode == 1
    assert "conflict" in start.stderr


@pytest.mark.parametrize(
    "records",
    [
        "123\\t777\\t1\\tpresent\\tpx4 canonical\\n",
        "123\\t777\\t1\\tpresent\\tpx4 first\\n124\\t778\\t1\\tpresent\\tpx4 second\\n",
        "",
    ],
    ids=["unowned", "ambiguous", "unrecognized-endpoint-occupant"],
)
def test_locked_stop_owner_check_rejects_unowned_or_ambiguous_px4(records: str, tmp_path: Path):
    source = LAUNCHER.read_text(encoding="utf-8")
    function = launcher_function(source, "read_stop_owner", "stop")
    script = r'''
set -euo pipefail
WORKSPACE_ROOT=/tmp/workspace
SIM_SESSION=iii_hil_sim
ADAPTER_SESSION=iii_hil_adapters
HIL_OWNER_RECORD=/tmp/nonexistent-owner-record
PI_ADDRESS=
STOP_RECORDS=
STOP_ENDPOINT_PIDS=
STOP_MANAGED_PID=
STOP_HAVE_OWNED_INSTANCE=0
resolve_pi_address() { printf '%s\n' 10.42.0.15; }
canonical_endpoint_pids() { printf '%s\n' 123; }
canonical_px4_records() { printf '%b' "$MOCK_RECORDS"; }
owned_px4_record() { return 1; }
owned_session_without_process() { return 1; }
session_exists() { return 1; }
report_owner_conflict() { printf 'owner conflict\n' >&2; }
report_unrelated_endpoint_occupants() { :; }
'''
    calls = tmp_path / "mutations"
    mutation_functions = (
        'kill_session() { printf kill >> "$MUTATIONS_FILE"; }\n'
        'terminate_managed_px4() { printf terminate >> "$MUTATIONS_FILE"; }\n'
        'gazebo_owner() { printf gazebo >> "$MUTATIONS_FILE"; }\n'
    )
    result = subprocess.run(
        ["bash", "-c", script + mutation_functions + function + "\nread_stop_owner\n"],
        env={**os.environ, "MOCK_RECORDS": records, "MUTATIONS_FILE": str(calls)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "owner conflict" in result.stderr
    assert not calls.exists()


@pytest.mark.parametrize("active_kind", ["none", "session", "endpoint", "px4"])
def test_stop_retires_only_an_inactive_record_from_a_previous_container(
    active_kind: str, tmp_path: Path,
) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    function = launcher_function(source, "read_stop_owner", "stop")
    owner = tmp_path / "owner.env"
    peer = tmp_path / "peer"
    owner.write_text("pi_address=10.42.0.15\ncontainer_id=old-container\n", encoding="utf-8")
    peer.write_text("10.42.0.15\n", encoding="utf-8")
    script = r'''
set -euo pipefail
SIM_SESSION=iii_hil_sim
ADAPTER_SESSION=iii_hil_adapters
PI_ADDRESS=
STOP_RECORDS=
STOP_ENDPOINT_PIDS=
STOP_MANAGED_PID=
STOP_HAVE_OWNED_INSTANCE=0
resolve_pi_address() { printf '%s\n' 10.42.0.15; }
canonical_endpoint_pids() {
    [[ "$ACTIVE_KIND" != endpoint ]] || printf '%s\n' 123
}
canonical_px4_records() {
    [[ "$ACTIVE_KIND" != px4 ]] || printf '123\t777\t0\tpresent\tpx4\n'
}
owned_px4_record() { return 1; }
owned_session_without_process() { return 1; }
session_exists() { [[ "$ACTIVE_KIND" == session && "$1" == "$SIM_SESSION" ]]; }
report_owner_conflict() { printf 'owner conflict\n' >&2; }
report_unrelated_endpoint_occupants() { :; }
''' + function + "\nread_stop_owner\n"
    result = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "ACTIVE_KIND": active_kind,
             "HIL_OWNER_RECORD": str(owner), "HIL_PEER_ADDRESS_FILE": str(peer)},
        capture_output=True, text=True, check=False,
    )
    if active_kind == "none":
        assert result.returncode == 0, result.stderr
        assert "Retired inactive workstation HIL owner record" in result.stdout
        assert not owner.exists()
        assert not peer.exists()
    else:
        assert result.returncode == 1
        assert owner.exists()
        assert peer.exists()


def test_deleted_owner_requires_matching_recorded_container_and_session(
    tmp_path: Path,
) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    functions = launcher_function(source, "owner_record_value", "host_report_conflict")
    cmdline = "px4 -s /tmp/iii-hil/px4-rcS-0 -i 0 etc"
    cmdline_hash = subprocess.run(
        ["sha256sum"], input=cmdline, text=True, capture_output=True, check=True
    ).stdout.split()[0]
    owner = tmp_path / "owner.env"
    owner.write_text(
        "\n".join(
            [
                "pid=42",
                "start_ticks=777",
                "executable=/home/iii/ws/PX4-Autopilot/build/px4_sitl_default/bin/px4",
                f"cmdline_sha256={cmdline_hash}",
                "container_id=container123",
                "session=iii_hil_sim",
                "pi_address=10.42.0.15",
                "xrce_port=8890",
                "gz_partition=partition123",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    script = r'''
docker() {
    case "$1" in
        ps) printf '%s\n' container123 ;;
        exec)
            cat >/dev/null
            [[ "$*" == *"container123"* && "$*" == *"iii_hil_sim"* ]]
            ;;
        *) return 99 ;;
    esac
}
records="42"$'\t'"777"$'\t'"0"$'\t'"deleted"$'\t'"$MOCK_CMDLINE"
host_owner_record_matches "$records" 1
if host_owner_record_matches "$records" 0 >/dev/null; then
    exit 88
fi
'''
    result = subprocess.run(
        ["bash", "-c", functions + script],
        env=dict(
            os.environ,
            MOCK_CMDLINE=cmdline,
            HIL_OWNER_RECORD=str(owner),
            SIM_SESSION="iii_hil_sim",
            PI_ADDRESS="10.42.0.15",
            XRCE_PORT="8890",
            GZ_PARTITION="partition123",
            SCRIPT_WORKSPACE_ROOT="/workspace",
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "42"


def test_inner_stop_targets_only_recorded_deleted_px4_and_leaves_foreign_endpoint(
    tmp_path: Path,
) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    preflight = launcher_function(source, "host_lifecycle_preflight", "session_user_command").split(
        "\nhost_lifecycle_preflight || exit $?", 1
    )[0]
    function = launcher_function(source, "read_stop_owner", "stop")
    function += "\n" + "stop() {" + source.split("stop() {", 1)[1].split(
        '\ncase "${ACTION}" in', 1
    )[0]
    operation_log = tmp_path / "operations.log"
    sim_stopped = tmp_path / "sim.stopped"
    adapter_stopped = tmp_path / "adapter.stopped"
    px4_stopped = tmp_path / "px4.stopped"
    owner = tmp_path / "owner.env"
    peer = tmp_path / "peer"
    owner.write_text("owned\n", encoding="utf-8")
    peer.write_text("10.42.0.15\n", encoding="utf-8")
    script = r'''
set -euo pipefail
ACTION=stop
PI_ADDRESS=
PI_ENDPOINT=iii.local
WORKSTATION_ADDRESS=192.168.1.20
WORKSTATION_INTERFACE=enp3s0
XRCE_PORT=8890
explicit_host=0
III_HIL_NO_CONTAINER_REEXEC=1
host_resolve_pi_address() { printf '%s\n' 10.42.0.15; }
select_workstation_route() { echo route-unavailable >&2; return 1; }
resolve_pi_address() { printf '%s\n' 10.42.0.15; }
canonical_px4_records() {
    [[ -f "$MOCK_PX4_STOPPED" ]] || printf '42\t777\t0\tdeleted\tpx4 -s /tmp/px4-rcS-0 -i 0 etc\n'
}
canonical_endpoint_pids() { printf '%s\n' 999; }
owned_px4_record() { printf '%s\n' "$1"; }
owned_session_without_process() { return 1; }
session_exists() {
    case "$1" in
        "$ADAPTER_SESSION") [[ ! -f "$MOCK_ADAPTER_STOPPED" ]] ;;
        "$SIM_SESSION") [[ ! -f "$MOCK_SIM_STOPPED" ]] ;;
        *) return 1 ;;
    esac
}
tmux_command() {
    printf 'tmux %s\n' "$*" >>"$MOCK_OPERATION_LOG"
    [[ "$*" == *"$ADAPTER_SESSION"* ]] && : >"$MOCK_ADAPTER_STOPPED"
}
terminate_managed_px4() {
    printf 'terminate %s\n' "$1" >>"$MOCK_OPERATION_LOG"
    : >"$MOCK_PX4_STOPPED"
}
env() {
    printf 'simulation launcher stop\n' >>"$MOCK_OPERATION_LOG"
    : >"$MOCK_SIM_STOPPED"
}
gazebo_owner() { printf 'gazebo %s\n' "$*" >>"$MOCK_OPERATION_LOG"; }
report_owner_conflict() { printf 'conflict\n' >>"$MOCK_OPERATION_LOG"; }
print_status() { return 1; }
report_unrelated_endpoint_occupants() {
    local pid
    while IFS= read -r pid; do
        [[ -n "$pid" && "$pid" != "${2:-}" ]] || continue
        printf 'Preserving unrelated canonical endpoint occupant PID=%s.\n' "$pid" >&2
    done <<<"$1"
}
'''
    result = subprocess.run(
        ["bash", "-c", script + preflight + function + "\nhost_lifecycle_preflight\nstop\n"],
        env=dict(
            os.environ,
            MOCK_OPERATION_LOG=str(operation_log),
            MOCK_SIM_STOPPED=str(sim_stopped),
            MOCK_ADAPTER_STOPPED=str(adapter_stopped),
            MOCK_PX4_STOPPED=str(px4_stopped),
            HIL_OWNER_RECORD=str(owner),
            HIL_PEER_ADDRESS_FILE=str(peer),
            ADAPTER_SESSION="iii_hil_adapters",
            SIM_SESSION="iii_hil_sim",
            WORKSPACE_ROOT="/home/iii/ws",
            PX4_INSTANCE="0",
            PX4_BUILD_DIR="/home/iii/ws/PX4-Autopilot/build/px4_sitl_default",
            SIM_LAUNCHER="/home/iii/ws/tools/simulation/launch_simulation_tools.sh",
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "route-unavailable" in result.stderr
    operations = operation_log.read_text(encoding="utf-8")
    assert "terminate 42" in operations
    assert "tmux kill-session -t iii_hil_adapters" in operations
    assert "simulation launcher stop" in operations
    assert "gazebo stop" in operations
    assert "terminate 999" not in operations
    assert "Preserving unrelated canonical endpoint occupant PID=999" in result.stderr
    assert not owner.exists()
    assert not peer.exists()


def test_owned_px4_termination_signals_only_the_verified_pid() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    function = launcher_function(source, "terminate_managed_px4", "acquire_lifecycle_lock")
    script = r'''
alive=1
proc_is_alive() { [[ "$alive" == "1" ]]; }
kill() {
    printf '%s\n' "$*"
    alive=0
}
III_HIL_STOP_TIMEOUT_SEC=1
terminate_managed_px4 42
'''
    result = subprocess.run(
        ["bash", "-c", function + script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "-TERM 42" in result.stdout
    assert "-- -" not in result.stdout


@pytest.mark.parametrize(
    ("route", "expected_source", "expected_interface"),
    [
        ("192.168.1.251 dev enp3s0 src 192.168.1.20 uid 1000", "192.168.1.20", "enp3s0"),
        ("10.42.0.15 dev enp4s0 src 10.42.0.1 uid 1000", "10.42.0.1", "enp4s0"),
    ],
)
def test_workstation_source_is_selected_from_route_to_resolved_peer(
    tmp_path: Path, route: str, expected_source: str, expected_interface: str
) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    function = launcher_function(source, "select_workstation_route", "host_endpoint_pids")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ip = fake_bin / "ip"
    fake_ip.write_text(f"#!/bin/sh\nprintf '%s\\n' '{route}'\n", encoding="utf-8")
    fake_ip.chmod(0o755)
    script = r'''
set -euo pipefail
valid_ipv4() {
    python3 - "$1" <<'PY'
import ipaddress
import sys
try:
    ipaddress.IPv4Address(sys.argv[1])
except ipaddress.AddressValueError:
    raise SystemExit(1)
PY
}
''' + function + r'''
select_workstation_route 192.168.1.251
printf '%s|%s|%s|%s' "$WORKSTATION_ADDRESS" "$WORKSTATION_INTERFACE" "$III_HIL_WORKSTATION_ADDRESS" "$III_HIL_WORKSTATION_INTERFACE"
'''
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "III_HIL_WORKSTATION_ADDRESS": "",
            "III_HIL_WORKSTATION_INTERFACE": "",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{expected_source}|{expected_interface}|{expected_source}|{expected_interface}"


def test_workstation_source_override_must_match_selected_route(tmp_path: Path) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    function = launcher_function(source, "select_workstation_route", "host_endpoint_pids")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ip = fake_bin / "ip"
    fake_ip.write_text(
        "#!/bin/sh\nprintf '%s\\n' '192.168.1.251 dev enp3s0 src 192.168.1.20 uid 1000'\n",
        encoding="utf-8",
    )
    fake_ip.chmod(0o755)
    script = r'''
set -euo pipefail
valid_ipv4() {
    python3 - "$1" <<'PY'
import ipaddress
import sys
try:
    ipaddress.IPv4Address(sys.argv[1])
except ipaddress.AddressValueError:
    raise SystemExit(1)
PY
}
''' + function + r'''
select_workstation_route 192.168.1.251
'''
    result = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin", "III_HIL_WORKSTATION_ADDRESS": "10.42.0.1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "does not match route to Pi 192.168.1.251" in result.stderr


def test_clean_status_derives_route_source_without_changing_selected_peer(tmp_path: Path) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    preflight = launcher_function(source, "host_lifecycle_preflight", "session_user_command").split(
        "\nhost_lifecycle_preflight || exit $?", 1
    )[0]
    route = launcher_function(source, "select_workstation_route", "host_endpoint_pids")
    valid_ipv4 = launcher_function(source, "valid_ipv4", "resolve_reachable_pi_address")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ip = fake_bin / "ip"
    fake_ip.write_text(
        "#!/bin/sh\nprintf '%s\\n' '192.168.1.251 dev enp3s0 src 192.168.1.20 uid 1000'\n",
        encoding="utf-8",
    )
    fake_ip.chmod(0o755)
    script = r'''
set -euo pipefail
ACTION=status
PI_ADDRESS=
PI_ENDPOINT=iii.local
WORKSTATION_ADDRESS=
WORKSTATION_INTERFACE=
XRCE_PORT=8890
explicit_host=0
III_HIL_NO_CONTAINER_REEXEC=1
valid_host_target() { return 0; }
host_resolve_pi_address() { printf '%s\n' 192.168.1.251; }
owner_record_value() { return 1; }
session_pi_address() { return 1; }
''' + valid_ipv4 + route + preflight + r'''
host_lifecycle_preflight
printf '%s|%s|%s|%s' "$PI_ADDRESS" "$WORKSTATION_ADDRESS" "$III_HIL_WORKSTATION_ADDRESS" "$III_HIL_WORKSTATION_INTERFACE"
'''
    result = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "192.168.1.251|192.168.1.20|192.168.1.20|enp3s0"
    assert 'echo "hil_pi_peer: ${PI_ADDRESS:-unknown}"' in source
    assert 'echo "hil_workstation_source: ${WORKSTATION_ADDRESS:-unavailable}"' in source


def test_route_loss_does_not_block_stop_of_the_exact_recorded_owner() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    preflight = launcher_function(source, "host_lifecycle_preflight", "session_user_command").split(
        "\nhost_lifecycle_preflight || exit $?", 1
    )[0]
    script = r'''
set -euo pipefail
ACTION=stop
PI_ADDRESS=
PI_ENDPOINT=iii.local
WORKSTATION_ADDRESS=192.168.1.20
WORKSTATION_INTERFACE=enp3s0
XRCE_PORT=8890
explicit_host=0
host_resolve_pi_address() { printf '%s\n' 192.168.1.251; }
select_workstation_route() { echo route-unavailable >&2; return 1; }
host_endpoint_pids() { printf '%s\n' 42; }
host_px4_records() { printf '42\t77\t1\tpresent\tpx4\n'; }
host_owner_record_matches() { [[ "$2" == 1 ]] && printf '%s\n' 42; }
host_report_unrelated_endpoint_occupants() { :; }
''' + preflight + r'''
host_lifecycle_preflight
printf '%s|%s|%s' "$PI_ADDRESS" "$WORKSTATION_ADDRESS" "$WORKSTATION_INTERFACE"
'''
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "192.168.1.251||"
    assert "route-unavailable" in result.stderr


def test_hostname_resolves_to_one_concrete_peer_without_special_ip_mapping(
    tmp_path: Path,
) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    host_resolve = launcher_function(source, "host_resolve_pi_address", "host_pi_ssh_reachable")
    resolver = launcher_function(source, "resolve_reachable_pi_address", "select_workstation_route")
    valid_ipv4 = launcher_function(source, "valid_ipv4", "resolve_reachable_pi_address")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_getent = fake_bin / "getent"
    fake_getent.write_text(
        "#!/bin/sh\nprintf '%s\\n' '10.42.0.15 STREAM iii.local' '192.168.1.251 STREAM iii.local' '10.42.0.15 DGRAM iii.local'\necho call >>\"$RESOLVE_LOG\"\n",
        encoding="utf-8",
    )
    fake_getent.chmod(0o755)
    script = r'''
set -euo pipefail
''' + valid_ipv4 + r'''
PI_ADDRESS=
PI_ENDPOINT=iii.local
ACTION=start
session_pi_address() { return 1; }
valid_host_target() { return 0; }
''' + host_resolve + resolver + r'''
select_workstation_route() { echo "route:$1" >>"$RESOLVE_LOG"; return 0; }
host_pi_ssh_reachable() {
    echo "probe:$1" >>"$RESOLVE_LOG"
    [[ "$1" == 192.168.1.251 ]]
}
resolved="$(host_resolve_pi_address)"
printf '%s' "$resolved"
'''
    log = tmp_path / "resolve-calls"
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "RESOLVE_LOG": str(log),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "192.168.1.251"
    assert log.read_text(encoding="utf-8").splitlines() == [
        "call",
        "route:10.42.0.15",
        "probe:10.42.0.15",
        "route:192.168.1.251",
        "probe:192.168.1.251",
    ]


def test_explicit_host_status_stop_ignores_recorded_peer_until_checked(tmp_path: Path) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    function = launcher_function(source, "host_resolve_pi_address", "select_workstation_route")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_getent = fake_bin / "getent"
    fake_getent.write_text(
        "#!/bin/sh\nprintf '%s\\n' '192.168.1.251 STREAM pi-x.local'\n",
        encoding="utf-8",
    )
    fake_getent.chmod(0o755)
    script = r'''
set -euo pipefail
PI_ADDRESS=
PI_ENDPOINT=pi-x.local
ACTION=stop
explicit_host=1
session_pi_address() { printf '%s\n' 10.42.0.15; }
valid_host_target() { return 0; }
''' + function + r'''
host_resolve_pi_address
'''
    result = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "does not resolve to the recorded owner peer 10.42.0.15" in result.stderr


def test_explicit_host_cannot_status_or_stop_a_different_recorded_peer() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    preflight = launcher_function(source, "host_lifecycle_preflight", "session_user_command").split(
        "\nhost_lifecycle_preflight || exit $?", 1
    )[0]
    script = r'''
set -euo pipefail
ACTION=stop
PI_ADDRESS=192.168.1.251
explicit_host=1
XRCE_PORT=8890
III_HIL_NO_CONTAINER_REEXEC=
host_resolve_pi_address() { printf '%s\n' "$PI_ADDRESS"; }
owner_record_value() { [[ "$1" == pi_address ]] && printf '%s\n' 10.42.0.15; }
''' + preflight + r'''
host_lifecycle_preflight
'''
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert "does not match the recorded owner peer 10.42.0.15" in result.stderr


def test_explicit_host_cannot_status_or_stop_a_different_pinned_session_peer() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    preflight = launcher_function(source, "host_lifecycle_preflight", "session_user_command").split(
        "\nhost_lifecycle_preflight || exit $?", 1
    )[0]
    script = r'''
set -euo pipefail
ACTION=status
PI_ADDRESS=192.168.1.251
explicit_host=1
XRCE_PORT=8890
III_HIL_NO_CONTAINER_REEXEC=1
host_resolve_pi_address() { printf '%s\n' "$PI_ADDRESS"; }
owner_record_value() { return 1; }
session_pi_address() { printf '%s\n' 10.42.0.15; }
'''
    script += preflight + "\nhost_lifecycle_preflight\n"
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 1
    assert "does not match the recorded owner peer 10.42.0.15" in result.stderr


def test_explicit_unresolvable_host_refuses_stop() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    preflight = launcher_function(source, "host_lifecycle_preflight", "session_user_command").split(
        "\nhost_lifecycle_preflight || exit $?", 1
    )[0]
    script = r'''
set -euo pipefail
ACTION=stop
PI_ENDPOINT=unknown-pi.local
PI_ADDRESS=
explicit_host=1
XRCE_PORT=8890
host_resolve_pi_address() { return 1; }
owner_record_value() { return 1; }
''' + preflight + r'''
host_lifecycle_preflight
'''
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert "Unable to resolve selected HIL Pi host unknown-pi.local; refusing stop" in result.stderr


def test_conflicting_owner_and_session_peers_cannot_be_swallowed_by_preflight() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    resolver = launcher_function(source, "host_resolve_pi_address", "host_pi_ssh_reachable")
    preflight = launcher_function(source, "host_lifecycle_preflight", "session_user_command").split(
        "\nhost_lifecycle_preflight || exit $?", 1
    )[0]
    script = r'''
set -euo pipefail
ACTION=stop
PI_ENDPOINT=iii.local
PI_ADDRESS=
explicit_host=0
owner_record_value() { [[ "$1" == pi_address ]] && printf '%s\n' 10.42.0.15; }
session_pi_address() { printf '%s\n' 192.168.1.251; }
''' + resolver + preflight + r'''
host_lifecycle_preflight
'''
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "conflicts with the pinned session peer" in result.stderr


def test_explicit_hostname_status_uses_pinned_peer_when_dns_lists_stale_ip_first(tmp_path: Path) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    resolver = launcher_function(source, "host_resolve_pi_address", "host_pi_ssh_reachable")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    getent = fake_bin / "getent"
    getent.write_text(
        "#!/bin/sh\nprintf '%s\\n' '10.42.0.15 STREAM iii.local' '192.168.1.251 STREAM iii.local'\n",
        encoding="utf-8",
    )
    getent.chmod(0o755)
    script = r'''
set -euo pipefail
ACTION=status
PI_ENDPOINT=iii.local
PI_ADDRESS=
explicit_host=1
owner_record_value() { [[ "$1" == pi_address ]] && printf '%s\n' 192.168.1.251; }
session_pi_address() { printf '%s\n' 192.168.1.251; }
valid_host_target() { return 0; }
''' + resolver + r'''
host_resolve_pi_address
'''
    result = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "192.168.1.251\n"


def test_unowned_status_selects_reachable_peer_instead_of_first_dns_address() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    resolver = launcher_function(source, "host_resolve_pi_address", "host_pi_ssh_reachable")
    script = r'''
set -euo pipefail
ACTION=status
PI_ENDPOINT=iii.local
PI_ADDRESS=
explicit_host=0
owner_record_value() { return 1; }
session_pi_address() { return 1; }
valid_host_target() { return 0; }
resolve_reachable_pi_address() { [[ "$1" == iii.local ]] && printf '%s\n' 192.168.1.251; }
''' + resolver + r'''
host_resolve_pi_address
'''
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "192.168.1.251\n"
