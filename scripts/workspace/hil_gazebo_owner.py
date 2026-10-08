#!/usr/bin/env python3
"""Ownership checks for a single workstation Gazebo HIL server.

The helper deliberately owns only one process.  It never follows process
groups and it requires the process identity (PID and kernel start ticks) and
the relevant process metadata to remain unchanged before sending a signal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal as signal_module
import shlex
import tempfile
import time
from typing import Callable, Iterable, Mapping, Sequence


DEFAULT_PROC_ROOT = Path("/proc")
OWNER_FIELDS = frozenset(
    {
        "pid",
        "start_ticks",
        "partition",
        "workspace",
        "command",
        "px4_pid",
        "px4_start_ticks",
        "process_group",
        "session",
    }
)
WORLD_DIRECTORY = Path("PX4-Autopilot") / "Tools" / "simulation" / "gz" / "worlds"


class OwnershipError(RuntimeError):
    """Raised when ownership is absent, ambiguous, stale, or unsafe to use."""


class ProcessInfo:
    __slots__ = (
        "pid",
        "start_ticks",
        "state",
        "cwd",
        "command",
        "environment",
        "executable",
        "process_group",
        "session",
    )

    def __init__(
        self,
        pid: int,
        start_ticks: int,
        state: str,
        cwd: str,
        command: tuple[str, ...],
        environment: Mapping[str, str],
        executable: str,
        process_group: int,
        session: int,
    ) -> None:
        self.pid = pid
        self.start_ticks = start_ticks
        self.state = state
        self.cwd = cwd
        self.command = command
        self.environment = environment
        self.executable = executable
        self.process_group = process_group
        self.session = session


def canonical_workspace(workspace: os.PathLike[str] | str) -> str:
    return os.path.realpath(os.fspath(workspace))


def workspace_partition(workspace: os.PathLike[str] | str, instance: int | str = 0) -> str:
    """Return the stable partition derived from the workspace real path."""

    digest = hashlib.sha256(canonical_workspace(workspace).encode("utf-8")).hexdigest()[:12]
    return f"iii_hil_{digest}_{instance}"


def partition_name(workspace: os.PathLike[str] | str, instance: int | str) -> str:
    """Backward-compatible descriptive alias for :func:`workspace_partition`."""

    return workspace_partition(workspace, instance)


def _under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _parse_stat(text: str) -> tuple[str, int, int, int]:
    """Parse state and field 22 from procfs stat.

    The comm field is parenthesized and may contain spaces and parentheses, so
    the fields must be split only after the final closing parenthesis.  After
    that point field 3 is offset 0 and field 22 is offset 19.
    """

    closing = text.rfind(")")
    if closing < 0:
        raise ValueError("proc stat has no closing comm parenthesis")
    fields = text[closing + 1 :].split()
    if len(fields) <= 19:
        raise ValueError("proc stat is too short")
    state = fields[0]
    process_group = int(fields[2])
    session = int(fields[3])
    start_ticks = int(fields[19])
    return state, start_ticks, process_group, session


def _read_command(path: Path) -> tuple[str, ...]:
    raw = path.read_bytes()
    return tuple(part.decode("utf-8", errors="surrogateescape") for part in raw.split(b"\0") if part)


def _read_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    raw = path.read_bytes()
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        values[key.decode("utf-8", errors="surrogateescape")] = value.decode(
            "utf-8", errors="surrogateescape"
        )
    return values


def read_process(pid: int, *, proc_root: os.PathLike[str] | str = DEFAULT_PROC_ROOT) -> ProcessInfo | None:
    """Read one process snapshot, returning ``None`` if it has disappeared."""

    process_root = Path(proc_root) / str(pid)
    try:
        state, start_ticks, process_group, session = _parse_stat(
            (process_root / "stat").read_text(encoding="utf-8", errors="replace")
        )
        cwd = os.path.realpath(os.readlink(process_root / "cwd"))
        command = _read_command(process_root / "cmdline")
        environment = _read_environment(process_root / "environ")
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError, ValueError):
        return None
    try:
        executable = os.path.realpath(os.readlink(process_root / "exe"))
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        # Zombie processes and a few restricted procfs configurations do not
        # expose /proc/<pid>/exe.  They remain useful for disappearance and
        # zombie handling, while PX4 validation rejects a missing executable.
        executable = ""
    return ProcessInfo(pid, start_ticks, state, cwd, command, environment, executable, process_group, session)


def _iter_pids(proc_root: os.PathLike[str] | str) -> Iterable[int]:
    root = Path(proc_root)
    try:
        entries = sorted(root.iterdir(), key=lambda entry: entry.name)
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return
    for entry in entries:
        if entry.name.isdecimal():
            yield int(entry.name)


def iter_processes(*, proc_root: os.PathLike[str] | str = DEFAULT_PROC_ROOT) -> Iterable[ProcessInfo]:
    for pid in _iter_pids(proc_root):
        process = read_process(pid, proc_root=proc_root)
        if process is not None:
            yield process


def _is_gz_server(command: Sequence[str]) -> bool:
    try:
        gz_index = next(index for index, token in enumerate(command) if os.path.basename(token) == "gz")
    except StopIteration:
        return False
    if gz_index + 1 >= len(command) or command[gz_index + 1] != "sim":
        return False
    return any(token in {"-s", "--server", "server"} for token in command[gz_index + 2 :])


def _world_path_in_command(command: Sequence[str], *, cwd: str, worlds_root: str) -> bool:
    for raw_token in command:
        token = raw_token
        if token.startswith("--world="):
            token = token.split("=", 1)[1]
        if not token or token.startswith("-"):
            continue
        candidate = token if os.path.isabs(token) else os.path.join(cwd, token)
        if _under(os.path.realpath(candidate), worlds_root):
            return True
    return False


def _is_valid_px4(
    process: ProcessInfo,
    *,
    workspace: os.PathLike[str] | str,
    partition: str,
    expected_start_ticks: int,
) -> bool:
    workspace_root = canonical_workspace(workspace)
    px4_root = os.path.realpath(os.path.join(workspace_root, "PX4-Autopilot"))
    return (
        process.state != "Z"
        and process.start_ticks == expected_start_ticks
        and process.process_group > 0
        and process.session > 0
        and bool(process.command)
        and os.path.basename(process.command[0]) == "px4"
        and os.path.basename(process.executable) == "px4"
        and _under(process.executable, px4_root)
        and _under(process.cwd, workspace_root)
        and process.environment.get("GZ_PARTITION") == partition
    )


def is_matching_server(
    process: ProcessInfo,
    *,
    workspace: os.PathLike[str] | str,
    partition: str,
) -> bool:
    workspace_root = canonical_workspace(workspace)
    worlds_root = os.path.realpath(os.path.join(workspace_root, os.fspath(WORLD_DIRECTORY)))
    # Gazebo rewrites its Linux process title into one argv entry after
    # startup. Parse that title only for classification; retain the original
    # proc command verbatim in the record and identity checks.
    command = process.command
    if len(command) == 1:
        try:
            command = tuple(shlex.split(command[0]))
        except ValueError:
            return False
    return (
        process.state != "Z"
        and _under(process.cwd, workspace_root)
        and process.environment.get("GZ_PARTITION") == partition
        and _is_gz_server(command)
        and _world_path_in_command(command, cwd=process.cwd, worlds_root=worlds_root)
    )


def matching_servers(
    workspace: os.PathLike[str] | str,
    partition: str,
    *,
    proc_root: os.PathLike[str] | str = DEFAULT_PROC_ROOT,
) -> list[ProcessInfo]:
    return [
        process
        for process in iter_processes(proc_root=proc_root)
        if is_matching_server(process, workspace=workspace, partition=partition)
    ]


def _record_value(
    process: ProcessInfo,
    px4: ProcessInfo,
    *,
    workspace: str,
    partition: str,
) -> dict[str, object]:
    return {
        "pid": process.pid,
        "start_ticks": process.start_ticks,
        "partition": partition,
        "workspace": workspace,
        "command": list(process.command),
        "px4_pid": px4.pid,
        "px4_start_ticks": px4.start_ticks,
        "process_group": process.process_group,
        "session": process.session,
    }


def _write_record(path: os.PathLike[str] | str, value: Mapping[str, object]) -> None:
    record_path = Path(path)
    parent = record_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(prefix=f".{record_path.name}.", dir=parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, record_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def _load_record(path: os.PathLike[str] | str) -> dict[str, object] | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise OwnershipError(f"invalid owner record: {path}") from exc
    if not isinstance(value, dict) or set(value) != OWNER_FIELDS:
        raise OwnershipError("owner record has unexpected fields")
    if isinstance(value.get("pid"), bool) or not isinstance(value.get("pid"), int) or value["pid"] <= 0:
        raise OwnershipError("owner record PID is invalid")
    if (
        isinstance(value.get("start_ticks"), bool)
        or not isinstance(value.get("start_ticks"), int)
        or value["start_ticks"] <= 0
    ):
        raise OwnershipError("owner record start ticks are invalid")
    for field in ("px4_pid", "px4_start_ticks", "process_group", "session"):
        if isinstance(value.get(field), bool) or not isinstance(value.get(field), int) or value[field] <= 0:
            raise OwnershipError(f"owner record {field} is invalid")
    if not isinstance(value.get("partition"), str) or not isinstance(value.get("workspace"), str):
        raise OwnershipError("owner record identity is invalid")
    command = value.get("command")
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise OwnershipError("owner record command is invalid")
    return value


def claim(
    workspace: os.PathLike[str] | str,
    partition: str,
    record: os.PathLike[str] | str,
    not_before_ticks: int,
    px4_pid: int,
    *,
    proc_root: os.PathLike[str] | str = DEFAULT_PROC_ROOT,
) -> dict[str, object]:
    workspace_root = canonical_workspace(workspace)
    px4 = read_process(px4_pid, proc_root=proc_root)
    if px4 is None or not _is_valid_px4(
        px4,
        workspace=workspace_root,
        partition=partition,
        expected_start_ticks=not_before_ticks,
    ):
        raise OwnershipError("PX4 process identity is missing or mismatched")
    all_matching = matching_servers(workspace_root, partition, proc_root=proc_root)
    if len(all_matching) != 1:
        if not all_matching:
            raise OwnershipError("no matching Gazebo server")
        raise OwnershipError("multiple matching Gazebo servers")
    candidate = all_matching[0]
    if candidate.start_ticks <= not_before_ticks:
        raise OwnershipError("matching Gazebo server is older than the requested start tick")
    if candidate.process_group != px4.process_group or candidate.session != px4.session:
        raise OwnershipError("Gazebo server process group or session does not match PX4")
    if candidate.process_group <= 0 or candidate.session <= 0:
        raise OwnershipError("Gazebo server process group or session is invalid")
    value = _record_value(candidate, px4, workspace=workspace_root, partition=partition)
    _write_record(record, value)
    return value


def _record_matches_process(
    value: Mapping[str, object],
    process: ProcessInfo,
    *,
    workspace: str,
    partition: str,
) -> bool:
    return (
        value.get("pid") == process.pid
        and value.get("start_ticks") == process.start_ticks
        and value.get("partition") == partition
        and value.get("workspace") == workspace
        and value.get("command") == list(process.command)
        and value.get("process_group") == process.process_group
        and value.get("session") == process.session
        and is_matching_server(process, workspace=workspace, partition=partition)
    )


def inspect(
    workspace: os.PathLike[str] | str,
    partition: str,
    record: os.PathLike[str] | str,
    *,
    proc_root: os.PathLike[str] | str = DEFAULT_PROC_ROOT,
) -> dict[str, object]:
    value = _load_record(record)
    if value is None:
        raise OwnershipError("owner record is missing")
    workspace_root = canonical_workspace(workspace)
    process = read_process(int(value["pid"]), proc_root=proc_root)
    candidates = matching_servers(workspace_root, partition, proc_root=proc_root)
    if (
        process is None
        or process.state == "Z"
        or len(candidates) != 1
        or candidates[0].pid != process.pid
        or not _record_matches_process(value, process, workspace=workspace_root, partition=partition)
    ):
        raise OwnershipError("owner record does not identify the sole live matching Gazebo server")
    return value


def _proc_entry_exists(pid: int, proc_root: os.PathLike[str] | str) -> bool:
    return (Path(proc_root) / str(pid)).exists()


def _sleep_until_exit(
    pid: int,
    value: Mapping[str, object],
    *,
    workspace: str,
    partition: str,
    proc_root: os.PathLike[str] | str,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    timeout: float = 10.0,
) -> str:
    """Return ``gone``, ``changed``, or ``timeout``."""

    deadline = clock() + timeout
    iterations = 0
    while clock() < deadline and iterations < 1000:
        process = read_process(pid, proc_root=proc_root)
        if process is None or process.state == "Z":
            return "gone"
        if not _record_matches_process(value, process, workspace=workspace, partition=partition):
            return "changed"
        remaining = max(0.0, deadline - clock())
        sleep(min(0.05, remaining))
        iterations += 1
    process = read_process(pid, proc_root=proc_root)
    if process is None or process.state == "Z":
        return "gone"
    if not _record_matches_process(value, process, workspace=workspace, partition=partition):
        return "changed"
    return "timeout"


def _remove_if_stopped(
    record: os.PathLike[str] | str,
    *,
    workspace: str,
    partition: str,
    proc_root: os.PathLike[str] | str,
) -> None:
    if matching_servers(workspace, partition, proc_root=proc_root):
        raise OwnershipError("a matching Gazebo server remains")
    try:
        Path(record).unlink()
    except FileNotFoundError:
        pass


def stop(
    workspace: os.PathLike[str] | str,
    partition: str,
    record: os.PathLike[str] | str,
    *,
    proc_root: os.PathLike[str] | str = DEFAULT_PROC_ROOT,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    signal_fn: Callable[[int, int], None] | None = None,
) -> None:
    clock = time.monotonic if clock is None else clock
    sleep = time.sleep if sleep is None else sleep
    signal_fn = os.kill if signal_fn is None else signal_fn
    workspace_root = canonical_workspace(workspace)
    value = _load_record(record)
    candidates = matching_servers(workspace_root, partition, proc_root=proc_root)
    if value is None:
        if candidates:
            raise OwnershipError("matching Gazebo server exists without an owner record")
        return

    pid = int(value["pid"])
    process = read_process(pid, proc_root=proc_root)
    if process is None or process.state == "Z":
        if _proc_entry_exists(pid, proc_root) and process is None:
            raise OwnershipError("recorded PID is present but unreadable")
        if not candidates:
            _remove_if_stopped(record, workspace=workspace_root, partition=partition, proc_root=proc_root)
            return
        raise OwnershipError("recorded PID is gone but a matching Gazebo server remains")

    if not _record_matches_process(value, process, workspace=workspace_root, partition=partition):
        raise OwnershipError("owner record metadata does not match the live PID")
    if len(candidates) != 1 or candidates[0].pid != pid:
        raise OwnershipError("owner record is not the sole matching Gazebo server")

    try:
        signal_fn(pid, signal_module.SIGTERM)
    except (ProcessLookupError, FileNotFoundError):
        process = read_process(pid, proc_root=proc_root)
        if process is None or process.state == "Z":
            _remove_if_stopped(record, workspace=workspace_root, partition=partition, proc_root=proc_root)
            return
        raise OwnershipError("recorded PID disappeared during termination")
    except OSError as exc:
        raise OwnershipError(f"unable to terminate recorded PID: {exc}") from exc

    result = _sleep_until_exit(
        pid,
        value,
        workspace=workspace_root,
        partition=partition,
        proc_root=proc_root,
        clock=clock,
        sleep=sleep,
    )
    if result == "gone":
        _remove_if_stopped(record, workspace=workspace_root, partition=partition, proc_root=proc_root)
        return
    if result == "changed":
        raise OwnershipError("recorded PID identity changed; refusing escalation")

    # Revalidate every identity field and the sole-server condition immediately
    # before escalating.  A reused PID or a newly appearing server is never a
    # valid target for SIGKILL.
    process = read_process(pid, proc_root=proc_root)
    candidates = matching_servers(workspace_root, partition, proc_root=proc_root)
    if (
        process is None
        or process.state == "Z"
        or len(candidates) != 1
        or candidates[0].pid != pid
        or not _record_matches_process(value, process, workspace=workspace_root, partition=partition)
    ):
        if process is None or process.state == "Z":
            _remove_if_stopped(record, workspace=workspace_root, partition=partition, proc_root=proc_root)
            return
        raise OwnershipError("owner identity changed; refusing SIGKILL")
    try:
        signal_fn(pid, signal_module.SIGKILL)
    except (ProcessLookupError, FileNotFoundError):
        pass
    except OSError as exc:
        raise OwnershipError(f"unable to kill recorded PID: {exc}") from exc
    result = _sleep_until_exit(
        pid,
        value,
        workspace=workspace_root,
        partition=partition,
        proc_root=proc_root,
        clock=clock,
        sleep=sleep,
    )
    if result != "gone":
        raise OwnershipError("recorded Gazebo server did not exit after SIGKILL")
    _remove_if_stopped(record, workspace=workspace_root, partition=partition, proc_root=proc_root)


def _compact_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    partition_parser = commands.add_parser("partition")
    partition_parser.add_argument("--workspace", required=True)
    partition_parser.add_argument("--instance", required=True, type=int)

    claim_parser = commands.add_parser("claim")
    claim_parser.add_argument("--workspace", required=True)
    claim_parser.add_argument("--partition", required=True)
    claim_parser.add_argument("--record", required=True)
    claim_parser.add_argument("--not-before-ticks", required=True, type=int)
    claim_parser.add_argument("--px4-pid", required=True, type=int)

    for name in ("inspect", "stop"):
        command_parser = commands.add_parser(name)
        command_parser.add_argument("--workspace", required=True)
        command_parser.add_argument("--partition", required=True)
        command_parser.add_argument("--record", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    proc_root: os.PathLike[str] | str = DEFAULT_PROC_ROOT,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    signal_fn: Callable[[int, int], None] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "partition":
            print(workspace_partition(args.workspace, args.instance))
        elif args.command == "claim":
            print(
                _compact_json(
                    claim(
                        args.workspace,
                        args.partition,
                        args.record,
                        args.not_before_ticks,
                        args.px4_pid,
                        proc_root=proc_root,
                    )
                )
            )
        elif args.command == "inspect":
            print(_compact_json(inspect(args.workspace, args.partition, args.record, proc_root=proc_root)))
        elif args.command == "stop":
            stop(
                args.workspace,
                args.partition,
                args.record,
                proc_root=proc_root,
                clock=clock,
                sleep=sleep,
                signal_fn=signal_fn,
            )
        return 0
    except OwnershipError as exc:
        print(f"refusing: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
