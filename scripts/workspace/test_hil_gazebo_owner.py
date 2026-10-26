from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil

import pytest


MODULE_PATH = Path(__file__).with_name("hil_gazebo_owner.py")
SPEC = importlib.util.spec_from_file_location("hil_gazebo_owner", MODULE_PATH)
assert SPEC and SPEC.loader
owner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(owner)


def _write_process(
    proc_root: Path,
    pid: int,
    *,
    workspace: Path,
    partition: str,
    start_ticks: int,
    state: str = "S",
    command: tuple[str, ...] | None = None,
    cwd: Path | None = None,
    executable: Path | str | None = None,
    process_group: int = 77,
    session: int = 88,
) -> Path:
    process = proc_root / str(pid)
    process.mkdir(parents=True)
    # The comm field intentionally contains spaces and a closing parenthesis;
    # the helper must parse field 22 only after the final closing parenthesis.
    fields = [state, "0", str(process_group), str(session)] + ["0"] * 15 + [str(start_ticks)] + ["0"] * 4
    (process / "stat").write_text(f"{pid} (gz sim server ) {' '.join(fields)}", encoding="utf-8")
    world = workspace / "PX4-Autopilot/Tools/simulation/gz/worlds/hil.sdf"
    command = command or ("/usr/bin/gz", "sim", "-s", "-r", str(world))
    (process / "cmdline").write_bytes(b"\0".join(item.encode() for item in command) + b"\0")
    (process / "environ").write_bytes(f"GZ_PARTITION={partition}\0OTHER=value\0".encode())
    os_cwd = cwd or workspace
    (process / "cwd").symlink_to(os_cwd, target_is_directory=True)
    if executable is None:
        executable = "/usr/bin/gz"
    (process / "exe").symlink_to(executable)
    return process


def _write_px4(
    proc_root: Path,
    pid: int,
    *,
    workspace: Path,
    partition: str,
    start_ticks: int,
    process_group: int = 77,
    session: int = 88,
    cwd: Path | None = None,
    executable: Path | str | None = None,
) -> Path:
    px4_executable = executable or workspace / "PX4-Autopilot/build/px4"
    Path(px4_executable).parent.mkdir(parents=True, exist_ok=True)
    return _write_process(
        proc_root,
        pid,
        workspace=workspace,
        partition=partition,
        start_ticks=start_ticks,
        command=(str(px4_executable), "-s", "rcS"),
        cwd=cwd,
        executable=px4_executable,
        process_group=process_group,
        session=session,
    )


def _claim(
    workspace: Path,
    proc_root: Path,
    partition: str,
    record: Path,
    *,
    px4_pid: int = 40,
    px4_start_ticks: int = 100,
    gz_pid: int = 41,
    gz_start_ticks: int = 101,
    process_group: int = 77,
    session: int = 88,
) -> dict[str, object]:
    _write_px4(
        proc_root,
        px4_pid,
        workspace=workspace,
        partition=partition,
        start_ticks=px4_start_ticks,
        process_group=process_group,
        session=session,
    )
    _write_process(
        proc_root,
        gz_pid,
        workspace=workspace,
        partition=partition,
        start_ticks=gz_start_ticks,
        process_group=process_group,
        session=session,
    )
    return owner.claim(workspace, partition, record, px4_start_ticks, px4_pid, proc_root=proc_root)


def _fixture(tmp_path: Path) -> tuple[Path, Path, str, Path]:
    workspace = tmp_path / "workspace"
    (workspace / "PX4-Autopilot/Tools/simulation/gz/worlds").mkdir(parents=True)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    partition = owner.workspace_partition(workspace, 0)
    record = tmp_path / "runtime" / "gazebo-owner.json"
    return workspace, proc_root, partition, record


def test_claim_accepts_gazebo_rewritten_process_title(tmp_path: Path):
    workspace, proc_root, partition, record = _fixture(tmp_path)
    _write_px4(proc_root, 40, workspace=workspace, partition=partition, start_ticks=100)
    title = f"gz sim --verbose=1 -r -s {workspace}/PX4-Autopilot/Tools/simulation/gz/worlds/hil.sdf"
    _write_process(proc_root, 41, workspace=workspace, partition=partition,
                   start_ticks=101, command=(title,))

    value = owner.claim(workspace, partition, record, 100, 40, proc_root=proc_root)

    assert value["pid"] == 41
    assert value["command"] == [title]
    assert owner.inspect(workspace, partition, record, proc_root=proc_root) == value


def test_partition_is_stable_for_realpath_and_distinct_for_workspaces(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(first, target_is_directory=True)

    assert owner.workspace_partition(first, 3) == owner.workspace_partition(alias, 3)
    assert owner.workspace_partition(first, 3).startswith("iii_hil_")
    assert owner.workspace_partition(first, 3) != owner.workspace_partition(second, 3)
    assert owner.workspace_partition(first, 3).endswith("_3")


def test_claim_accepts_only_one_new_matching_server_and_writes_record(tmp_path: Path):
    workspace, proc_root, partition, record = _fixture(tmp_path)
    value = _claim(workspace, proc_root, partition, record)

    assert value["pid"] == 41
    assert value["start_ticks"] == 101
    assert value["workspace"] == str(workspace.resolve())
    assert value["px4_pid"] == 40
    assert value["px4_start_ticks"] == 100
    assert value["process_group"] == 77
    assert value["session"] == 88
    assert json.loads(record.read_text()) == value


def test_claim_rejects_older_server(tmp_path: Path):
    workspace, proc_root, partition, record = _fixture(tmp_path)
    _write_px4(proc_root, 40, workspace=workspace, partition=partition, start_ticks=100)
    _write_process(proc_root, 41, workspace=workspace, partition=partition, start_ticks=100)

    with pytest.raises(owner.OwnershipError, match="older"):
        owner.claim(workspace, partition, record, 100, 40, proc_root=proc_root)
    assert not record.exists()


def test_claim_rejects_server_started_at_same_tick_as_px4(tmp_path: Path):
    workspace, proc_root, partition, record = _fixture(tmp_path)
    _write_px4(proc_root, 40, workspace=workspace, partition=partition, start_ticks=100)
    _write_process(proc_root, 41, workspace=workspace, partition=partition, start_ticks=100)

    with pytest.raises(owner.OwnershipError, match="older"):
        owner.claim(workspace, partition, record, 100, 40, proc_root=proc_root)


def test_foreign_partition_and_workspace_are_not_adopted(tmp_path: Path):
    workspace, proc_root, partition, record = _fixture(tmp_path)
    foreign_workspace = tmp_path / "foreign"
    (foreign_workspace / "PX4-Autopilot/Tools/simulation/gz/worlds").mkdir(parents=True)
    _write_process(proc_root, 41, workspace=workspace, partition="foreign", start_ticks=100)
    _write_process(proc_root, 42, workspace=foreign_workspace, partition=partition, start_ticks=101)

    with pytest.raises(owner.OwnershipError, match="no matching"):
        _write_px4(proc_root, 40, workspace=workspace, partition=partition, start_ticks=1)
        owner.claim(workspace, partition, record, 1, 40, proc_root=proc_root)


def test_multiple_matching_servers_are_rejected(tmp_path: Path):
    workspace, proc_root, partition, record = _fixture(tmp_path)
    _write_px4(proc_root, 40, workspace=workspace, partition=partition, start_ticks=100)
    _write_process(proc_root, 41, workspace=workspace, partition=partition, start_ticks=101)
    _write_process(proc_root, 42, workspace=workspace, partition=partition, start_ticks=102)

    with pytest.raises(owner.OwnershipError, match="multiple"):
        owner.claim(workspace, partition, record, 100, 40, proc_root=proc_root)


def test_claim_rejects_same_partition_server_from_foreign_process_group(tmp_path: Path):
    workspace, proc_root, partition, record = _fixture(tmp_path)
    _write_px4(proc_root, 40, workspace=workspace, partition=partition, start_ticks=100, process_group=77, session=88)
    _write_process(proc_root, 41, workspace=workspace, partition=partition, start_ticks=101, process_group=99, session=88)

    with pytest.raises(owner.OwnershipError, match="process group"):
        owner.claim(workspace, partition, record, 100, 40, proc_root=proc_root)


def test_claim_rejects_stale_or_mismatched_px4(tmp_path: Path):
    workspace, proc_root, partition, record = _fixture(tmp_path)
    _write_px4(proc_root, 40, workspace=workspace, partition=partition, start_ticks=99)
    _write_process(proc_root, 41, workspace=workspace, partition=partition, start_ticks=101)

    with pytest.raises(owner.OwnershipError, match="PX4"):
        owner.claim(workspace, partition, record, 100, 40, proc_root=proc_root)


def test_claim_rejects_px4_with_mismatched_partition(tmp_path: Path):
    workspace, proc_root, partition, record = _fixture(tmp_path)
    _write_px4(proc_root, 40, workspace=workspace, partition="foreign", start_ticks=100)
    _write_process(proc_root, 41, workspace=workspace, partition=partition, start_ticks=101)

    with pytest.raises(owner.OwnershipError, match="PX4"):
        owner.claim(workspace, partition, record, 100, 40, proc_root=proc_root)


@pytest.mark.parametrize("process_group,session", [(0, 88), (77, 0)])
def test_claim_rejects_px4_without_positive_process_group_and_session(
    tmp_path: Path,
    process_group: int,
    session: int,
):
    workspace, proc_root, partition, record = _fixture(tmp_path)
    _write_px4(
        proc_root,
        40,
        workspace=workspace,
        partition=partition,
        start_ticks=100,
        process_group=process_group,
        session=session,
    )
    _write_process(
        proc_root,
        41,
        workspace=workspace,
        partition=partition,
        start_ticks=101,
        process_group=process_group,
        session=session,
    )

    with pytest.raises(owner.OwnershipError, match="PX4"):
        owner.claim(workspace, partition, record, 100, 40, proc_root=proc_root)


def test_pid_reuse_refuses_stop_without_signalling(tmp_path: Path):
    workspace, proc_root, partition, record = _fixture(tmp_path)
    _claim(workspace, proc_root, partition, record)
    shutil.rmtree(proc_root / "41")
    _write_process(proc_root, 41, workspace=workspace, partition=partition, start_ticks=200, process_group=77, session=88)
    signals: list[tuple[int, int]] = []

    with pytest.raises(owner.OwnershipError, match="metadata"):
        owner.stop(workspace, partition, record, proc_root=proc_root, signal_fn=lambda pid, sig: signals.append((pid, sig)))
    assert signals == []
    assert record.exists()


def test_valid_stop_signals_only_recorded_pid_and_removes_record(tmp_path: Path):
    workspace, proc_root, partition, record = _fixture(tmp_path)
    _claim(workspace, proc_root, partition, record)
    _write_process(proc_root, 42, workspace=workspace, partition="foreign", start_ticks=101)
    # Stop is required to work after PX4 has already exited; its provenance is
    # retained in the record, but PX4 need not remain alive for server cleanup.
    shutil.rmtree(proc_root / "40")
    signals: list[tuple[int, int]] = []

    def signal_fn(pid: int, sig: int) -> None:
        signals.append((pid, sig))
        shutil.rmtree(proc_root / str(pid))

    owner.stop(workspace, partition, record, proc_root=proc_root, signal_fn=signal_fn)

    assert signals == [(41, owner.signal_module.SIGTERM)]
    assert not record.exists()
    assert (proc_root / "42").exists()


def test_stop_removes_record_for_already_exited_owner_without_signalling(tmp_path: Path):
    workspace, proc_root, partition, record = _fixture(tmp_path)
    _claim(workspace, proc_root, partition, record)
    shutil.rmtree(proc_root / "41")
    signals: list[tuple[int, int]] = []

    owner.stop(workspace, partition, record, proc_root=proc_root, signal_fn=lambda pid, sig: signals.append((pid, sig)))

    assert signals == []
    assert not record.exists()


def test_inspect_requires_exact_live_identity_and_one_server(tmp_path: Path):
    workspace, proc_root, partition, record = _fixture(tmp_path)
    expected = _claim(workspace, proc_root, partition, record)

    assert owner.inspect(workspace, partition, record, proc_root=proc_root) == expected


def test_inspect_rejects_changed_server_process_group_or_session(tmp_path: Path):
    workspace, proc_root, partition, record = _fixture(tmp_path)
    _claim(workspace, proc_root, partition, record)
    shutil.rmtree(proc_root / "41")
    _write_process(proc_root, 41, workspace=workspace, partition=partition, start_ticks=101, process_group=77, session=99)

    with pytest.raises(owner.OwnershipError, match="sole live"):
        owner.inspect(workspace, partition, record, proc_root=proc_root)
