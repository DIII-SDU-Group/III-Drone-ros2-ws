"""Keep virtual HIL telemetry separate from the physical PX4 transport, and
keep every aircraft profile's shell on the provisioned runtime contract."""

from pathlib import Path
import os
import re
import subprocess
import unittest
import json
import tempfile

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[2]
VARS = ROOT / "deployment/ansible/vars/raspberry-pi-5-noble-arm64.yml"
TEMPLATE = ROOT / "deployment/ansible/roles/runtime_control_plane/templates/runtime.env.j2"
AIRCRAFT_PROFILES = ("hil", "real", "opti_track")
# HIL-qualified stack middleware that provisioning writes for every aircraft
# profile (contract C6); PX4's UXRCE_DDS_DOM_ID must equal ROS_DOMAIN_ID.
STACK_DDS = {
    "ROS_DOMAIN_ID": "42",
    "ROS_LOCALHOST_ONLY": "0",
    "ROS_AUTOMATIC_DISCOVERY_RANGE": "SUBNET",
    "RMW_IMPLEMENTATION": "rmw_fastrtps_cpp",
    "FASTDDS_BUILTIN_TRANSPORTS": "UDPv4",
}
# The Pi's services use Fast DDS's default transports: shared memory between
# Pi processes, UDPv4 to the workstation. Operator shells stay UDPv4.
SERVICE_DDS = {**STACK_DDS, "FASTDDS_BUILTIN_TRANSPORTS": "DEFAULT"}
SHELL_PROFILES = {"real": "setup/setup_real.bash", "opti_track": "setup/setup_opti_track.bash"}


def render_runtime_environment(profile, **overrides):
    values = yaml.safe_load(VARS.read_text())
    values["iii_profile"] = profile
    values.update(overrides)
    return jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(
        TEMPLATE.read_text()
    ).render(values)


def runtime_environment(profile, **overrides):
    rendered = render_runtime_environment(profile, **overrides)
    return dict(line.split("=", 1) for line in rendered.splitlines() if line and not line.startswith("#"))


def aircraft_shell(directory, profile, *, runtime_env=None, extra=None, script=None):
    """Source an aircraft setup profile hermetically and return its environment.

    A stub ROS prefix stands in for /opt/ros/jazzy; the onboard runtime env and
    workspace overlay paths are explicit so the host's own files never leak in.
    """
    directory = Path(directory)
    ros = directory / "ros"
    ros.mkdir(exist_ok=True)
    (ros / "setup.bash").write_text(":\n", encoding="utf-8")
    env = {
        "HOME": str(directory),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "III_ROS_PREFIX": str(ros),
        "III_WORKSPACE_INSTALL": str(directory / "absent-install"),
        "III_ONBOARD_RUNTIME_ENV": str(runtime_env or directory / "absent-runtime.env"),
        # Stale inherited values the profile must replace or drop.
        "CYCLONEDDS_URI": "file:///stale/cyclonedds.xml",
        "III_SYSTEM_DAEMON_SOCKET": "/stale/system_manager.sock",
    }
    env.update(extra or {})
    body = script or (
        'set -eu; source "$1"; '
        'python3 -c "import json, os; print(json.dumps(dict(os.environ)))"'
    )
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", body, "bash", str(ROOT / SHELL_PROFILES[profile])],
        env=env, text=True, capture_output=True, check=False,
    )


class HilTransportProfilesTests(unittest.TestCase):
    def test_hil_runtime_does_not_listen_to_the_physical_hil_endpoint(self):
        values = yaml.safe_load(VARS.read_text())
        env = runtime_environment("hil")
        self.assertEqual(env["III_RUNTIME_API_PX4_SYSTEM_ID"], "8")
        self.assertEqual(env["III_RUNTIME_API_PX4_MAVLINK_ENDPOINT"], "udpin://0.0.0.0:14544")
        self.assertEqual(env["ROS_DOMAIN_ID"], "42")
        self.assertEqual(env["RMW_IMPLEMENTATION"], "rmw_fastrtps_cpp")
        # Shared memory on the Pi like the REAL default; the workstation
        # shell stays UDPv4 (cross-host).
        self.assertEqual(env["FASTDDS_BUILTIN_TRANSPORTS"], "DEFAULT")
        self.assertNotEqual(values["iii_hil_sitl_mavlink_udp_port"], values["iii_hil_mavlink_udp_port"])

    def test_real_and_opti_track_keep_their_physical_endpoint(self):
        for profile in ("real", "opti_track"):
            with self.subTest(profile=profile):
                env = runtime_environment(profile)
                self.assertEqual(env["III_RUNTIME_API_PX4_MAVLINK_ENDPOINT"], "udpin://0.0.0.0:14540")
                self.assertEqual(env["III_RUNTIME_API_PX4_SYSTEM_ID"], "1")
                self.assertEqual(env["SIMULATION"], "false")

    def test_every_aircraft_profile_provisions_the_stack_middleware(self):
        for profile in AIRCRAFT_PROFILES:
            with self.subTest(profile=profile):
                env = runtime_environment(profile)
                self.assertEqual({key: env.get(key) for key in SERVICE_DDS}, SERVICE_DDS)
                self.assertEqual(env["III_SYSTEM_PROFILE"], profile)
                self.assertNotIn("CYCLONEDDS_URI", env)

    def test_provisioned_ros_domain_override_reaches_the_runtime_env(self):
        for profile in AIRCRAFT_PROFILES:
            with self.subTest(profile=profile):
                # Extra vars arrive as strings from `iii host provision`.
                env = runtime_environment(profile, iii_ros_domain_id="57")
                self.assertEqual(env["ROS_DOMAIN_ID"], "57")

    def test_service_middleware_overrides_follow_the_provisioned_domain(self):
        tasks = yaml.safe_load(
            (ROOT / "deployment/ansible/roles/runtime_control_plane/tasks/main.yml").read_text()
        )
        by_name = {task["name"]: task for task in tasks}
        install = by_name["Install aircraft Fast DDS middleware overrides"]
        legacy = by_name["Remove the superseded HIL-only middleware overrides"]
        # Installed for every aircraft profile; the HIL-only file is retired.
        self.assertNotIn("when", install)
        self.assertNotIn("when", legacy)
        self.assertTrue(install["ansible.builtin.copy"]["dest"].endswith("/30-iii-fastdds.conf"))
        self.assertTrue(legacy["ansible.builtin.file"]["path"].endswith("/30-hil-fastdds.conf"))
        self.assertEqual(legacy["ansible.builtin.file"]["state"], "absent")
        values = yaml.safe_load(VARS.read_text())
        for domain in (values["iii_ros_domain_id"], "57"):
            content = jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(
                install["ansible.builtin.copy"]["content"]
            ).render({**values, "iii_ros_domain_id": domain})
            environment = dict(
                line.removeprefix("Environment=").split("=", 1)
                for line in content.splitlines()
                if line.startswith("Environment=")
            )
            self.assertEqual(environment, {**SERVICE_DDS, "ROS_DOMAIN_ID": str(domain)})
            self.assertIn("UnsetEnvironment=CYCLONEDDS_URI", content)

    def test_onboard_aircraft_shells_adopt_the_provisioned_runtime_contract(self):
        for profile in SHELL_PROFILES:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                runtime_env = Path(directory, "runtime.env")
                runtime_env.write_text(
                    render_runtime_environment(profile, iii_ros_domain_id="57"), encoding="utf-8"
                )
                provisioned = runtime_environment(profile, iii_ros_domain_id="57")
                result = aircraft_shell(directory, profile, runtime_env=runtime_env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                values = json.loads(result.stdout)
                for key in (
                    "III_SYSTEM_RUNTIME_DIR",
                    "III_SYSTEM_DAEMON_SOCKET",
                    "III_SYSTEM_DAEMON_LOG",
                    "CONFIG_BASE_DIR",
                    *STACK_DDS,
                ):
                    self.assertEqual(values[key], provisioned[key], key)
                self.assertEqual(values["III_SYSTEM_DAEMON_SOCKET"], "/run/iii/system_manager.sock")
                self.assertEqual(values["CONFIG_BASE_DIR"], "/home/iii/.config/iii_drone")
                self.assertEqual(values["ROS_DOMAIN_ID"], "57")
                self.assertEqual(values["CLI_CONFIGURATION"], "dev")
                self.assertEqual(values["III_SYSTEM_PROFILE"], profile)
                self.assertEqual(values["III_RUNTIME_TARGET"], profile)
                self.assertEqual(values["SIMULATION"], "false")
                # Listener settings are not client endpoints, and the runtime
                # never uses Cyclone DDS.
                self.assertNotIn("III_RUNTIME_API_HOST", values)
                self.assertNotIn("CYCLONEDDS_URI", values)

    def test_workstation_aircraft_shells_keep_the_editable_workspace_defaults(self):
        for profile in SHELL_PROFILES:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                result = aircraft_shell(directory, profile)
                self.assertEqual(result.returncode, 0, result.stderr)
                values = json.loads(result.stdout)
                self.assertEqual(values["III_SYSTEM_DAEMON_SOCKET"], str(ROOT / "runtime/system_manager.sock"))
                self.assertEqual(values["III_SYSTEM_RUNTIME_DIR"], str(ROOT / "runtime"))
                self.assertEqual(values["CONFIG_BASE_DIR"], str(ROOT / ".config"))
                self.assertEqual(values["WORKSPACE_DIR"], str(ROOT))
                self.assertEqual(values["ROS_DOMAIN_ID"], STACK_DDS["ROS_DOMAIN_ID"])
                self.assertEqual(values["RMW_IMPLEMENTATION"], STACK_DDS["RMW_IMPLEMENTATION"])
                self.assertEqual(values["FASTDDS_BUILTIN_TRANSPORTS"], STACK_DDS["FASTDDS_BUILTIN_TRANSPORTS"])
                self.assertNotIn("CYCLONEDDS_URI", values)
                self.assertEqual(values["III_SYSTEM_PROFILE"], profile)
            with tempfile.TemporaryDirectory() as directory:
                result = aircraft_shell(directory, profile, extra={"III_ROS_DOMAIN_ID": "61"})
                self.assertEqual(json.loads(result.stdout)["ROS_DOMAIN_ID"], "61")

    def test_aircraft_shells_ignore_a_non_aircraft_runtime_env(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime_env = Path(directory, "runtime.env")
            runtime_env.write_text(
                "III_SYSTEM_PROFILE=sim\n"
                "III_SYSTEM_DAEMON_SOCKET=/run/iii/system_manager.sock\n"
                "ROS_DOMAIN_ID=7\n",
                encoding="utf-8",
            )
            values = json.loads(aircraft_shell(directory, "opti_track", runtime_env=runtime_env).stdout)
        self.assertEqual(values["III_SYSTEM_DAEMON_SOCKET"], str(ROOT / "runtime/system_manager.sock"))
        self.assertEqual(values["ROS_DOMAIN_ID"], "42")

    def test_aircraft_shells_source_repeatedly_and_load_the_workspace_overlay(self):
        for profile in SHELL_PROFILES:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                overlay = Path(directory, "install")
                overlay.mkdir()
                (overlay / "setup.bash").write_text(
                    'export III_TEST_OVERLAY_SOURCED="$((${III_TEST_OVERLAY_SOURCED:-0} + 1))"\n',
                    encoding="utf-8",
                )
                result = aircraft_shell(
                    directory,
                    profile,
                    extra={"III_WORKSPACE_INSTALL": str(overlay)},
                    script='set -eu; source "$1"; source "$1"; printf "%s" "$III_TEST_OVERLAY_SOURCED"',
                )
                # No readonly variables: a second source is clean.
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout, "2")

    def test_aircraft_shells_fail_without_ros(self):
        for profile in SHELL_PROFILES:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                result = aircraft_shell(
                    directory,
                    profile,
                    extra={"III_ROS_PREFIX": str(Path(directory, "missing-ros"))},
                    script='source "$1"; printf "%s|%s" "$?" "${III_SYSTEM_PROFILE:-unset}"',
                )
                self.assertEqual(result.stdout, "30|unset")
                self.assertIn("requires ROS Jazzy", result.stderr)

    def test_hil_shell_and_launcher_match_the_provisioned_listener(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("III_PX4_")}
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; printf "%s" "$III_PX4_SYSTEM_ADDRESS"', "bash", str(ROOT / "setup/setup_hil.bash")],
            env=env, text=True, capture_output=True, check=True,
        )
        expected = runtime_environment("hil")["III_RUNTIME_API_PX4_MAVLINK_ENDPOINT"]
        self.assertEqual(result.stdout, expected)
        source = (ROOT / "tools/simulation/launch_hil_workstation.sh").read_text()
        port = re.search(r'MAVLINK_REMOTE_PORT="\$\{III_HIL_MAVLINK_REMOTE_PORT:-(\d+)\}"', source)
        self.assertIsNotNone(port)
        self.assertEqual(expected, "udpin://0.0.0.0:" + port.group(1))

    def test_explicit_command_endpoint_remains_supported(self):
        env = dict(os.environ, III_PX4_SYSTEM_ADDRESS="udpin://127.0.0.1:15544")
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; printf "%s" "$III_PX4_SYSTEM_ADDRESS"', "bash", str(ROOT / "setup/setup_hil.bash")],
            env=env, text=True, capture_output=True, check=True,
        )
        self.assertEqual(result.stdout, env["III_PX4_SYSTEM_ADDRESS"])

    def test_hil_shell_is_a_complete_remote_simulated_profile(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("III_", "CYCLONEDDS_", "ROS_"))}
        env["RMW_IMPLEMENTATION"] = "rmw_cyclonedds_cpp"
        env["CYCLONEDDS_URI"] = "<CycloneDDS/>"
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; python3 -c \'import os,json; print(json.dumps(dict(os.environ)))\'', "bash", str(ROOT / "setup/setup_hil.bash")],
            env=env, text=True, capture_output=True, check=True,
        )
        values = json.loads(result.stdout)
        self.assertEqual(values["III_SYSTEM_PROFILE"], "hil")
        self.assertEqual(values["III_ENVIRONMENT_PROFILE"], "hil")
        self.assertEqual(values["SIMULATION"], "true")
        self.assertEqual(values["CLI_CONFIGURATION"], "remote")
        self.assertEqual(values["ROS_DOMAIN_ID"], "42")
        self.assertEqual(values["RMW_IMPLEMENTATION"], "rmw_fastrtps_cpp")
        self.assertEqual(values["FASTDDS_BUILTIN_TRANSPORTS"], "UDPv4")
        self.assertNotIn("CYCLONEDDS_URI", values)
        self.assertEqual(values["III_HIL_PI_ENDPOINT"], "iii.local")
        self.assertEqual(values["III_HIL_PI_ADDRESS"], "")
        self.assertEqual(values["III_RUNTIME_API_HOST"], "iii.local")
        self.assertEqual(values["III_SSH_HOST"], "iii.local")
        self.assertEqual(values["III_RUNTIME_API_URL"], "http://iii.local:8765")

    def test_hil_gc_is_separate_from_the_default_gc(self):
        env = dict(
            line.split("=", 1) for line in (ROOT / "setup/ground-control.hil.env").read_text().splitlines()
            if line and not line.startswith("#")
        )
        self.assertEqual(env["III_GC_EXPECTED_PROFILE"], "hil")
        self.assertEqual(env["III_GC_COMPOSE_PROJECT"], "iii-ground-control-hil")
        self.assertEqual(env["III_GC_PROXY_PORT"], "8781")
        self.assertEqual(env["III_GC_FRONTEND_PORT"], "5174")

    def test_explicit_hil_endpoint_overrides_inherited_runtime_aliases(self):
        env = {
            "III_HIL_PI_ENDPOINT": "192.168.1.251",
            "III_RUNTIME_HOST": "stale-runtime",
            "III_RUNTIME_API_HOST": "stale-api",
            "III_SSH_HOST": "stale-ssh",
            "III_RUNTIME_API_URL": "http://stale-api:8765",
        }
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; printf "%s|%s|%s|%s|%s" "$III_HIL_PI_ENDPOINT" "$III_HIL_PI_ADDRESS" "$III_RUNTIME_API_HOST" "$III_SSH_HOST" "$III_RUNTIME_API_URL"', "bash", str(ROOT / "setup/setup_hil.bash")],
            env={**{k: v for k, v in os.environ.items() if not k.startswith(("III_", "CYCLONEDDS_", "ROS_"))}, **env},
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(
            result.stdout,
            "192.168.1.251||192.168.1.251|192.168.1.251|http://192.168.1.251:8765",
        )

    def test_onboard_hil_shell_uses_deployed_daemon_socket_without_listener_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime_env = Path(directory, "runtime.env")
            runtime_env.write_text(
                "III_SYSTEM_PROFILE=hil\n"
                "III_SYSTEM_RUNTIME_DIR=/run/iii\n"
                "III_SYSTEM_DAEMON_SOCKET=/run/iii/system_manager.sock\n"
                "III_SYSTEM_DAEMON_LOG=/home/iii/.ros/log/system-daemon.log\n"
                "CONFIG_BASE_DIR=/home/iii/.config/iii_drone\n"
                "III_RUNTIME_API_HOST=0.0.0.0\n",
                encoding="utf-8",
            )
            env = {k: v for k, v in os.environ.items() if not k.startswith(("III_", "CYCLONEDDS_", "ROS_"))}
            env.update({
                "III_HIL_ONBOARD_RUNTIME_ENV": str(runtime_env),
            })
            result = subprocess.run(
                ["bash", "-c", 'source "$1"; printf "%s|%s|%s|%s|%s" "$CLI_CONFIGURATION" "$III_SYSTEM_DAEMON_SOCKET" "$CONFIG_BASE_DIR" "$III_RUNTIME_API_HOST" "$III_RUNTIME_API_URL"', "bash", str(ROOT / "setup/setup_hil.bash")],
                env=env, text=True, capture_output=True, check=True,
            )
        self.assertEqual(
            result.stdout,
            "dev|/run/iii/system_manager.sock|/home/iii/.config/iii_drone|iii.local|http://iii.local:8765",
        )

    def test_onboard_hil_shell_follows_the_provisioned_stack_domain(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime_env = Path(directory, "runtime.env")
            runtime_env.write_text(
                render_runtime_environment("hil", iii_ros_domain_id="57"), encoding="utf-8"
            )
            env = {k: v for k, v in os.environ.items() if not k.startswith(("III_", "CYCLONEDDS_", "ROS_"))}
            env["III_HIL_ONBOARD_RUNTIME_ENV"] = str(runtime_env)
            script = 'source "$1"; printf "%s|%s" "$ROS_DOMAIN_ID" "${hil_onboard_ros_domain_id-unset}"'
            onboard = subprocess.run(
                ["bash", "-c", script, "bash", str(ROOT / "setup/setup_hil.bash")],
                env=env, text=True, capture_output=True, check=True,
            )
            explicit = subprocess.run(
                ["bash", "-c", script, "bash", str(ROOT / "setup/setup_hil.bash")],
                env={**env, "III_HIL_ROS_DOMAIN_ID": "58"}, text=True, capture_output=True, check=True,
            )
        self.assertEqual(onboard.stdout, "57|unset")
        self.assertEqual(explicit.stdout, "58|unset")

    def test_legacy_hil_host_alias_remains_a_coherent_target_override(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("III_", "CYCLONEDDS_", "ROS_"))}
        env["III_SSH_HOST"] = "pi-on-lan.local"
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; printf "%s|%s|%s|%s" "$III_HIL_PI_ENDPOINT" "$III_RUNTIME_API_HOST" "$III_SSH_HOST" "$III_RUNTIME_API_URL"', "bash", str(ROOT / "setup/setup_hil.bash")],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(
            result.stdout,
            "pi-on-lan.local|pi-on-lan.local|pi-on-lan.local|http://pi-on-lan.local:8765",
        )


if __name__ == "__main__":
    unittest.main()
