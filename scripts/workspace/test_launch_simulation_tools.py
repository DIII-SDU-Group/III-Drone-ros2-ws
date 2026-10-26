"""Behavioral tests for owned Gazebo viewer lifecycle."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "simulation" / "launch_simulation_tools.sh"


FAKE_TMUX = r"""#!/usr/bin/env python3
import os
from pathlib import Path
import sys

state_path = Path(os.environ["FAKE_TMUX_STATE"])
log_path = Path(os.environ["FAKE_TMUX_LOG"])
state = dict(
    line.split("=", 1) for line in state_path.read_text().splitlines() if "=" in line
) if state_path.exists() else {"session": "0", "gui_panes": ""}

def save():
    state_path.write_text("".join(f"{key}={value}\n" for key, value in state.items()))

args = sys.argv[1:]
log_path.write_text(log_path.read_text() + " ".join(args) + "\n" if log_path.exists() else " ".join(args) + "\n")
command = args[0] if args else ""
if command == "has-session":
    raise SystemExit(0 if state["session"] == "1" else 1)
if command == "new-session":
    state["session"] = "1"
    save()
elif command == "kill-session":
    state["session"] = "0"
    state["gui_panes"] = ""
    save()
elif command == "split-window":
    panes = [pane for pane in state.get("gui_panes", "").split(",") if pane]
    if panes and os.environ.get("FAKE_TMUX_NO_SPACE_WITH_GUI") == "1":
        print("no space for new pane", file=sys.stderr)
        raise SystemExit(1)
    next_index = max([int(pane.split(":", 1)[0]) for pane in panes] + [0]) + 1
    if os.environ.get("FAKE_TMUX_SPLIT_DEAD") == "1":
        panes.append(f"{next_index}:1:bash")
    else:
        panes.append(f"{next_index}:0:gz")
    state["gui_panes"] = ",".join(panes)
    save()
    if "-P" in args:
        print(next_index)
elif command == "kill-pane":
    target = args[args.index("-t") + 1]
    pane_index = target.rsplit(".", 1)[1]
    state["gui_panes"] = ",".join(
        pane for pane in state.get("gui_panes", "").split(",")
        if pane and pane.split(":", 1)[0] != pane_index
    )
    save()
elif command == "respawn-pane":
    target = args[args.index("-t") + 1]
    pane_index = target.rsplit(".", 1)[1]
    panes = []
    for pane in state.get("gui_panes", "").split(","):
        if not pane:
            continue
        if pane.split(":", 1)[0] == pane_index:
            panes.append(f"{pane_index}:1:bash" if os.environ.get("FAKE_TMUX_SPLIT_DEAD") == "1" else f"{pane_index}:0:gz")
        else:
            panes.append(pane)
    state["gui_panes"] = ",".join(panes)
    save()
elif command == "list-panes":
    fmt = args[args.index("-F") + 1] if "-F" in args else ""
    if fmt == "#{pane_pid}":
        print("999999")
    elif "#{pane_index}" in fmt and "#{pane_title}" in fmt:
        # tmux prints backslash-t bytes literally. It separates fields with
        # tabs only when the format argument contains actual tab bytes.
        transition_after = int(state.get("transition_after", "0"))
        if transition_after:
            reads = int(state.get("pane_reads", "0")) + 1
            state["pane_reads"] = str(reads)
            if reads >= transition_after:
                panes = []
                for pane in state.get("gui_panes", "").split(","):
                    if pane:
                        pane_index, pane_dead, *_ = pane.split(":", 2)
                        panes.append(f"{pane_index}:{pane_dead}:gz")
                state["gui_panes"] = ",".join(panes)
                state.pop("transition_after", None)
            save()
        separator = "\\t" if "\\t" in fmt else "\t"
        print(separator.join(("0", "0", "PX4 / Gazebo", "px4", "999999")))
        for pane in state.get("gui_panes", "").split(","):
            if pane:
                pane_index, pane_dead, *pane_command = pane.split(":", 2)
                print(separator.join((pane_index, pane_dead, "Gazebo GUI", pane_command[0] if pane_command else "bash", "4242")))
    else:
        print("pane=0 title=PX4 / Gazebo active=1 dead=0 exit= command=bash")
        for pane in state.get("gui_panes", "").split(","):
            if pane:
                pane_index, pane_dead, *pane_command = pane.split(":", 2)
                command = pane_command[0] if pane_command else "bash"
                print(f"pane={pane_index} title=Gazebo GUI active=0 dead={pane_dead} exit= command={command}")
raise SystemExit(0)
"""


def run_launcher(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args], cwd=ROOT, env=env, text=True,
        capture_output=True, check=False, timeout=10,
    )


def test_rendered_start_repairs_only_the_owned_gazebo_viewer() -> None:
    real_cache = ROOT / "PX4-Autopilot" / "build" / "px4_sitl_default"
    cache_before = (
        real_cache.exists(),
        real_cache.stat().st_ino if real_cache.exists() else None,
        real_cache.stat().st_mtime_ns if real_cache.exists() else None,
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        tmux = bin_dir / "tmux"
        tmux.write_text(FAKE_TMUX, encoding="utf-8")
        tmux.chmod(0o755)
        state = root / "tmux-state"
        log = root / "tmux.log"
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_TMUX_STATE": str(state),
            "FAKE_TMUX_LOG": str(log),
            "III_SIM_TOOLS_SESSION": "owned-hil",
            "III_SIM_TOOLS_WORKSPACE_ROOT": str(ROOT),
            "III_SIM_TOOLS_PX4_ROOT": str(root / "PX4-Autopilot"),
            "III_SIM_TOOLS_PX4_BUILD_DIR": str(root / "PX4-Autopilot" / "build" / "px4_sitl_default"),
            "III_SIM_TOOLS_PX4_COMMAND": "true",
            "III_SIM_TOOLS_GZ_GUI_COMMAND": "true",
            "III_SIM_TOOLS_GZ_IP": "127.0.0.1",
            "III_SIM_TOOLS_GZ_GUI_READY_TIMEOUT_SECONDS": "1",
            "III_SIM_TOOLS_GZ_GUI_READY_POLL_INTERVAL_SECONDS": "0.01",
            "GZ_PARTITION": "iii_hil_fb50fb32be99_0",
        }
        proc_dir = root / "proc" / "4242"
        proc_dir.mkdir(parents=True)
        (proc_dir / "cmdline").write_bytes(
            b"/usr/bin/ruby\0/opt/ros/jazzy/opt/gz_tools_vendor/bin/gz\0sim\0-g\0"
        )
        env["III_SIM_TOOLS_GZ_GUI_PROC_ROOT"] = str(root / "proc")

        headless = run_launcher(env, "--no-attach", "--headless")
        assert headless.returncode == 0, (
            f"{headless.stderr}\n{log.read_text(encoding='utf-8') if log.exists() else ''}"
        )
        assert "gui_panes=" in state.read_text(encoding="utf-8")

        # Viewer-only repair must never remove a live PX4 build cache, even
        # when its old Ninja file refers to an unavailable vendor library.
        build_dir = Path(env["III_SIM_TOOLS_PX4_BUILD_DIR"])
        build_dir.mkdir(parents=True, exist_ok=True)
        cache_marker = build_dir / "live-px4-cache-marker"
        cache_marker.write_text("live", encoding="utf-8")
        (build_dir / "build.ninja").write_text(
            "/opt/ros/missing/libgz-sim8.so.8.0\n", encoding="utf-8"
        )

        rendered = run_launcher(env, "--no-attach", "--rendered")
        assert rendered.returncode == 0, rendered.stderr
        assert cache_marker.read_text(encoding="utf-8") == "live"
        assert "gui_panes=1:0:gz" in state.read_text(encoding="utf-8")
        commands = log.read_text(encoding="utf-8")
        assert commands.count("split-window") == 1
        assert "GZ_IP=127.0.0.1" in commands
        assert "GZ_PARTITION=iii_hil_fb50fb32be99_0" in commands

        before = log.read_text(encoding="utf-8").count("split-window")
        running = run_launcher(env, "--no-attach", "--rendered")
        assert running.returncode == 0, running.stderr
        assert log.read_text(encoding="utf-8").count("split-window") == before
        running_status = run_launcher(env, "--status")
        assert running_status.returncode == 0, running_status.stderr
        assert "gazebo_gui: running" in running_status.stdout

        (proc_dir / "cmdline").write_bytes(b"/usr/bin/sleep\0100\0")
        wrong_process = run_launcher(env, "--status")
        assert "gazebo_gui: starting" in wrong_process.stdout
        (proc_dir / "cmdline").write_bytes(
            b"/usr/bin/ruby\0/opt/ros/jazzy/opt/gz_tools_vendor/bin/gz\0sim\0-g\0"
        )

        state.write_text("session=1\ngui_panes=1:1:bash\n", encoding="utf-8")
        dead_status = run_launcher(env, "--status")
        assert dead_status.returncode == 0, dead_status.stderr
        assert "gazebo_gui: dead" in dead_status.stdout

        repaired = run_launcher(
            {**env, "FAKE_TMUX_NO_SPACE_WITH_GUI": "1"}, "--no-attach", "--rendered"
        )
        assert repaired.returncode == 0, repaired.stderr
        assert "gui_panes=1:0:gz" in state.read_text(encoding="utf-8")
        commands = log.read_text(encoding="utf-8")
        assert "respawn-pane" in commands
        assert "kill-pane" not in commands
        assert commands.count("split-window") == 1

        # If a dead duplicate precedes a running GUI pane, keep the running
        # pane and avoid both killing it and creating a third pane.
        state.write_text("session=1\ngui_panes=1:1:bash,2:0:gz\n", encoding="utf-8")
        before = log.read_text(encoding="utf-8")
        duplicate = run_launcher(env, "--no-attach", "--rendered")
        assert duplicate.returncode == 0, duplicate.stderr
        assert log.read_text(encoding="utf-8").count("split-window") == 1
        assert "kill-pane" not in log.read_text(encoding="utf-8")[len(before):]
        duplicate_status = run_launcher(env, "--status")
        assert duplicate_status.returncode == 0, duplicate_status.stderr
        assert "gazebo_gui: running" in duplicate_status.stdout

        # A live pane still running its startup shell is starting. It must
        # not report ready until `exec gz sim -g` takes over.
        state.write_text(
            "session=1\ngui_panes=1:0:bash\ntransition_after=4\n",
            encoding="utf-8",
        )
        starting_status = run_launcher(env, "--status")
        assert starting_status.returncode == 0, starting_status.stderr
        assert "gazebo_gui: starting" in starting_status.stdout
        before = log.read_text(encoding="utf-8")
        waiting = run_launcher(env, "--no-attach", "--rendered")
        assert waiting.returncode == 0, waiting.stderr
        assert log.read_text(encoding="utf-8").count("split-window") == 1
        assert "respawn-pane" not in log.read_text(encoding="utf-8")[len(before):]
        waiting_status = run_launcher(env, "--status")
        assert waiting_status.returncode == 0, waiting_status.stderr
        assert "gazebo_gui: running" in waiting_status.stdout

        # A new pane that dies before the command switches away from bash
        # fails readiness and is not retried into an unbounded split loop.
        state.write_text("session=1\ngui_panes=1:1:bash\n", encoding="utf-8")
        dead_env = {**env, "FAKE_TMUX_SPLIT_DEAD": "1"}
        before = log.read_text(encoding="utf-8").count("split-window")
        dead_launch = run_launcher(dead_env, "--no-attach", "--rendered")
        assert dead_launch.returncode != 0
        assert "died before the viewer started" in dead_launch.stderr
        assert log.read_text(encoding="utf-8").count("split-window") == before
        assert "respawn-pane" in log.read_text(encoding="utf-8")

        # A pane that stays in its startup shell times out instead of being
        # counted as ready or causing a duplicate split.
        state.write_text("session=1\ngui_panes=1:0:bash\n", encoding="utf-8")
        before = log.read_text(encoding="utf-8").count("split-window")
        timed_out = run_launcher(env, "--no-attach", "--rendered")
        assert timed_out.returncode != 0
        assert "Timed out waiting" in timed_out.stderr
        assert log.read_text(encoding="utf-8").count("split-window") == before

        state.write_text("session=1\ngui_panes=\n", encoding="utf-8")
        before = log.read_text(encoding="utf-8").count("split-window")
        no_viewer = run_launcher(env, "--no-attach", "--headless")
        assert no_viewer.returncode == 0, no_viewer.stderr
        assert log.read_text(encoding="utf-8").count("split-window") == before

        stopped = run_launcher(env, "--stop")
        assert stopped.returncode == 0, stopped.stderr
        assert "session=0" in state.read_text(encoding="utf-8")
        assert "kill-session" in log.read_text(encoding="utf-8")

    assert real_cache.exists() is cache_before[0]
    if cache_before[0]:
        assert real_cache.stat().st_ino == cache_before[1]
        assert real_cache.stat().st_mtime_ns == cache_before[2]


def test_default_px4_command_disables_timestamp_synchronization() -> None:
    """SIM must match HIL: PX4 DDS stamps stay in the lockstep sim-time domain."""
    result = subprocess.run(
        ["bash", "-c", f'source <(grep -m1 "^DEFAULT_PX4_COMMAND=" "{SCRIPT}"); printf "%s" "$DEFAULT_PX4_COMMAND"'],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "WORKSPACE_ROOT": "/ws", "PX4_ROOT": "/ws/PX4", "PX4_BUILD_DIR": "/ws/b", "PX4_INSTANCE": "0"},
    )
    command = result.stdout
    assert "PX4_PARAM_UXRCE_DDS_SYNCT=0" in command
    assert command.index("PX4_PARAM_UXRCE_DDS_SYNCT=0") < command.index("/ws/b/bin/px4 -i 0")


def _foreign_px4_env(root: Path, px4_partition: str, own_partition: str | None) -> dict[str, str]:
    bin_dir = root / "bin"
    bin_dir.mkdir()
    tmux = bin_dir / "tmux"
    tmux.write_text(FAKE_TMUX, encoding="utf-8")
    tmux.chmod(0o755)
    build_dir = root / "PX4-Autopilot" / "build" / "px4_sitl_default"
    ps_listing = root / "ps.txt"
    ps_listing.write_text(f"4321 4321 px4 {build_dir}/bin/px4 -i 97\n", encoding="utf-8")
    proc = root / "proc" / "4321"
    proc.mkdir(parents=True)
    (proc / "environ").write_bytes(f"HOME=/home/iii\0GZ_PARTITION={px4_partition}\0".encode())
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_TMUX_STATE": str(root / "tmux-state"),
        "FAKE_TMUX_LOG": str(root / "tmux.log"),
        "III_SIM_TOOLS_SESSION": "sim-under-test",
        "III_SIM_TOOLS_WORKSPACE_ROOT": str(ROOT),
        "III_SIM_TOOLS_PX4_ROOT": str(root / "PX4-Autopilot"),
        "III_SIM_TOOLS_PX4_BUILD_DIR": str(build_dir),
        "III_SIM_TOOLS_PX4_INSTANCE": "97",
        "III_SIM_TOOLS_PX4_COMMAND": "true",
        "III_SIM_TOOLS_PS_COMMAND": f"cat {ps_listing}",
        "III_SIM_TOOLS_PROC_ROOT": str(root / "proc"),
    }
    env.pop("GZ_PARTITION", None)
    if own_partition is not None:
        env["GZ_PARTITION"] = own_partition
    return env


def test_sim_start_refuses_while_a_hil_partition_owns_the_px4_instance() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env = _foreign_px4_env(root, "iii_hil_fb50fb32be99_0", None)
        result = run_launcher(env, "--no-attach", "--headless")
        assert result.returncode == 2, result.stderr
        assert "Refusing to start: PX4 instance 97 is owned by another simulation" in result.stderr
        log = root / "tmux.log"
        assert "new-session" not in (log.read_text(encoding="utf-8") if log.exists() else "")


def test_sim_stop_leaves_a_hil_owned_px4_and_its_lock_alone() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env = _foreign_px4_env(root, "iii_hil_fb50fb32be99_0", None)
        lock = Path("/tmp/px4_lock-97")
        lock.write_text("held by hil", encoding="utf-8")
        try:
            result = run_launcher(env, "--stop")
            assert result.returncode == 0, result.stderr
            assert "leaving it and its instance lock untouched" in result.stderr
            assert "Cleaning stale PX4" not in result.stderr
            assert lock.exists()
        finally:
            lock.unlink(missing_ok=True)


def test_same_partition_orphan_px4_is_still_cleaned_as_stale() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env = _foreign_px4_env(root, "iii_hil_fb50fb32be99_0", "iii_hil_fb50fb32be99_0")
        result = run_launcher(env, "--stop")
        assert result.returncode == 0, result.stderr
        assert "Cleaning stale PX4 SITL state for instance 97" in result.stderr


AIRFRAME = "99999_gz_d4s_dc_drone"


def _asset_env(root: Path) -> dict[str, str]:
    """A fake workspace whose PX4 copies of the D4S assets are all current."""
    bin_dir = root / "bin"
    bin_dir.mkdir()
    tmux = bin_dir / "tmux"
    tmux.write_text(FAKE_TMUX, encoding="utf-8")
    tmux.chmod(0o755)
    make = bin_dir / "make"
    make.write_text(f"#!/bin/sh\necho \"make $*\" >> {root / 'make.log'}\n", encoding="utf-8")
    make.chmod(0o755)
    workspace = root / "ws"
    (workspace / "setup").mkdir(parents=True)
    (workspace / "setup" / "setup_dev.bash").write_text("", encoding="utf-8")
    assets = root / "sim" / "Gazebo-simulation-assets"
    installer = root / "sim" / "scripts" / "install.sh"
    installer.parent.mkdir(parents=True)
    installer.write_text(f"#!/bin/sh\necho installed >> {root / 'installer.log'}\n", encoding="utf-8")
    installer.chmod(0o755)
    px4 = root / "PX4-Autopilot"
    files = {
        "models/d4s_dc_drone/model.sdf": "Tools/simulation/gz/models/d4s_dc_drone/model.sdf",
        "worlds/hca_full_pylon_setup.sdf": "Tools/simulation/gz/worlds/hca_full_pylon_setup.sdf",
        f"init.d-posix_airframes/{AIRFRAME}": f"ROMFS/px4fmu_common/init.d-posix/airframes/{AIRFRAME}",
        "world_models/hcaa_pylon_setup/conductors.yaml": "Tools/simulation/gz/models/hcaa_pylon_setup/conductors.yaml",
    }
    for source, installed in files.items():
        for path in (assets / source, px4 / installed):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
    (px4 / "ROMFS/px4fmu_common/init.d-posix/airframes/CMakeLists.txt").write_text(AIRFRAME, encoding="utf-8")
    build_airframe = px4 / "build/px4_sitl_default/etc/init.d-posix/airframes" / AIRFRAME
    build_airframe.parent.mkdir(parents=True)
    build_airframe.write_text(f"init.d-posix_airframes/{AIRFRAME}", encoding="utf-8")
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_TMUX_STATE": str(root / "tmux-state"),
        "FAKE_TMUX_LOG": str(root / "tmux.log"),
        "III_SIM_TOOLS_SESSION": "asset-freshness",
        "III_SIM_TOOLS_WORKSPACE_ROOT": str(workspace),
        "III_SIM_TOOLS_PX4_ROOT": str(px4),
        "III_SIM_TOOLS_PX4_BUILD_DIR": str(px4 / "build/px4_sitl_default"),
        "III_SIM_TOOLS_ASSET_INSTALLER": str(installer),
        "III_SIM_TOOLS_PX4_COMMAND": "true",
        "III_SIM_TOOLS_ENSURE_ASSETS_WITH_CUSTOM_COMMAND": "1",
        "III_SIM_TOOLS_PS_COMMAND": "true",
    }


def _log(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_current_assets_and_build_start_without_reinstalling_or_rebuilding() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        result = run_launcher(_asset_env(root), "--no-attach", "--headless")
        assert result.returncode == 0, result.stderr
        assert _log(root / "installer.log") == ""
        assert _log(root / "make.log") == ""


def test_a_changed_world_model_reinstalls_the_assets() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env = _asset_env(root)
        (root / "sim/Gazebo-simulation-assets/world_models/hcaa_pylon_setup/conductors.yaml").write_text(
            "conductor_radius_m: 0.0125", encoding="utf-8")
        result = run_launcher(env, "--no-attach", "--headless")
        assert result.returncode == 0, result.stderr
        assert _log(root / "installer.log") == "installed\n"


def test_a_custom_px4_command_rebuilds_a_build_tree_with_a_stale_airframe() -> None:
    # HIL starts the built PX4 binary directly; it reads airframes from the
    # build tree, which only a PX4 build refreshes.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env = _asset_env(root)
        (root / "PX4-Autopilot/build/px4_sitl_default/etc/init.d-posix/airframes" / AIRFRAME).write_text(
            "stale", encoding="utf-8")
        # Like the ROS setup scripts, read a variable that may be unset.
        (root / "ws/setup/setup_dev.bash").write_text(
            ': "${AMENT_TRACE_SETUP_FILES}"\n', encoding="utf-8")
        result = run_launcher(env, "--no-attach", "--headless")
        assert result.returncode == 0, result.stderr
        assert "stale D4S airframe" in result.stderr
        assert _log(root / "make.log") == "make px4_sitl_default\n"


def test_the_default_px4_model_is_the_production_drone() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env = _asset_env(root)
        env.pop("III_SIM_TOOLS_PX4_COMMAND")
        env.pop("III_SIM_TOOLS_PX4_SIM_MODEL", None)
        result = run_launcher(env, "--no-attach", "--headless")
        assert result.returncode == 0, result.stderr
        assert "PX4_SIM_MODEL=gz_d4s_dc_drone\\ GZ_IP" in _log(root / "tmux.log")


def test_sim_model_selects_the_px4_model_of_a_new_session() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env = _asset_env(root)
        env.pop("III_SIM_TOOLS_PX4_COMMAND")
        result = run_launcher(
            env, "--no-attach", "--headless", "--sim-model", "gz_d4s_dc_drone_powerline_eval")
        assert result.returncode == 0, result.stderr
        assert "PX4_SIM_MODEL=gz_d4s_dc_drone_powerline_eval\\ GZ_IP" in _log(root / "tmux.log")


def test_the_sim_model_environment_variable_selects_the_px4_model() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env = _asset_env(root)
        env.pop("III_SIM_TOOLS_PX4_COMMAND")
        env["III_SIM_TOOLS_PX4_SIM_MODEL"] = "gz_d4s_dc_drone_powerline_eval"
        result = run_launcher(env, "--no-attach", "--headless")
        assert result.returncode == 0, result.stderr
        assert "PX4_SIM_MODEL=gz_d4s_dc_drone_powerline_eval\\ GZ_IP" in _log(root / "tmux.log")


def test_an_invalid_sim_model_is_rejected_before_starting() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env = _asset_env(root)
        result = run_launcher(env, "--no-attach", "--headless", "--sim-model", "d4s_dc_drone")
        assert result.returncode == 1
        assert "expected gz_<model>" in result.stderr
        assert "new-session" not in _log(root / "tmux.log")


def test_a_selected_iii_model_that_is_not_installed_reinstalls_the_assets() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env = _asset_env(root)
        variant = root / "sim/Gazebo-simulation-assets/models/d4s_dc_drone_powerline_eval/model.sdf"
        variant.parent.mkdir(parents=True)
        variant.write_text("variant", encoding="utf-8")
        result = run_launcher(
            env, "--no-attach", "--headless", "--sim-model", "gz_d4s_dc_drone_powerline_eval")
        assert result.returncode == 0, result.stderr
        assert _log(root / "installer.log") == "installed\n"


def test_a_changed_variant_model_reinstalls_the_assets_for_any_selected_model() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env = _asset_env(root)
        for base in ("sim/Gazebo-simulation-assets/models", "PX4-Autopilot/Tools/simulation/gz/models"):
            variant = root / base / "d4s_dc_drone_powerline_eval/model.sdf"
            variant.parent.mkdir(parents=True)
            variant.write_text("variant", encoding="utf-8")
        (root / "sim/Gazebo-simulation-assets/models/d4s_dc_drone_powerline_eval/model.sdf").write_text(
            "changed variant", encoding="utf-8")
        result = run_launcher(env, "--no-attach", "--headless")
        assert result.returncode == 0, result.stderr
        assert _log(root / "installer.log") == "installed\n"


def test_status_reports_the_requested_and_the_running_px4_model() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env = _foreign_px4_env(root, "iii_sim_partition", "iii_sim_partition")
        (root / "proc" / "4321" / "environ").write_bytes(
            b"GZ_PARTITION=iii_sim_partition\0PX4_SIM_MODEL=gz_d4s_dc_drone_powerline_eval\0")
        env["III_SIM_TOOLS_STATUS_DISCOVERY_TIMEOUT_SEC"] = "1"
        result = run_launcher(env, "--status")
        assert result.returncode == 0, result.stderr
        assert "px4_sim_model_requested: gz_d4s_dc_drone\n" in result.stdout
        assert "px4_sim_model_running: gz_d4s_dc_drone_powerline_eval\n" in result.stdout
