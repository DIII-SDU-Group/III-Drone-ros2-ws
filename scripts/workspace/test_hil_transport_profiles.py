"""Keep virtual HIL telemetry separate from the physical PX4 transport."""

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


def runtime_environment(profile):
    values = yaml.safe_load(VARS.read_text())
    values["iii_profile"] = profile
    rendered = jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(
        TEMPLATE.read_text()
    ).render(values)
    return dict(line.split("=", 1) for line in rendered.splitlines() if line and not line.startswith("#"))


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
                self.assertNotIn("ROS_DOMAIN_ID", env)
                self.assertNotIn("RMW_IMPLEMENTATION", env)

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
