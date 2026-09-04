#!/usr/bin/env python3
"""Host-side regression tests for the iii-dev command transport."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


WORKSPACE = Path(__file__).resolve().parents[2]
ENTRYPOINT = WORKSPACE / "iii-dev"
SIM_SCRIPT = WORKSPACE / "tools" / "simulation" / "launch_simulation_tools.sh"


FAKE_DOCKER = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
with Path(os.environ["FAKE_COMMAND_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\n")

if args and args[0] == "info":
    raise SystemExit(int(os.environ.get("FAKE_DOCKER_INFO_RC", "0")))
if args and args[0] == "ps":
    key = "FAKE_DOCKER_ALL" if "-a" in args else "FAKE_DOCKER_RUNNING"
    value = os.environ.get(key, "")
    if value:
        print(value)
    raise SystemExit(0)
if args and args[0] == "exec":
    value = os.environ.get("FAKE_DOCKER_EXEC_STDOUT", "")
    if value:
        print(value)
    raise SystemExit(int(os.environ.get("FAKE_DOCKER_EXEC_RC", "0")))
raise SystemExit(0)
"""


FAKE_TMUX = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
with Path(os.environ["FAKE_COMMAND_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(["tmux", *args]) + "\n")
if args and args[0] == "has-session":
    raise SystemExit(int(os.environ.get("FAKE_TMUX_HAS_SESSION_RC", "0")))
raise SystemExit(0)
"""


class IiiDevTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.log = self.root / "commands.jsonl"
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.docker = self.bin_dir / "docker"
        self.docker.write_text(FAKE_DOCKER, encoding="utf-8")
        self.docker.chmod(0o755)
        self.env = {
            **os.environ,
            "FAKE_COMMAND_LOG": str(self.log),
            "FAKE_DOCKER_RUNNING": "container-123\tiii-dev-test",
            "III_DEV_DOCKER_BIN": str(self.docker),
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ENTRYPOINT), *args],
            cwd=WORKSPACE,
            env=env or self.env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def commands(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
        ]

    def exec_commands(self) -> list[list[str]]:
        return [
            command for command in self.commands() if command and command[0] == "exec"
        ]

    def test_help_does_not_require_docker(self) -> None:
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stack start [options]", result.stdout)
        self.assertEqual(self.commands(), [])

    def test_every_wrapper_command_and_subcommand_supports_both_help_flags(
        self,
    ) -> None:
        paths = [
            ("container",),
            ("container", "status"),
            ("container", "up"),
            ("shell",),
            ("exec",),
            ("sim",),
            ("sim", "start"),
            ("sim", "restart"),
            ("sim", "attach"),
            ("sim", "status"),
            ("sim", "stop"),
            ("system",),
            ("api",),
            ("api", "start"),
            ("api", "stop"),
            ("api", "restart"),
            ("api", "status"),
            ("api", "logs"),
            ("gui",),
            ("gui", "start"),
            ("gui", "stop"),
            ("gui", "restart"),
            ("gui", "recover"),
            ("gui", "status"),
            ("gui", "logs"),
            ("gui", "open"),
            ("tmux",),
            ("tmux", "list"),
            ("tmux", "attach"),
            ("rosbag",),
            ("rosbag", "status"),
            ("rosbag", "list"),
            ("rosbag", "start"),
            ("rosbag", "stop"),
            ("rosbag", "delete"),
            ("rosbag", "clear"),
            ("stack",),
            ("stack", "start"),
            ("stack", "status"),
            ("stack", "attach"),
            ("stack", "stop"),
        ]

        for path in paths:
            for flag in ("-h", "--help"):
                with self.subTest(path=path, flag=flag):
                    self.log.unlink(missing_ok=True)
                    result = self.run_cli(*path, flag)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("Usage:", result.stdout)
                    self.assertEqual(self.commands(), [])

    def test_nested_system_help_is_forwarded_without_mutation(self) -> None:
        for flag in ("-h", "--help"):
            with self.subTest(flag=flag):
                self.log.unlink(missing_ok=True)
                result = self.run_cli("system", "start", flag)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    self.exec_commands()[0][-4:], ["iii", "system", "start", flag]
                )

    def test_exec_preserves_help_arguments_for_the_target_command(self) -> None:
        result = self.run_cli("exec", "example-command", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.exec_commands()[0][-2:], ["example-command", "--help"])

    def test_exec_discovers_exact_workspace_container_and_preserves_arguments(
        self,
    ) -> None:
        result = self.run_cli("exec", "printf", "%s", "two words")
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        discovery = next(
            command for command in commands if command and command[0] == "ps"
        )
        self.assertIn(f"label=devcontainer.local_folder={WORKSPACE}", discovery)
        forwarded = self.exec_commands()[0]
        self.assertEqual(forwarded[1:5], ["--user", "iii", "--workdir", "/home/iii/ws"])
        self.assertEqual(forwarded[-3:], ["printf", "%s", "two words"])
        self.assertTrue(
            any(
                'source "${workspace}/setup/setup_dev.bash"' in argument
                for argument in forwarded
            ),
            forwarded,
        )

    def test_missing_or_ambiguous_container_is_rejected(self) -> None:
        no_container = {**self.env, "FAKE_DOCKER_RUNNING": ""}
        result = self.run_cli("system", "status", env=no_container)
        self.assertEqual(result.returncode, 1)
        self.assertIn("No running devcontainer", result.stderr)

        self.log.write_text("", encoding="utf-8")
        ambiguous = {
            **self.env,
            "FAKE_DOCKER_RUNNING": "one\tfirst\ntwo\tsecond",
        }
        result = self.run_cli("system", "status", env=ambiguous)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Expected one", result.stderr)
        self.assertEqual(self.exec_commands(), [])

    def test_simulation_actions_have_explicit_non_attaching_semantics(self) -> None:
        result = self.run_cli("sim", "start", "--headless")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.exec_commands()[0][-2:],
            ["--no-attach", "--headless"],
        )

        self.log.write_text("", encoding="utf-8")
        result = self.run_cli("sim", "restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.exec_commands()[0][-3:],
            [
                str(Path("/home/iii/ws/tools/simulation/launch_simulation_tools.sh")),
                "--recreate",
                "--no-attach",
            ],
        )

        self.log.write_text("", encoding="utf-8")
        result = self.run_cli("sim", "start", "--recreate")
        self.assertEqual(result.returncode, 1)
        self.assertIn("accepts only --headless", result.stderr)
        self.assertEqual(self.exec_commands(), [])

    def test_system_arguments_are_forwarded_to_the_in_container_cli(self) -> None:
        result = self.run_cli("system", "logs", "mission_executor", "--lines", "25")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.exec_commands()[0][-6:],
            ["iii", "system", "logs", "mission_executor", "--lines", "25"],
        )

    def test_rosbag_commands_are_forwarded_to_the_container_helper(self) -> None:
        result = self.run_cli(
            "rosbag", "start", "--id", "field-test", "--topic", "/tf"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.exec_commands()[0][-6:],
            [
                "/home/iii/ws/scripts/workspace/iii_rosbag.sh",
                "start",
                "--id",
                "field-test",
                "--topic",
                "/tf",
            ],
        )

        self.log.write_text("", encoding="utf-8")
        result = self.run_cli("rosbag", "clear", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.exec_commands()[0][-3:],
            [
                "/home/iii/ws/scripts/workspace/iii_rosbag.sh",
                "clear",
                "--force",
            ],
        )

    def test_stack_start_orders_simulation_readiness_boot_and_start(self) -> None:
        env = {
            **self.env,
            "FAKE_DOCKER_EXEC_STDOUT": (
                "tmux_session: running\n"
                "simulation_process_groups: 4321\n"
                "gazebo_transport: available"
            ),
        }
        result = self.run_cli("stack", "start", "--headless", "--no-gui", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        forwarded = self.exec_commands()
        joined = [" ".join(command) for command in forwarded]
        self.assertIn("--no-attach --headless", joined[0])
        self.assertIn("III_SIM_TOOLS_STATUS_DISCOVERY_TIMEOUT_SEC=8", joined[1])
        boot_plan_index = next(
            index
            for index, command in enumerate(joined)
            if "iii system boot --dry-run --operation-id iii-dev-boot-" in command
        )
        boot_apply_index = next(
            index
            for index, command in enumerate(joined)
            if "iii system boot --operation-id iii-dev-boot-" in command
        )
        api_start_index = next(
            index
            for index, command in enumerate(joined)
            if command.endswith("systemctl start iii-runtime-api.service")
        )
        api_health_index = next(
            index
            for index, command in enumerate(joined)
            if "curl --fail --silent" in command
        )
        system_start_plan_index = next(
            index
            for index, command in enumerate(joined)
            if "iii system start --dry-run --operation-id iii-dev-start-" in command
        )
        system_start_apply_index = next(
            index
            for index, command in enumerate(joined)
            if "iii system start --operation-id iii-dev-start-" in command
        )
        self.assertIn("--output=json", joined[boot_plan_index])
        self.assertIn("--confirm --non-interactive --output=json", joined[boot_apply_index])
        self.assertLess(boot_plan_index, boot_apply_index)
        self.assertLess(boot_apply_index, system_start_plan_index)
        self.assertLess(system_start_plan_index, system_start_apply_index)
        self.assertLess(system_start_apply_index, api_start_index)
        self.assertLess(api_start_index, api_health_index)

    def test_stack_stop_retains_and_applies_shutdown_operation(self) -> None:
        result = self.run_cli("stack", "stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        joined = [" ".join(command) for command in self.exec_commands()]
        shutdown = [command for command in joined if "iii system shutdown" in command]
        self.assertEqual(len(shutdown), 2)
        self.assertIn("--dry-run --operation-id iii-dev-shutdown-", shutdown[0])
        self.assertIn("--operation-id iii-dev-shutdown-", shutdown[1])
        self.assertIn("--confirm --non-interactive --output=json", shutdown[1])

    def test_runtime_api_control_is_explicit_and_systemd_scoped(self) -> None:
        result = self.run_cli("api", "restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            " ".join(self.exec_commands()[0]).endswith(
                "sudo -n systemctl restart iii-runtime-api.service"
            )
        )

    def test_simulation_attach_is_attach_only(self) -> None:
        tmux = self.bin_dir / "tmux"
        tmux.write_text(FAKE_TMUX, encoding="utf-8")
        tmux.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "FAKE_COMMAND_LOG": str(self.log),
            "FAKE_TMUX_HAS_SESSION_RC": "0",
            "III_SIM_TOOLS_USER": str(os.getuid()),
            "III_SIM_TOOLS_WORKSPACE_ROOT": str(self.root),
        }
        result = subprocess.run(
            [str(SIM_SCRIPT), "--attach"],
            cwd=WORKSPACE,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        self.assertIn(["tmux", "has-session", "-t", "=iii_sim_tools"], commands)
        self.assertIn(["tmux", "attach", "-t", "=iii_sim_tools"], commands)
        self.assertFalse(any("new-session" in command for command in commands))

        result = subprocess.run(
            [str(SIM_SCRIPT), "--attach", "--status"],
            cwd=WORKSPACE,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot be combined", result.stderr)


if __name__ == "__main__":
    unittest.main()
