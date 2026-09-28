#!/usr/bin/env python3
"""Host-side regression tests for the iii-dev command transport."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import textwrap
import time
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
    running_file = os.environ.get("FAKE_DOCKER_RUNNING_FILE")
    value = (Path(running_file).read_text(encoding="utf-8")
             if running_file and "-a" not in args else os.environ.get(key, ""))
    if value:
        print(value)
    raise SystemExit(0)
if args and args[0] == "stop":
    rc = int(os.environ.get("FAKE_DOCKER_STOP_RC", "0"))
    if rc == 0:
        running_file = os.environ.get("FAKE_DOCKER_RUNNING_FILE")
        if running_file and os.environ.get("FAKE_DOCKER_STOP_STAYS_RUNNING") != "1":
            Path(running_file).write_text("", encoding="utf-8")
        print(args[-1])
    raise SystemExit(rc)
if args and args[0] == "exec":
    if args[-3:] == ["test", "-f", "/run/lock/iii-dev-post-start.ready"]:
        raise SystemExit(0 if os.environ.get("FAKE_POST_START_READY", "1") == "1" else 1)
    if ".devcontainer/post_start.sh" in args:
        raise SystemExit(int(os.environ.get("FAKE_POST_START_RC", "0")))
    if "curl" in " ".join(args) and "/vehicle/status" in " ".join(args):
        print(os.environ.get("FAKE_RUNTIME_VEHICLE_STATUS", json.dumps({
            "freshness": "fresh",
            "latest": {"command_transport": {"connected": True, "command_available": True},
                       "ros_uxrce": {"available": True}},
        })))
        raise SystemExit(0)
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
        self.gc = self.bin_dir / "fake-gc"
        self.gc.write_text(
            "#!/bin/bash\n"
            "if [[ \"$1\" == owned ]]; then exit \"${FAKE_GC_OWNED_RC:-0}\"; fi\n"
            "python3 -c 'import json,os,sys; open(os.environ[\"FAKE_COMMAND_LOG\"],\"a\").write(json.dumps([\"gc\", *sys.argv[1:]])+\"\\n\")' \"$@\"\n"
            "exit 0\n",
            encoding="utf-8",
        )
        self.gc.chmod(0o755)
        self.qgc = self.bin_dir / "fake-iii"
        self.qgc.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "if os.environ.get('PYTHONPATH'): raise SystemExit('PYTHONPATH was not cleared')\n"
            "with open(os.environ['FAKE_COMMAND_LOG'], 'a', encoding='utf-8') as f:\n"
            " f.write(json.dumps(['qgc', *sys.argv[1:], os.environ.get('III_GC_INSTALL_ROOT')]) + '\\n')\n",
            encoding="utf-8",
        )
        self.qgc.chmod(0o755)
        self.native_cli = self.bin_dir / "fake-native-iii"
        self.native_cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "if os.environ.get('PYTHONPATH'): raise SystemExit('PYTHONPATH was not cleared')\n"
            "with open(os.environ['FAKE_COMMAND_LOG'], 'a', encoding='utf-8') as f:\n"
            " f.write(json.dumps(['native-cli', *sys.argv[1:], os.environ.get('III_GC_INSTALL_ROOT')]) + '\\n')\n",
            encoding="utf-8",
        )
        self.native_cli.chmod(0o755)
        self.env = {
            **os.environ,
            "FAKE_COMMAND_LOG": str(self.log),
            "FAKE_DOCKER_RUNNING": "container-123\tiii-dev-test",
            "III_DEV_DOCKER_BIN": str(self.docker),
            "III_DEV_GC_SCRIPT": str(self.gc),
            "III_DEV_QGC_CLI": str(self.qgc),
            "III_DEV_NATIVE_CLI": str(self.native_cli),
            "III_GC_INSTALL_ROOT": str(self.root / "installed-gc"),
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

    def native_commands(self) -> list[list[str]]:
        return [
            command for command in self.commands()
            if command and command[0] == "native-cli"
        ]

    def test_help_does_not_require_docker(self) -> None:
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stack start [options]", result.stdout)
        self.assertNotIn("system <iii arguments>", result.stdout)
        self.assertNotIn("api start|stop", result.stdout)
        self.assertNotIn("gui start|stop", result.stdout)
        self.assertNotIn("rosbag status|list", result.stdout)
        self.assertIn("iii --runtime-target sim system", result.stdout)
        self.assertIn("iii --runtime-target sim api", result.stdout)
        self.assertIn("iii --runtime-target sim rosbag", result.stdout)
        self.assertIn("iii_ground_control.sh", result.stdout)
        self.assertEqual(self.commands(), [])

    def test_every_wrapper_command_and_subcommand_supports_both_help_flags(
        self,
    ) -> None:
        paths = [
            ("container",),
            ("container", "status"),
            ("container", "up"),
            ("container", "down"),
            ("shell",),
            ("exec",),
            ("sim",),
            ("sim", "start"),
            ("sim", "restart"),
            ("sim", "attach"),
            ("sim", "status"),
            ("sim", "stop"),
            ("hil",),
            ("hil", "start"),
            ("hil", "status"),
            ("hil", "logs"),
            ("hil", "stop"),
            ("hil", "restart"),
            ("tmux",),
            ("tmux", "list"),
            ("tmux", "attach"),
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

    def test_removed_top_level_commands_show_native_migration_guidance(self) -> None:
        cases = (
            (("system", "status"), "iii --runtime-target sim system status"),
            (("api", "status"), "iii --runtime-target sim api status"),
            (("rosbag", "status"), "iii --runtime-target sim rosbag status"),
            (("gui", "status"), "installed GC launcher"),
        )
        for arguments, guidance in cases:
            with self.subTest(arguments=arguments):
                self.log.unlink(missing_ok=True)
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertIn("was removed", result.stderr)
                self.assertIn(guidance, result.stderr)
                self.assertEqual(self.commands(), [])

    def test_exec_preserves_help_arguments_for_the_target_command(self) -> None:
        result = self.run_cli("exec", "example-command", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.exec_commands()[0][-2:], ["example-command", "--help"])

    def test_container_up_resumes_incomplete_post_start(self) -> None:
        environment = {**self.env, "FAKE_POST_START_READY": "0"}
        result = self.run_cli("container", "up", env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("completing interrupted post-start setup", result.stdout)
        self.assertTrue(any(
            command[-3:] == ["bash", ".devcontainer/post_start.sh", "--if-needed"]
            for command in self.exec_commands()
        ))

    def test_container_up_reports_failed_post_start_retry(self) -> None:
        environment = {
            **self.env, "FAKE_POST_START_READY": "0", "FAKE_POST_START_RC": "3",
        }
        result = self.run_cli("container", "up", env=environment)
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("Workspace devcontainer: running", result.stdout)

    def test_container_down_stops_only_this_workspace_container(self) -> None:
        running_file = self.root / "running"
        running_file.write_text("container-123\tiii-dev-test", encoding="utf-8")
        environment = {**self.env, "FAKE_DOCKER_RUNNING_FILE": str(running_file)}

        result = self.run_cli("container", "down", env=environment)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Workspace devcontainer stopped.", result.stdout)
        self.assertEqual(running_file.read_text(encoding="utf-8"), "")
        self.assertEqual(
            [command for command in self.commands() if command[0] == "stop"],
            [["stop", "--time", "20", "container-123"]],
        )
        discoveries = [command for command in self.commands() if command[0] == "ps"]
        self.assertTrue(all(
            f"label=devcontainer.local_folder={WORKSPACE}" in command
            for command in discoveries
        ))

    def test_container_down_is_idempotent_and_rejects_ambiguity(self) -> None:
        stopped = self.run_cli("container", "down", env={**self.env, "FAKE_DOCKER_RUNNING": ""})
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertIn("already stopped", stopped.stdout)
        self.assertFalse(any(command[0] == "stop" for command in self.commands()))

        self.log.unlink(missing_ok=True)
        ambiguous = {**self.env, "FAKE_DOCKER_RUNNING": "one\tfirst\ntwo\tsecond"}
        result = self.run_cli("container", "down", env=ambiguous)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Expected one running workspace devcontainer", result.stderr)
        self.assertFalse(any(command[0] == "stop" for command in self.commands()))

    def test_container_down_reports_stop_and_verification_failures(self) -> None:
        running_file = self.root / "running"
        running_file.write_text("container-123\tiii-dev-test", encoding="utf-8")
        base = {**self.env, "FAKE_DOCKER_RUNNING_FILE": str(running_file)}
        failed = self.run_cli("container", "down", env={**base, "FAKE_DOCKER_STOP_RC": "3"})
        self.assertEqual(failed.returncode, 1)
        self.assertIn("Failed to stop workspace devcontainer", failed.stderr)

        self.log.unlink(missing_ok=True)
        still_running = self.run_cli("container", "down", env={**base, "FAKE_DOCKER_STOP_STAYS_RUNNING": "1"})
        self.assertEqual(still_running.returncode, 1)
        self.assertIn("still running after stop", still_running.stderr)

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
        result = self.run_cli("exec", "printf", "x", env=no_container)
        self.assertEqual(result.returncode, 1)
        self.assertIn("No running devcontainer", result.stderr)

        self.log.write_text("", encoding="utf-8")
        ambiguous = {
            **self.env,
            "FAKE_DOCKER_RUNNING": "one\tfirst\ntwo\tsecond",
        }
        result = self.run_cli("exec", "printf", "x", env=ambiguous)
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
        self.assertFalse(any(command[0] == "qgc" for command in self.commands()))

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
        qgc_commands = [command for command in self.commands() if command[0] == "qgc"]
        self.assertEqual(len(qgc_commands), 2)
        self.assertEqual(qgc_commands[0][1:4], ["qgc", "start", "--dry-run"])
        self.assertEqual(qgc_commands[1][1:4], ["qgc", "start", "--operation-id"])
        self.assertEqual(qgc_commands[0][-1], str(Path(self.env["III_GC_INSTALL_ROOT"])))

        self.log.write_text("", encoding="utf-8")
        result = self.run_cli("sim", "start", "--recreate")
        self.assertEqual(result.returncode, 1)
        self.assertIn("accepts only --headless", result.stderr)
        self.assertEqual(self.exec_commands(), [])

    def test_installed_gc_snapshot_and_user_cli_are_default_entrypoints(self) -> None:
        install_root = self.root / "installed-gc"
        installed_gc = install_root / "workspace/scripts/workspace/iii_ground_control.sh"
        installed_gc.parent.mkdir(parents=True)
        installed_gc.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['FAKE_COMMAND_LOG'], 'a', encoding='utf-8') as f:\n"
            " f.write(json.dumps(['installed-gc', *sys.argv[1:]]) + '\\n')\n",
            encoding="utf-8",
        )
        installed_gc.chmod(0o755)
        home = self.root / "home"
        user_cli = home / ".local/bin/iii"
        user_cli.parent.mkdir(parents=True)
        user_cli.write_text(self.qgc.read_text(encoding="utf-8"), encoding="utf-8")
        user_cli.chmod(0o755)
        env = {
            **self.env,
            "HOME": str(home),
            "III_GC_INSTALL_ROOT": str(install_root),
        }
        env.pop("III_DEV_GC_SCRIPT")
        env.pop("III_DEV_QGC_CLI")

        gui = self.run_cli("stack", "status", env=env)
        self.assertEqual(gui.returncode, 0, gui.stderr)
        self.assertIn(["installed-gc", "status"], self.commands())

        self.log.unlink(missing_ok=True)
        sim = self.run_cli("sim", "start", env=env)
        self.assertEqual(sim.returncode, 0, sim.stderr)
        qgc = [command for command in self.commands() if command[0] == "qgc"]
        self.assertEqual(len(qgc), 2)
        self.assertTrue(all(command[-1] == str(install_root) for command in qgc))

    def hil_environment(self):
        coordinator = self.root / "coordinator.py"
        coordinator.write_text(
            "import os,sys,json\n"
            "from pathlib import Path\n"
            "args=sys.argv[1:]\n"
            "with Path(os.environ['FAKE_COMMAND_LOG']).open('a') as f: f.write(json.dumps(['hil',*args])+'\\n')\n"
            "if args and args[0] == 'status':\n"
            " print(json.dumps({'state':os.environ.get('FAKE_STATUS_STATE','ready'),'components':{'pi_runtime':'ready','workstation':'ready','gazebo_viewer':'running','ground_control':'ready'},'viewer_state':'running','render_mode':'rendered','gc_url':'http://127.0.0.1:5174','log_path':os.environ['III_HIL_LOG_PATH']}))\n"
            "sys.exit(int(os.environ.get('FAKE_HIL_RC','0')))\n",
            encoding="utf-8",
        )
        gc = self.root / "gc"
        gc.write_text(
            "#!/bin/bash\n"
            "if [[ \"$1\" == owned ]]; then exit \"${FAKE_GC_OWNED_RC:-0}\"; fi\n"
            "printf '[\"gc\",\"%s\"]\\n' \"$1\" >> \"$FAKE_COMMAND_LOG\"\n"
        )
        gc.chmod(0o755)
        curl = self.bin_dir / "curl"
        curl.write_text("#!/bin/bash\nexit 0\n")
        curl.chmod(0o755)
        binder = self.root / "bind-gc.py"
        binder.write_text(
            "import json,os,sys\n"
            "from pathlib import Path\n"
            "if os.environ.get('FAKE_BIND_CAPTURE'):\n"
            " Path(os.environ['FAKE_BIND_CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
            "raise SystemExit(int(os.environ.get('FAKE_BIND_RC','0')))\n",
            encoding="utf-8",
        )
        return {
            **self.env,
            "III_DEV_HIL_COORDINATOR": str(coordinator),
            "III_DEV_GC_SCRIPT": str(gc),
            "III_DEV_HIL_LOG_DIR": str(self.root / "hil-logs"),
            "III_HIL_PEER_STATE_DIR": str(self.root / "peer-state"),
            "III_HIL_PI_ENDPOINT": "192.0.2.40",
            "III_HIL_PI_ADDRESS": "192.0.2.40",
            "III_HIL_WORKSTATION_ADDRESS": "192.0.2.10",
            "III_DEV_HIL_GC_BIND_SCRIPT": str(binder),
            # Keep persistent-failure wrapper tests bounded; production uses
            # the script's 90-second final-status deadline.
            "III_DEV_HIL_FINAL_STATUS_TIMEOUT_MS": "1000",
            "III_DEV_OPEN_OPERATOR_WINDOWS": "0",
            "PATH": str(self.bin_dir) + ":" + os.environ["PATH"],
        }

    def hil_hanging_final_status_environment(self):
        env = {
            **self.hil_environment(),
            "FAKE_GC_OWNED_RC": "1",
            "FAKE_STATUS_PID_FILE": str(self.root / "status-pid"),
        }
        Path(env["III_DEV_HIL_COORDINATOR"]).write_text(
            textwrap.dedent("""\
                import json, os, sys, time
                from pathlib import Path
                args = sys.argv[1:]
                with Path(os.environ['FAKE_COMMAND_LOG']).open('a') as log:
                    log.write(json.dumps(['hil', *args]) + '\\n')
                if args == ['status', '--json']:
                    Path(os.environ['FAKE_STATUS_PID_FILE']).write_text(str(os.getpid()))
                    time.sleep(30)
                else:
                    print('started')
                """),
            encoding="utf-8",
        )
        return env

    def hil_operator_window_environment(self) -> dict[str, str]:
        env = self.hil_environment()
        env["III_DEV_OPEN_OPERATOR_WINDOWS"] = "1"
        qgc = self.root / "qgc-cli"
        qgc.write_text(
            "#!/bin/bash\n"
            "if [[ -n \"${PYTHONPATH:-}\" ]]; then echo 'QGC CLI inherited workspace PYTHONPATH' >&2; exit 42; fi\n"
            "printf '[\"qgc\",\"%s\",\"%s\"]\\n' \"$2\" \"$3\" >>\"$FAKE_COMMAND_LOG\"\n"
            "if [[ \"$2\" == status ]]; then printf '{\"payload\":{\"selection\":{\"selected\":true},\"unit\":{\"ActiveState\":\"%s\"}}}\\n' \"${FAKE_QGC_STATE:-active}\"; fi\n"
            "exit \"${FAKE_QGC_RC:-0}\"\n",
            encoding="utf-8",
        )
        qgc.chmod(0o755)
        browser = self.root / "browser-window.py"
        browser.write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "with Path(os.environ['FAKE_COMMAND_LOG']).open('a') as f:\n"
            " f.write(json.dumps(['browser-window', sys.argv[1]]) + '\\n')\n"
            "print(os.environ.get('FAKE_BROWSER_RESULT', 'opened'))\n"
            "raise SystemExit(int(os.environ.get('FAKE_BROWSER_RC', '0')))\n",
            encoding="utf-8",
        )
        env["III_DEV_QGC_CLI"] = str(qgc)
        env["III_DEV_BROWSER_WINDOW_SCRIPT"] = str(browser)
        return env

    def test_hil_rendered_start_opens_qgc_and_new_gc_window_after_ready(self):
        env = self.hil_operator_window_environment()
        result = self.run_cli("hil", "start", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        self.assertIn(["qgc", "start", "--dry-run"], commands)
        self.assertIn(["qgc", "start", "--operation-id"], commands)
        self.assertIn(["qgc", "status", "--output=json"], commands)
        self.assertIn(["browser-window", "http://127.0.0.1:5174"], commands)
        self.assertLess(commands.index(["hil", "status", "--json"]), commands.index(["qgc", "start", "--dry-run"]))
        self.assertIn("[done] QGroundControl", result.stdout)
        self.assertIn("[done] Ground-control browser window opened", result.stdout)

        already_open = self.run_cli("hil", "start", env={**env, "FAKE_BROWSER_RESULT": "already-open"})
        self.assertEqual(already_open.returncode, 0, already_open.stderr)
        self.assertIn("[done] Ground-control browser window already open", already_open.stdout)

        self.log.unlink(missing_ok=True)
        headless = self.run_cli("hil", "start", "--headless", env=env)
        self.assertEqual(headless.returncode, 0, headless.stderr)
        self.assertFalse(any(command[0] in {"qgc", "browser-window"} for command in self.commands()))

    def test_hil_operator_window_failure_keeps_ready_core_and_reports_failure(self):
        env = {**self.hil_operator_window_environment(), "FAKE_QGC_RC": "1"}
        result = self.run_cli("hil", "start", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("HIL core is ready", result.stderr)
        self.assertIn(["browser-window", "http://127.0.0.1:5174"], self.commands())
        self.assertNotIn(["gc", "stop"], self.commands())

    def test_hil_qgc_unit_must_be_active_after_cli_accepts_start(self):
        env = {**self.hil_operator_window_environment(), "FAKE_QGC_STATE": "failed"}
        result = self.run_cli("hil", "start", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("[fail] QGroundControl", result.stderr)
        self.assertIn(["browser-window", "http://127.0.0.1:5174"], self.commands())
        self.assertNotIn(["gc", "stop"], self.commands())

    def test_hil_actions_coordinate_pi_workstation_and_separate_gc(self):
        env = self.hil_environment()
        for action in ("start", "restart", "status", "stop"):
            with self.subTest(action=action):
                self.log.unlink(missing_ok=True)
                result = self.run_cli("hil", action, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = [["hil", action]]
                if action in {"start", "restart"}:
                    expected.extend([["gc", "start"], ["hil", "status", "--json"]])
                elif action == "status":
                    expected[0].append("--json")
                elif action == "stop":
                    expected.append(["gc", "stop"])
                self.assertCountEqual(self.commands(), expected)

    def test_hil_start_forwards_headless_only_when_requested(self):
        env = self.hil_environment()
        default = self.run_cli("hil", "start", env=env)
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertIn(["hil", "start"], self.commands())
        self.assertIn("HIL · starting (rendered)", default.stdout)
        self.assertIn("Log:", default.stdout)

        self.log.unlink(missing_ok=True)
        headless = self.run_cli("hil", "start", "--headless", env=env)
        self.assertEqual(headless.returncode, 0, headless.stderr)
        self.assertIn(["hil", "start", "--headless"], self.commands())
        self.assertIn("HIL · starting (headless)", headless.stdout)

    def test_hil_start_streams_structured_progress_before_completion(self):
        env = self.hil_environment()
        coordinator = Path(env["III_DEV_HIL_COORDINATOR"])
        coordinator.write_text(
            "import json,sys,time\n"
            "if sys.argv[1:] == ['status','--json']:\n"
            " print(json.dumps({'state':'ready','components':{'pi_runtime':'ready','workstation':'ready','gazebo_viewer':'running','ground_control':'ready'},'viewer_state':'running','render_mode':'rendered','gc_url':'http://127.0.0.1:5174','log_path':'test.log'}))\n"
            "else:\n"
            " print('III_HIL_PROGRESS|workstation_start|start|Starting workstation PX4 and Gazebo', flush=True)\n"
            " print('III_HIL_PROGRESS|readiness|update|runtime=ready vehicle=waiting checks=waiting workstation=ready', flush=True)\n"
            " print('raw child diagnostic that belongs in the log', flush=True)\n"
            " time.sleep(0.5)\n"
            " print('III_HIL_PROGRESS|workstation_start|done|Workstation PX4 and Gazebo started', flush=True)\n",
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [str(ENTRYPOINT), "hil", "start"],
            cwd=WORKSPACE, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            observed = []
            while True:
                line = process.stdout.readline()
                observed.append(line)
                if "[start] Starting workstation PX4 and Gazebo" in line:
                    break
                self.assertTrue(line, "HIL start ended without stage progress")
            self.assertIsNone(process.poll(), "progress arrived only after completion")
            stdout, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        output = "".join(observed) + stdout
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("[done] Workstation PX4 and Gazebo started", output)
        self.assertIn("[wait] runtime=ready vehicle=waiting checks=waiting workstation=ready", output)
        self.assertNotIn("raw child diagnostic", output)
        self.assertIn("raw child diagnostic", (self.root / "hil-logs" / "latest.log").read_text())

    def test_hil_start_heartbeat_and_failure_do_not_report_false_completion(self):
        env = {**self.hil_environment(), "III_HIL_PROGRESS_HEARTBEAT_SEC": "1"}
        coordinator = Path(env["III_DEV_HIL_COORDINATOR"])
        coordinator.write_text(
            "import sys,time\n"
            "print('III_HIL_PROGRESS|pi_boot|start|Booting Pi HIL runtime', flush=True)\n"
            "time.sleep(1.3)\n"
            "print('failed: simulated Pi boot error', file=sys.stderr, flush=True)\n"
            "raise SystemExit(7)\n",
            encoding="utf-8",
        )
        result = self.run_cli("hil", "start", env=env)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("[start] Booting Pi HIL runtime", result.stdout)
        self.assertIn("still running", result.stdout)
        self.assertNotIn("[done] Pi/workstation lifecycle", result.stdout)
        self.assertIn("simulated Pi boot error", result.stderr)

    def test_hil_start_requires_final_status_state_ready(self):
        for state in ("running", "stopped", "degraded"):
            with self.subTest(state=state):
                env = {**self.hil_environment(), "FAKE_STATUS_STATE": state}
                result = self.run_cli("hil", "start", env=env)
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    f"expected state=ready, got state={state}", result.stderr
                )

        self.log.unlink(missing_ok=True)
        ready = self.run_cli(
            "hil", "start", env={**self.hil_environment(), "FAKE_STATUS_STATE": "ready"}
        )
        self.assertEqual(ready.returncode, 0, ready.stderr)
        self.assertEqual(self.commands().count(["hil", "status", "--json"]), 1)

    def test_hil_start_waits_for_valid_running_to_become_ready_without_restart(self):
        env = {
            **self.hil_environment(),
            "III_DEV_HIL_FINAL_STATUS_TIMEOUT_MS": "1500",
            "FAKE_GC_OWNED_RC": "1",
            "FAKE_STATUS_COUNTER": str(self.root / "status-counter"),
        }
        coordinator = Path(env["III_DEV_HIL_COORDINATOR"])
        coordinator.write_text(
            textwrap.dedent("""\
                import json, os, sys
                from pathlib import Path
                args = sys.argv[1:]
                with Path(os.environ['FAKE_COMMAND_LOG']).open('a') as log:
                    log.write(json.dumps(['hil', *args]) + '\\n')
                if args == ['status', '--json']:
                    counter = Path(os.environ['FAKE_STATUS_COUNTER'])
                    observed = int(counter.read_text()) if counter.exists() else 0
                    counter.write_text(str(observed + 1))
                    state = 'running' if observed == 0 else 'ready'
                    print(json.dumps({'state': state, 'components': {
                        'pi_runtime': 'ready', 'workstation': state,
                        'gazebo_viewer': 'running', 'ground_control': 'ready'},
                        'viewer_state': 'running', 'render_mode': 'rendered',
                        'gc_url': 'http://127.0.0.1:5174',
                        'log_path': os.environ['III_HIL_LOG_PATH']}))
                else:
                    print('started')
                """),
            encoding="utf-8",
        )
        result = self.run_cli("hil", "start", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "status-counter").read_text(), "2")
        self.assertEqual(self.commands().count(["hil", "start"]), 1)
        self.assertEqual(self.commands().count(["hil", "status", "--json"]), 2)
        self.assertNotIn(["gc", "stop"], self.commands())
        self.assertIn("[done] Final HIL health check", result.stdout)

    def test_hil_start_final_status_errors_do_not_retry(self):
        for mode, expected_rc, expected_message in (
            ("invalid_json", 1, "status output was not valid JSON"),
            ("command_error", 7, "status command exited 7"),
        ):
            with self.subTest(mode=mode):
                self.log.unlink(missing_ok=True)
                env = {
                    **self.hil_environment(),
                    "FAKE_STATUS_ERROR_MODE": mode,
                    "FAKE_GC_OWNED_RC": "1",
                }
                coordinator = Path(env["III_DEV_HIL_COORDINATOR"])
                coordinator.write_text(
                    textwrap.dedent("""\
                        import json, os, sys
                        from pathlib import Path
                        args = sys.argv[1:]
                        with Path(os.environ['FAKE_COMMAND_LOG']).open('a') as log:
                            log.write(json.dumps(['hil', *args]) + '\\n')
                        if args == ['status', '--json']:
                            if os.environ['FAKE_STATUS_ERROR_MODE'] == 'invalid_json':
                                print('{invalid-json')
                                raise SystemExit(0)
                            print('simulated status command error')
                            raise SystemExit(7)
                        print('started')
                        """),
                    encoding="utf-8",
                )
                result = self.run_cli("hil", "start", env=env)
                self.assertEqual(result.returncode, expected_rc, result.stderr)
                self.assertIn(expected_message, result.stderr)
                self.assertEqual(self.commands().count(["hil", "status", "--json"]), 1)
                self.assertIn(["gc", "stop"], self.commands())

    def test_hil_start_hung_final_status_is_bounded_and_rolls_back(self):
        env = self.hil_hanging_final_status_environment()
        env["III_DEV_HIL_FINAL_STATUS_TIMEOUT_MS"] = "500"
        started = time.monotonic()
        result = self.run_cli("hil", "start", env=env)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertIn("status command exceeded 500 ms deadline", result.stderr)
        self.assertEqual(self.commands().count(["hil", "status", "--json"]), 1)
        self.assertIn(["gc", "stop"], self.commands())
        status_pid = int(Path(env["FAKE_STATUS_PID_FILE"]).read_text())
        self.assertFalse(Path(f"/proc/{status_pid}").exists())

    def test_hil_final_status_retries_a_hung_probe_within_the_overall_deadline(self):
        env = {
            **self.hil_environment(),
            "III_DEV_HIL_FINAL_STATUS_TIMEOUT_MS": "8000",
            "III_DEV_HIL_FINAL_STATUS_CALL_TIMEOUT_MS": "400",
            "FAKE_GC_OWNED_RC": "1",
            "FAKE_STATUS_COUNTER": str(self.root / "status-counter"),
            "FAKE_STATUS_PID_FILE": str(self.root / "status-pid"),
        }
        Path(env["III_DEV_HIL_COORDINATOR"]).write_text(
            textwrap.dedent("""\
                import json, os, sys, time
                from pathlib import Path
                args = sys.argv[1:]
                with Path(os.environ['FAKE_COMMAND_LOG']).open('a') as log:
                    log.write(json.dumps(['hil', *args]) + '\\n')
                if args == ['status', '--json']:
                    counter = Path(os.environ['FAKE_STATUS_COUNTER'])
                    observed = int(counter.read_text()) if counter.exists() else 0
                    counter.write_text(str(observed + 1))
                    if observed == 0:
                        Path(os.environ['FAKE_STATUS_PID_FILE']).write_text(str(os.getpid()))
                        time.sleep(30)
                    print(json.dumps({'state': 'ready', 'components': {
                        'pi_runtime': 'ready', 'workstation': 'ready',
                        'gazebo_viewer': 'running', 'ground_control': 'ready'},
                        'viewer_state': 'running', 'render_mode': 'rendered',
                        'gc_url': 'http://127.0.0.1:5174',
                        'log_path': os.environ['III_HIL_LOG_PATH']}))
                else:
                    print('started')
                """),
            encoding="utf-8",
        )
        started = time.monotonic()
        result = self.run_cli("hil", "start", env=env)
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands().count(["hil", "status", "--json"]), 2)
        self.assertNotIn(["gc", "stop"], self.commands())
        hung_pid = int(Path(env["FAKE_STATUS_PID_FILE"]).read_text())
        self.assertFalse(Path(f"/proc/{hung_pid}").exists())
        log = (self.root / "hil-logs" / "latest.log").read_text(encoding="utf-8")
        self.assertIn("exceeded 400 ms call timeout; retrying", log)

    def test_hil_final_status_deadlines_are_validated_and_configurable(self):
        for key, value, message in (
            ("III_DEV_HIL_FINAL_STATUS_CALL_TIMEOUT_MS", "0", "invalid final status call timeout 0"),
            ("III_DEV_HIL_FINAL_STATUS_CALL_TIMEOUT_MS", "soon", "invalid final status call timeout soon"),
            ("III_DEV_HIL_FINAL_STATUS_TIMEOUT_MS", "600001", "invalid final status deadline 600001"),
        ):
            with self.subTest(key=key, value=value):
                self.log.unlink(missing_ok=True)
                env = {**self.hil_environment(), key: value, "FAKE_GC_OWNED_RC": "1"}
                result = self.run_cli("hil", "start", env=env)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertIn(["gc", "stop"], self.commands())
        # Deadlines beyond the former 30 s hard cap are accepted.
        self.log.unlink(missing_ok=True)
        ready = self.run_cli("hil", "start", env={
            **self.hil_environment(), "III_DEV_HIL_FINAL_STATUS_TIMEOUT_MS": "120000",
        })
        self.assertEqual(ready.returncode, 0, ready.stderr)

    def test_hil_final_status_clock_does_not_spawn_python_per_sample(self):
        source = (WORKSPACE / "scripts/workspace/iii_dev.sh").read_text(encoding="utf-8")
        gate = source.split("hil_capture_status_json() {", 1)[1].split("\nhil_print_status_json() {", 1)[0]
        self.assertNotIn("monotonic_ns", gate)
        self.assertIn("hil_now_ns now_ns", gate)

    def test_hil_start_signal_during_final_status_reaps_probe_and_rolls_back(self):
        env = self.hil_hanging_final_status_environment()
        env["III_DEV_HIL_FINAL_STATUS_TIMEOUT_MS"] = "5000"
        process = subprocess.Popen(
            [str(ENTRYPOINT), "hil", "start"], cwd=WORKSPACE, env=env,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        marker = Path(env["FAKE_STATUS_PID_FILE"])
        try:
            deadline = time.monotonic() + 4
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "final status probe was not entered")
            status_pid = int(marker.read_text())
            process.send_signal(signal.SIGTERM)
            _, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 143, stderr)
            self.assertIn("HIL interrupted by SIGTERM", stderr)
            self.assertIn(["gc", "stop"], self.commands())
            self.assertFalse(Path(f"/proc/{status_pid}").exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    def test_hil_start_signal_reaps_both_children_and_rolls_back_owned_gc(self):
        env = self.hil_environment()
        markers = self.root / "signal-markers"
        coordinator = Path(env["III_DEV_HIL_COORDINATOR"])
        coordinator.write_text(
            "import json,os,signal,sys,time\n"
            "from pathlib import Path\n"
            "marker=Path(os.environ['SIGNAL_MARKERS'])\n"
            "def term(_sig,_frame): marker.open('a').write('coordinator-term\\n'); raise SystemExit(143)\n"
            "signal.signal(signal.SIGTERM,term)\n"
            "if sys.argv[1:] == ['status','--json']:\n"
            " print(json.dumps({'state':'ready','components':{'pi_runtime':'ready','workstation':'ready','gazebo_viewer':'running','ground_control':'ready'},'viewer_state':'running','render_mode':'rendered','gc_url':'http://127.0.0.1:5174','log_path':os.environ['III_HIL_LOG_PATH']})); raise SystemExit(0)\n"
            "marker.open('a').write('coordinator-start\\n')\n"
            "while True: time.sleep(1)\n",
            encoding="utf-8",
        )
        gc = Path(env["III_DEV_GC_SCRIPT"])
        gc.write_text(
            "#!/bin/bash\n"
            "if [[ \"$1\" == owned ]]; then exit 1; fi\n"
            "if [[ \"$1\" == stop ]]; then echo gc-stop >> \"$SIGNAL_MARKERS\"; exit 0; fi\n"
            "echo gc-start >> \"$SIGNAL_MARKERS\"\n"
            "trap 'echo gc-term >> \"$SIGNAL_MARKERS\"; exit 143' TERM\n"
            "while :; do /bin/sleep 1; done\n",
            encoding="utf-8",
        )
        gc.chmod(0o755)
        env["SIGNAL_MARKERS"] = str(markers)
        process = subprocess.Popen(
            [str(ENTRYPOINT), "hil", "start"],
            cwd=WORKSPACE,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if markers.exists() and {
                "coordinator-start",
                "gc-start",
            }.issubset(set(markers.read_text().splitlines())):
                break
            time.sleep(0.02)
        else:
            process.kill()
            process.communicate(timeout=2)
            self.fail("HIL children did not start")
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 143, (stdout, stderr))
        marker_text = markers.read_text(encoding="utf-8")
        self.assertIn("coordinator-term", marker_text)
        self.assertIn("gc-term", marker_text)
        self.assertIn("gc-stop", marker_text)

    def test_signal_cleanup_does_not_target_reaped_child_and_kills_live_peer(self):
        env = self.hil_environment()
        markers = self.root / "reaped-signal-markers"
        coordinator = Path(env["III_DEV_HIL_COORDINATOR"])
        coordinator.write_text(
            "import json,os,signal,sys\n"
            "from pathlib import Path\n"
            "marker=Path(os.environ['SIGNAL_MARKERS'])\n"
            "def term(_sig,_frame): marker.open('a').write('coordinator-term\\n'); raise SystemExit(143)\n"
            "signal.signal(signal.SIGTERM,term)\n"
            "if sys.argv[1:] == ['status','--json']:\n"
            " print(json.dumps({'state':'ready','components':{'pi_runtime':'ready','workstation':'ready','gazebo_viewer':'running','ground_control':'ready'},'viewer_state':'running','render_mode':'rendered','gc_url':'http://127.0.0.1:5174','log_path':os.environ['III_HIL_LOG_PATH']})); raise SystemExit(0)\n"
            "marker.open('a').write('coordinator-exited\\n')\n",
            encoding="utf-8",
        )
        gc = Path(env["III_DEV_GC_SCRIPT"])
        gc.write_text(
            "#!/bin/bash\n"
            "if [[ \"$1\" == owned ]]; then exit 1; fi\n"
            "if [[ \"$1\" == stop ]]; then echo gc-stop >> \"$SIGNAL_MARKERS\"; exit 0; fi\n"
            "echo gc-start >> \"$SIGNAL_MARKERS\"\n"
            "trap 'echo gc-term >> \"$SIGNAL_MARKERS\"; exit 143' TERM\n"
            "while :; do /bin/sleep 1; done\n",
            encoding="utf-8",
        )
        gc.chmod(0o755)
        env["SIGNAL_MARKERS"] = str(markers)
        process = subprocess.Popen(
            [str(ENTRYPOINT), "hil", "start"],
            cwd=WORKSPACE,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if markers.exists() and {
                "coordinator-exited",
                "gc-start",
            }.issubset(set(markers.read_text().splitlines())):
                break
            time.sleep(0.02)
        else:
            process.kill()
            process.communicate(timeout=2)
            self.fail("HIL children did not reach the reaped-peer state")
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 143, (stdout, stderr))
        marker_text = markers.read_text(encoding="utf-8")
        self.assertIn("gc-term", marker_text)
        self.assertIn("gc-stop", marker_text)
        self.assertNotIn("coordinator-term", marker_text)

    def test_hil_start_overlaps_coordinator_and_ground_control(self):
        env = self.hil_environment()
        timing_log = self.root / "timing.jsonl"
        env["FAKE_TIMING_LOG"] = str(timing_log)
        coordinator = Path(env["III_DEV_HIL_COORDINATOR"])
        coordinator.write_text(
            "import json,os,sys,time\n"
            "from pathlib import Path\n"
            "if sys.argv[1:] != ['status', '--json']:\n"
            " with Path(os.environ['FAKE_TIMING_LOG']).open('a') as f: f.write(json.dumps(['coordinator','start',time.monotonic()])+'\\n')\n"
            " time.sleep(0.4)\n"
            " with Path(os.environ['FAKE_TIMING_LOG']).open('a') as f: f.write(json.dumps(['coordinator','end',time.monotonic()])+'\\n')\n"
            "with Path(os.environ['FAKE_COMMAND_LOG']).open('a') as f: f.write(json.dumps(['hil', *sys.argv[1:]]) + '\\n')\n"
            "if sys.argv[1:] == ['status', '--json']:\n"
            " print(json.dumps({'state':'ready','components':{'pi_runtime':'ready','workstation':'ready','gazebo_viewer':'running','ground_control':'ready'},'viewer_state':'running','render_mode':'rendered','gc_url':'http://127.0.0.1:5174','log_path':os.environ['III_HIL_LOG_PATH']}))\n",
            encoding="utf-8",
        )
        gc = Path(env["III_DEV_GC_SCRIPT"])
        gc.write_text(
            "#!/bin/bash\n"
            "if [[ \"$1\" == owned ]]; then exit 1; fi\n"
            "python3 -c 'import json,os,time; open(os.environ[\"FAKE_TIMING_LOG\"],\"a\").write(json.dumps([\"ground-control\",\"start\",time.monotonic()])+\"\\n\")'\n"
            "sleep 0.4\n"
            "python3 -c 'import json,os,time; open(os.environ[\"FAKE_TIMING_LOG\"],\"a\").write(json.dumps([\"ground-control\",\"end\",time.monotonic()])+\"\\n\")'\n"
            "printf '[\"gc\",\"%s\"]\\n' \"$1\" >> \"$FAKE_COMMAND_LOG\"\n",
            encoding="utf-8",
        )
        gc.chmod(0o755)
        result = self.run_cli("hil", "start", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in timing_log.read_text().splitlines()]
        intervals = {
            name: {phase: timestamp for owner, phase, timestamp in events if owner == name}
            for name in ("coordinator", "ground-control")
        }
        self.assertLess(intervals["coordinator"]["start"], intervals["ground-control"]["end"])
        self.assertLess(intervals["ground-control"]["start"], intervals["coordinator"]["end"])
        self.assertCountEqual(
            self.commands(),
            [["hil", "start"], ["gc", "start"], ["hil", "status", "--json"]],
        )

    def test_hil_gate_waits_for_delayed_identity_publication(self):
        env = {**self.hil_environment(), "III_HIL_GATE_PUBLISH_DELAY_SEC": "0.1"}
        result = self.run_cli("hil", "start", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["hil", "start"], self.commands())
        self.assertIn(["gc", "start"], self.commands())

    def test_hil_gate_timeout_never_executes_payload_or_launches_gc(self):
        env = {
            **self.hil_environment(),
            "III_HIL_GATE_PUBLISH_DELAY_SEC": "0.1",
            "III_HIL_GATE_TIMEOUT_MS": "10",
        }
        marker = self.root / "payload-ran"
        coordinator = Path(env["III_DEV_HIL_COORDINATOR"])
        coordinator.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        result = self.run_cli("hil", "start", env=env)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(marker.exists())
        self.assertNotIn(["gc", "start"], self.commands())
        self.assertIn("coordinator startup handshake", result.stderr)

    def test_hil_sigterm_during_unreleased_gate_reaps_gate_and_rolls_back(self):
        env = {
            **self.hil_environment(),
            "III_HIL_GATE_PUBLISH_DELAY_SEC": "1",
            "FAKE_GC_OWNED_RC": "1",
        }
        payload_marker = self.root / "sigterm-gate-payload-ran"
        rollback_marker = self.root / "sigterm-gate-rollback"
        coordinator = Path(env["III_DEV_HIL_COORDINATOR"])
        coordinator.write_text(
            "from pathlib import Path\n"
            f"Path({str(payload_marker)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        gc = Path(env["III_DEV_GC_SCRIPT"])
        gc.write_text(
            "#!/bin/bash\n"
            "if [[ \"$1\" == owned ]]; then exit 1; fi\n"
            f"if [[ \"$1\" == stop ]]; then printf stop > {str(rollback_marker)!r}; exit 0; fi\n"
            "printf start >> \"$FAKE_COMMAND_LOG\"\n"
            "exit 0\n",
            encoding="utf-8",
        )
        gc.chmod(0o755)
        process = subprocess.Popen(
            [str(ENTRYPOINT), "hil", "start"],
            cwd=WORKSPACE,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertIn("HIL · starting", process.stdout.readline())
        time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)

        self.assertEqual(process.returncode, 143, (stdout, stderr))
        self.assertFalse(payload_marker.exists())
        self.assertTrue(rollback_marker.exists())
        self.assertNotIn(["gc", "start"], self.commands())
        orphan_pids = []
        for proc_dir in Path("/proc").glob("[0-9]*"):
            try:
                command_line = (proc_dir / "cmdline").read_bytes()
            except OSError:
                continue
            if str(self.root).encode() in command_line:
                orphan_pids.append(proc_dir.name)
        self.assertEqual(orphan_pids, [])

    def test_hil_start_rolls_back_partial_gc_when_coordinator_fails(self):
        env = {
            **self.hil_environment(),
            "FAKE_HIL_RC": "1",
            "FAKE_CURL_RC": "1",
            "FAKE_GC_OWNED_RC": "1",
        }
        curl = self.bin_dir / "curl"
        curl.write_text("#!/bin/bash\nexit \"${FAKE_CURL_RC:-0}\"\n", encoding="utf-8")
        curl.chmod(0o755)
        gc = Path(env["III_DEV_GC_SCRIPT"])
        gc.write_text(
            "#!/bin/bash\n"
            "printf '[\"gc\",\"%s\"]\\n' \"$1\" >> \"$FAKE_COMMAND_LOG\"\n"
            "exit $([[ \"$1\" == stop ]] && echo 0 || echo 1)\n",
            encoding="utf-8",
        )
        gc.chmod(0o755)
        result = self.run_cli("hil", "start", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn(["gc", "start"], self.commands())
        self.assertIn(["gc", "stop"], self.commands())
        self.assertIn("Pi/workstation lifecycle", result.stderr)

    def test_hil_start_reports_launcher_reason_before_generic_child_failure(self):
        env = self.hil_environment()
        coordinator = Path(env["III_DEV_HIL_COORDINATOR"])
        coordinator.write_text(
            "import sys\n"
            "print('The III workspace devcontainer is not running; HIL cannot start safely.')\n"
            "print(\"Coordinated HIL start failed: Command '['launcher', 'stop']' returned non-zero exit status 1.\", file=sys.stderr)\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        result = self.run_cli("hil", "start", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "Reason: The III workspace devcontainer is not running; HIL cannot start safely.",
            result.stderr,
        )

    def test_hil_start_rolls_back_when_gc_child_fails_after_start(self):
        env = self.hil_environment()
        gc = Path(env["III_DEV_GC_SCRIPT"])
        gc.write_text(
            "#!/bin/bash\n"
            "if [[ \"$1\" == owned ]]; then exit 1; fi\n"
            "printf '[\"gc\",\"%s\"]\\n' \"$1\" >> \"$FAKE_COMMAND_LOG\"\n"
            "[[ \"$1\" == stop ]] && exit 0\n"
            "echo 'Ground-control proxy port is occupied.' >&2\n"
            "exit 7\n",
            encoding="utf-8",
        )
        gc.chmod(0o755)
        result = self.run_cli("hil", "start", env=env)
        self.assertEqual(result.returncode, 7)
        self.assertIn("failed at ground control", result.stderr)
        self.assertIn("Reason: Ground-control proxy port is occupied.", result.stderr)
        self.assertIn(["gc", "stop"], self.commands())

    def test_hil_start_final_state_failure_rolls_back_absent_project(self):
        env = {
            **self.hil_environment(),
            "FAKE_STATUS_STATE": "running",
            "FAKE_GC_OWNED_RC": "1",
            "III_DEV_HIL_FINAL_STATUS_TIMEOUT_MS": "2500",
        }
        result = self.run_cli("hil", "start", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("expected state=ready, got state=running", result.stderr)
        self.assertIn(["gc", "stop"], self.commands())
        self.assertGreaterEqual(self.commands().count(["hil", "status", "--json"]), 2)
        self.assertEqual(self.commands().count(["hil", "start"]), 1)

    def test_hil_final_failure_does_not_reinspect_reaped_child_pids(self):
        env = {
            **self.hil_environment(),
            "FAKE_STATUS_STATE": "running",
            "FAKE_GC_OWNED_RC": "1",
        }
        ps_log = self.root / "ps-calls"
        ps = self.bin_dir / "ps"
        ps.write_text(
            "#!/bin/bash\n"
            "printf '%s\\n' \"$*\" >> \"$PS_LOG\"\n"
            "exec /usr/bin/ps \"$@\"\n",
            encoding="utf-8",
        )
        ps.chmod(0o755)
        env["PS_LOG"] = str(ps_log)
        result = self.run_cli("hil", "start", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(ps_log.exists())

    def test_hil_start_retains_gc_lock_through_final_status_validation(self):
        env = {**self.hil_environment(), "FAKE_GC_OWNED_RC": "1"}
        marker = self.root / "status-lock"
        coordinator = Path(env["III_DEV_HIL_COORDINATOR"])
        coordinator.write_text(
            "import fcntl,json,os,sys\n"
            "from pathlib import Path\n"
            "if sys.argv[1:] == ['status','--json']:\n"
            " fd=open(os.environ['III_GC_LOCK_PATH'],'r+')\n"
            " try:\n"
            "  fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); Path(os.environ['STATUS_LOCK_MARKER']).write_text('released')\n"
            " except BlockingIOError:\n"
            "  Path(os.environ['STATUS_LOCK_MARKER']).write_text('held')\n"
            " print(json.dumps({'state':'ready','components':{'pi_runtime':'ready','workstation':'ready','gazebo_viewer':'running','ground_control':'ready'},'viewer_state':'running','render_mode':'rendered','gc_url':'http://127.0.0.1:5174','log_path':os.environ['III_HIL_LOG_PATH']}))\n"
            "else:\n"
            " print('started')\n",
            encoding="utf-8",
        )
        env["STATUS_LOCK_MARKER"] = str(marker)
        result = self.run_cli("hil", "start", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "held")

    def test_hil_start_gc_and_final_failures_preserve_preexisting_project(self):
        for failure in ("gc", "final"):
            with self.subTest(failure=failure):
                env = {
                    **self.hil_environment(),
                    "FAKE_GC_OWNED_RC": "0",
                    **({"FAKE_STATUS_STATE": "degraded"} if failure == "final" else {}),
                }
                if failure == "gc":
                    gc = Path(env["III_DEV_GC_SCRIPT"])
                    gc.write_text(
                        "#!/bin/bash\n"
                        "if [[ \"$1\" == owned ]]; then exit 0; fi\n"
                        "printf '[\"gc\",\"%s\"]\\n' \"$1\" >> \"$FAKE_COMMAND_LOG\"\n"
                        "exit 9\n",
                        encoding="utf-8",
                    )
                    gc.chmod(0o755)
                result = self.run_cli("hil", "start", env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(["gc", "stop"], self.commands())

    def test_hil_start_does_not_stop_preexisting_healthy_gc(self):
        env = {
            **self.hil_environment(),
            "FAKE_HIL_RC": "1",
            "FAKE_CURL_RC": "1",
            "FAKE_GC_OWNED_RC": "0",
        }
        curl = self.bin_dir / "curl"
        curl.write_text("#!/bin/bash\nexit \"${FAKE_CURL_RC:-0}\"\n", encoding="utf-8")
        curl.chmod(0o755)
        result = self.run_cli("hil", "start", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn(["gc", "start"], self.commands())
        self.assertNotIn(["gc", "stop"], self.commands())

    def test_hil_start_does_not_stop_degraded_or_stopped_preexisting_project(self):
        env = {
            **self.hil_environment(),
            "FAKE_HIL_RC": "1",
            "FAKE_CURL_RC": "1",
            "FAKE_GC_OWNED_RC": "0",
        }
        result = self.run_cli("hil", "start", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(["gc", "stop"], self.commands())

    def test_hil_status_json_and_logs_surface_are_structured(self):
        env = self.hil_environment()
        started = self.run_cli("hil", "start", env=env)
        self.assertEqual(started.returncode, 0, started.stderr)

        self.log.unlink(missing_ok=True)
        status = self.run_cli("hil", "status", "--json", env=env)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["state"], "ready")
        self.assertEqual(self.commands(), [["hil", "status", "--json"]])

        logs = self.run_cli("hil", "logs", env=env)
        self.assertEqual(logs.returncode, 0, logs.stderr)
        self.assertIn('"state": "ready"', logs.stdout)

    def test_hil_status_host_argument_overrides_inherited_targets(self):
        env = self.hil_environment()
        capture = self.root / "hil-host.json"
        coordinator = Path(env["III_DEV_HIL_COORDINATOR"])
        coordinator.write_text(
            "import json,os,sys\n"
            "from pathlib import Path\n"
            "keys=['III_HIL_PI_ENDPOINT','III_HIL_PI_ADDRESS','III_SSH_HOST','III_RUNTIME_HOST','III_RUNTIME_API_HOST','III_RUNTIME_API_URL']\n"
            "Path(os.environ['FAKE_HIL_HOST_CAPTURE']).write_text(json.dumps({'args':sys.argv[1:],'env':{k:os.environ.get(k) for k in keys}}))\n"
            "print(json.dumps({'state':'ready','components':{'pi_runtime':'ready','workstation':'ready','gazebo_viewer':'running','ground_control':'ready'},'viewer_state':'running','render_mode':'rendered','gc_url':'http://127.0.0.1:5174','log_path':'test'}))\n",
            encoding="utf-8",
        )
        env.update({
            "FAKE_HIL_HOST_CAPTURE": str(capture),
            "III_HIL_PI_ADDRESS": "10.42.0.15",
            "III_HIL_PI_ENDPOINT": "old.local",
            "III_SSH_HOST": "old.local",
            "III_RUNTIME_API_URL": "http://old.local:8765",
        })
        result = self.run_cli("hil", "status", "--json", "--host", "alternate.local", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        selected = json.loads(capture.read_text())
        self.assertEqual(selected["args"], ["status", "--json", "--host", "alternate.local"])
        self.assertEqual(selected["env"], {
            "III_HIL_PI_ENDPOINT": "alternate.local",
            "III_HIL_PI_ADDRESS": "",
            "III_SSH_HOST": "alternate.local",
            "III_RUNTIME_HOST": "alternate.local",
            "III_RUNTIME_API_HOST": "alternate.local",
            "III_RUNTIME_API_URL": "http://alternate.local:8765",
        })

    def test_hil_start_and_stop_forward_host_argument(self):
        env = self.hil_environment()
        bind_capture = self.root / "gc-bind.json"
        env["FAKE_BIND_CAPTURE"] = str(bind_capture)
        started = self.run_cli("hil", "start", "--host", "alternate.local", env=env)
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertIn(["hil", "start", "--host", "alternate.local"], self.commands())
        self.assertEqual(json.loads(bind_capture.read_text()), [
            "--proxy-url", "http://127.0.0.1:8781",
            "--runtime-url", "http://alternate.local:8765",
        ])
        self.log.unlink(missing_ok=True)
        stopped = self.run_cli("hil", "stop", "--host", "alternate.local", env=env)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertIn(["hil", "stop", "--host", "alternate.local"], self.commands())

    def hil_stop_environment(self, *, coordinator_rc=0, gc_rc=0, components=""):
        env = self.hil_environment()
        Path(env["III_DEV_HIL_COORDINATOR"]).write_text(
            "import json,os,sys\n"
            "from pathlib import Path\n"
            "with Path(os.environ['FAKE_COMMAND_LOG']).open('a') as f: f.write(json.dumps(['hil',*sys.argv[1:]])+'\\n')\n"
            f"sys.stdout.write({components!r})\n"
            f"if {coordinator_rc}: print('Coordinated HIL stop failed: Pi runtime unreachable (identity unavailable: No route to host)', file=sys.stderr)\n"
            f"sys.exit({coordinator_rc})\n",
            encoding="utf-8",
        )
        gc = self.root / "gc-stop"
        gc.write_text(
            "#!/bin/bash\n"
            "if [[ \"$1\" == owned ]]; then exit 0; fi\n"
            "printf '[\"gc\",\"%s\"]\\n' \"$1\" >> \"$FAKE_COMMAND_LOG\"\n"
            f"if (({gc_rc})); then echo 'ground-control compose stop failed: daemon unavailable' >&2; fi\n"
            f"exit {gc_rc}\n",
            encoding="utf-8",
        )
        gc.chmod(0o755)
        env["III_DEV_GC_SCRIPT"] = str(gc)
        return env

    def test_hil_stop_success_output_is_unchanged(self):
        result = self.run_cli("hil", "stop", env=self.hil_stop_environment(
            components="III_HIL_STOP|workstation|stopped|\nIII_HIL_STOP|pi_runtime|stopped|\n",
        ))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "✓ Pi runtime and workstation simulation stopped\n✓ Ground control stopped\n\nHIL stopped\n",
            result.stdout,
        )
        self.assertNotIn("III_HIL_STOP", result.stdout)
        self.assertEqual(self.commands(), [["hil", "stop"], ["gc", "stop"]])

    def test_hil_stop_with_unreachable_pi_still_stops_ground_control(self):
        env = self.hil_stop_environment(
            coordinator_rc=1,
            components=(
                "III_HIL_STOP|pi_runtime|unreachable|identity unavailable: No route to host\n"
                "III_HIL_STOP|workstation|stopped|launcher-verified; Pi-side endpoint proof unavailable\n"
            ),
        )
        result = self.run_cli("hil", "stop", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.commands(), [["hil", "stop"], ["gc", "stop"]])
        self.assertIn("✗ Pi runtime: unreachable (identity unavailable: No route to host)", result.stdout)
        self.assertIn("✓ Workstation simulation: stopped (launcher-verified", result.stdout)
        self.assertIn("✓ Ground control stopped", result.stdout)
        self.assertNotIn("✓ Pi runtime and workstation simulation stopped", result.stdout)
        self.assertIn("HIL stop failed at Pi/workstation lifecycle", result.stderr)
        self.assertIn("Pi runtime unreachable", result.stderr)
        self.assertIn("HIL stop incomplete", result.stderr)
        self.assertNotIn("HIL stopped\n", result.stdout)

    def test_hil_stop_reports_ground_control_failure_after_core_stop(self):
        result = self.run_cli("hil", "stop", env=self.hil_stop_environment(gc_rc=3))
        self.assertEqual(result.returncode, 1)
        self.assertIn("✓ Pi runtime and workstation simulation stopped", result.stdout)
        self.assertIn("✗ Ground control: stop failed", result.stdout)
        self.assertIn("HIL stop failed at ground control", result.stderr)
        self.assertIn("compose stop failed", result.stderr)
        self.assertIn("HIL stop incomplete", result.stderr)

    def test_hil_stop_attempts_every_owner_when_both_fail(self):
        result = self.run_cli(
            "hil", "stop", env=self.hil_stop_environment(coordinator_rc=1, gc_rc=1)
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.commands(), [["hil", "stop"], ["gc", "stop"]])
        self.assertIn("HIL stop failed at Pi/workstation lifecycle", result.stderr)
        self.assertIn("HIL stop failed at ground control", result.stderr)

    def test_hil_start_fails_if_ground_control_selects_another_runtime(self):
        env = {**self.hil_environment(), "FAKE_BIND_RC": "1", "FAKE_GC_OWNED_RC": "1"}
        result = self.run_cli("hil", "start", "--host", "alternate.local", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ground-control runtime selection", result.stderr)
        self.assertIn(["gc", "stop"], self.commands())

    def test_hil_status_prints_valid_degraded_output_and_preserves_exit_code(self):
        env = self.hil_environment()
        coordinator = Path(env["III_DEV_HIL_COORDINATOR"])
        coordinator.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "print(json.dumps({\n"
            "  'state': 'degraded',\n"
            "  'components': {\n"
            "    'pi_runtime': 'unknown',\n"
            "    'workstation': 'ready',\n"
            "    'gazebo_viewer': 'running',\n"
            "    'ground_control': 'ready'\n"
            "  },\n"
            "  'viewer_state': 'running',\n"
            "  'render_mode': 'rendered',\n"
            "  'gc_url': 'http://127.0.0.1:5174',\n"
            "  'log_path': 'runtime_logs/hil/latest.log',\n"
            "  'diagnostics': {'pi_runtime': ['identity unavailable: refused']}\n"
            "}))\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        coordinator.chmod(0o755)

        text_result = self.run_cli("hil", "status", env=env)
        self.assertEqual(text_result.returncode, 1)
        self.assertIn("HIL · degraded", text_result.stdout)
        self.assertIn("Pi runtime:      unknown", text_result.stdout)
        self.assertIn("Pi detail:       identity unavailable: refused", text_result.stdout)

        json_result = self.run_cli("hil", "status", "--json", env=env)
        self.assertEqual(json_result.returncode, 1)
        status = json.loads(json_result.stdout)
        self.assertEqual(status["state"], "degraded")
        self.assertEqual(status["components"]["pi_runtime"], "unknown")
        self.assertEqual(
            status["diagnostics"]["pi_runtime"],
            ["identity unavailable: refused"],
        )

    def test_failed_hil_transition_leaves_gc_available(self):
        env = {**self.hil_environment(), "FAKE_HIL_RC": "1"}
        for action in ("start", "restart", "stop"):
            with self.subTest(action=action):
                self.log.unlink(missing_ok=True)
                result = self.run_cli("hil", action, env=env)
                self.assertEqual(result.returncode, 1)
                commands = self.commands()
                self.assertIn(["hil", action], commands)
                if action in {"start", "restart"}:
                    self.assertIn(["gc", "start"], commands)
                self.assertIn("failed at Pi/workstation lifecycle", result.stderr)
                self.assertIn("./iii-dev hil status", result.stderr)

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
        docker_joined = [" ".join(command) for command in forwarded]
        native = [command[1:-1] for command in self.native_commands()]
        joined = [" ".join(command) for command in native]
        self.assertTrue(any("--no-attach --headless" in command for command in docker_joined))
        self.assertTrue(any("III_SIM_TOOLS_STATUS_DISCOVERY_TIMEOUT_SEC=8" in command for command in docker_joined))
        boot_plan_index = next(
            index
            for index, command in enumerate(joined)
            if "--runtime-target sim system boot --dry-run --operation-id iii-dev-boot-" in command
        )
        boot_apply_index = next(
            index
            for index, command in enumerate(joined)
            if "--runtime-target sim system boot --operation-id iii-dev-boot-" in command
        )
        api_start_index = next(
            index
            for index, command in enumerate(joined)
            if "--runtime-target sim api start --dry-run --operation-id iii-dev-api-start-" in command
        )
        system_start_plan_index = next(
            index
            for index, command in enumerate(joined)
            if "--runtime-target sim system start --dry-run --operation-id iii-dev-start-" in command
        )
        system_start_apply_index = next(
            index
            for index, command in enumerate(joined)
            if "--runtime-target sim system start --operation-id iii-dev-start-" in command
        )
        api_start_apply_index = next(
            index
            for index, command in enumerate(joined)
            if "--runtime-target sim api start --operation-id iii-dev-api-start-" in command
        )
        api_health_index = next(index for index, command in enumerate(self.commands()) if command[0] == "exec" and "curl --fail --silent" in " ".join(command))
        api_apply_command = self.native_commands()[api_start_apply_index]
        api_apply_index = self.commands().index(api_apply_command)
        self.assertIn("--output=json", joined[boot_plan_index])
        self.assertIn("--confirm --non-interactive --output=json", joined[boot_apply_index])
        self.assertLess(boot_plan_index, boot_apply_index)
        self.assertLess(boot_apply_index, system_start_plan_index)
        self.assertLess(system_start_plan_index, system_start_apply_index)
        self.assertLess(system_start_apply_index, api_start_index)
        self.assertLess(api_start_index, api_start_apply_index)
        self.assertIn("--confirm --non-interactive --output=json", joined[api_start_apply_index])
        self.assertLess(api_apply_index, api_health_index)

    def test_stack_start_rejects_api_health_without_fresh_command_transport(self) -> None:
        for freshness, connected in (("stale", False), ("stale", True), ("fresh", False)):
            with self.subTest(freshness=freshness, connected=connected):
                env = {
                    **self.env,
                    "FAKE_DOCKER_EXEC_STDOUT": (
                        "tmux_session: running\nsimulation_process_groups: 4321\n"
                        "gazebo_transport: available"
                    ),
                    "III_DEV_RUNTIME_API_READY_TIMEOUT_SEC": "1",
                    "FAKE_RUNTIME_VEHICLE_STATUS": json.dumps({
                        "freshness": freshness,
                        "degraded_reason": "connecting to PX4 MAVSDK command transport",
                        "latest": {"command_transport": {
                            "connected": connected, "command_available": connected,
                        }, "ros_uxrce": {"available": True}},
                    }),
                }
                result = self.run_cli("stack", "start", "--headless", "--no-gui", env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("connecting to PX4 MAVSDK command transport", result.stderr)
                self.assertNotIn("== Ready ==", result.stdout)

    def test_stack_start_uses_installed_gc_launcher_when_gui_is_enabled(self) -> None:
        env = {
            **self.env,
            "FAKE_DOCKER_EXEC_STDOUT": (
                "tmux_session: running\n"
                "simulation_process_groups: 4321\n"
                "gazebo_transport: available"
            ),
        }
        result = self.run_cli("stack", "start", "--headless", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["gc", "start"], self.commands())
        self.assertIn(
            ["--runtime-target", "sim", "api", "start", "--dry-run"],
            [command[1:6] for command in self.native_commands()],
        )

    def test_stack_stop_retains_and_applies_shutdown_operation(self) -> None:
        result = self.run_cli("stack", "stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        native = [command[1:-1] for command in self.native_commands()]
        joined = [" ".join(command) for command in native]
        shutdown = [command for command in joined if "--runtime-target sim system shutdown" in command]
        self.assertEqual(len(shutdown), 2)
        self.assertIn("--dry-run --operation-id iii-dev-shutdown-", shutdown[0])
        self.assertIn("--operation-id iii-dev-shutdown-", shutdown[1])
        self.assertIn("--confirm --non-interactive --output=json", shutdown[1])
        api_stop = [command for command in joined if "--runtime-target sim api stop" in command]
        self.assertEqual(len(api_stop), 2)
        self.assertIn("--dry-run --operation-id iii-dev-api-stop-", api_stop[0])
        self.assertIn("--confirm --non-interactive --output=json", api_stop[1])
        self.assertIn(["gc", "stop"], self.commands())
        ordered = self.commands()
        self.assertLess(ordered.index(["gc", "stop"]), ordered.index(self.native_commands()[0]))

    def test_stack_status_and_attach_use_explicit_sim_target(self) -> None:
        status = self.run_cli("stack", "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        routed = [command[1:-1] for command in self.native_commands()]
        self.assertIn(["--runtime-target", "sim", "system", "status"], routed)
        self.assertIn(["--runtime-target", "sim", "api", "status"], routed)
        self.assertIn(["gc", "status"], self.commands())

        self.log.unlink(missing_ok=True)
        attached = self.run_cli("stack", "attach", "system")
        self.assertEqual(attached.returncode, 0, attached.stderr)
        self.assertEqual(
            [command[1:-1] for command in self.native_commands()],
            [["--runtime-target", "sim", "system", "attach"]],
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
