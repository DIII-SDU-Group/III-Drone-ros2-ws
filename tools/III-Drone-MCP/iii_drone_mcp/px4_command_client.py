from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
import signal
import struct
import subprocess
import time
from typing import Any, Optional


@dataclass(frozen=True)
class Px4CommandTelemetry:
    armed: Optional[bool]
    flight_mode: Optional[str]
    in_air: Optional[bool]


class Px4CommandClient:
    """MAVSDK-backed QGroundControl-equivalent PX4 command client."""

    def __init__(self, system_address: str = "udpin://0.0.0.0:14540"):
        self._system_address = system_address
        self._drone = None
        self._plugin_manager = None
        self._server_process = None
        self._owns_server = False

    async def connect(self):
        self._install_grpc_loop_close_filter()
        try:
            from mavsdk import System
        except ImportError as exc:
            raise RuntimeError("mavsdk is required for PX4 command operations") from exc

        existing_port = self.find_existing_server_port(self._system_address)
        if existing_port is not None:
            self._drone = System(mavsdk_server_address="127.0.0.1", port=existing_port)
            await self._init_plugins("127.0.0.1", existing_port)
            async for state in self._drone.core.connection_state():
                if state.is_connected:
                    return

        self.cleanup_stale_servers(self._system_address)
        self._drone = System()
        self._server_process = self._drone._start_mavsdk_server(
            self._system_address,
            self._drone._port,
            self._drone._sysid,
            self._drone._compid,
        )
        self._owns_server = True
        self._drone._server_process = self._server_process
        await asyncio.sleep(0.5)
        await self._init_plugins("127.0.0.1", self._drone._port)
        async for state in self._drone.core.connection_state():
            if state.is_connected:
                return

    @staticmethod
    def _install_grpc_loop_close_filter() -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if getattr(loop, "_iii_drone_grpc_loop_close_filter", False):
            return

        previous_handler = loop.get_exception_handler()

        def exception_handler(loop, context):
            exception = context.get("exception")
            handle = repr(context.get("handle", ""))
            if (
                isinstance(exception, RuntimeError)
                and str(exception) == "Event loop is closed"
                and "PollerCompletionQueue" in handle
            ):
                return
            if previous_handler is not None:
                previous_handler(loop, context)
            else:
                loop.default_exception_handler(context)

        loop.set_exception_handler(exception_handler)
        setattr(loop, "_iii_drone_grpc_loop_close_filter", True)

    async def _init_plugins(self, host: str, port: int) -> None:
        from mavsdk.async_plugin_manager import AsyncPluginManager
        from mavsdk import system as mavsdk_system

        self._plugin_manager = await AsyncPluginManager.create(host=host, port=port)
        plugin_specs = [
            ("action", "Action"),
            ("action_server", "ActionServer"),
            ("arm_authorizer_server", "ArmAuthorizerServer"),
            ("calibration", "Calibration"),
            ("camera", "Camera"),
            ("camera_server", "CameraServer"),
            ("component_metadata", "ComponentMetadata"),
            ("component_metadata_server", "ComponentMetadataServer"),
            ("core", "Core"),
            ("events", "Events"),
            ("failure", "Failure"),
            ("follow_me", "FollowMe"),
            ("ftp", "Ftp"),
            ("ftp_server", "FtpServer"),
            ("geofence", "Geofence"),
            ("gimbal", "Gimbal"),
            ("gripper", "Gripper"),
            ("info", "Info"),
            ("log_files", "LogFiles"),
            ("log_streaming", "LogStreaming"),
            ("manual_control", "ManualControl"),
            ("mavlink_direct", "MavlinkDirect"),
            ("mission", "Mission"),
            ("mission_raw", "MissionRaw"),
            ("mission_raw_server", "MissionRawServer"),
            ("mocap", "Mocap"),
            ("offboard", "Offboard"),
            ("param", "Param"),
            ("param_server", "ParamServer"),
            ("rtk", "Rtk"),
            ("server_utility", "ServerUtility"),
            ("shell", "Shell"),
            ("telemetry", "Telemetry"),
            ("telemetry_server", "TelemetryServer"),
            ("tracking_server", "TrackingServer"),
            ("transponder", "Transponder"),
            ("tune", "Tune"),
            ("winch", "Winch"),
        ]
        self._drone._plugins = {}
        for module_name, class_name in plugin_specs:
            module = getattr(mavsdk_system, module_name)
            self._drone._plugins[module_name] = getattr(module, class_name)(self._plugin_manager)

    async def arm(self):
        await self._require_drone().action.arm()

    async def takeoff(self):
        await self._require_drone().action.takeoff()

    async def disarm(self):
        await self._require_drone().action.disarm()

    async def land(self):
        await self._require_drone().action.land()

    async def hold(self):
        await self._require_drone().action.hold()

    async def return_to_launch(self):
        await self._require_drone().action.return_to_launch()

    async def set_mode(self, mode: str):
        """Set a PX4 main mode through MAVLink COMMAND_LONG.

        MAVSDK-Python does not expose arbitrary PX4 mode selection, so this
        uses pymavlink when an operator/agent needs the programmatic equivalent
        of selecting a standard QGroundControl flight mode.
        """
        try:
            from pymavlink import mavutil
        except ImportError as exc:
            raise RuntimeError("pymavlink is required for arbitrary PX4 mode selection") from exc

        mode_map = {
            "manual": 1,
            "altitude": 2,
            "altctl": 2,
            "position": 3,
            "posctl": 3,
            "mission": 4,
            "acro": 5,
            "offboard": 6,
            "stabilized": 7,
            "rattitude": 8,
        }
        mode_key = mode.strip().lower()
        if mode_key not in mode_map:
            raise ValueError(f"unsupported PX4 mode: {mode}")

        connection = mavutil.mavlink_connection(self._pymavlink_address())
        try:
            if connection.wait_heartbeat(timeout=5) is None:
                raise TimeoutError("PX4 heartbeat was not received within 5s")
            connection.mav.command_long_send(
                connection.target_system,
                connection.target_component,
                mavutil.mavlink.MAV_CMD_DO_SET_MODE,
                0,
                mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                mode_map[mode_key],
                0,
                0,
                0,
                0,
                0,
            )
        finally:
            connection.close()

    async def set_external_nav_state(
        self,
        nav_state: int,
        *,
        target_system: int = 1,
        target_component: int = 1,
        source_system: int = 255,
        source_component: int = 0,
    ) -> dict[str, Any]:
        """Activate a px4_ros2 external mode by nav_state.

        px4_ros2 registered modes occupy PX4's EXTERNAL1..EXTERNAL8 nav-state
        range. QGroundControl activates them with MAV_CMD_DO_SET_MODE as
        AUTO/EXTERNALn, not by placing the nav_state directly in custom_mode.
        MAVSDK direct transport avoids UDP port conflicts with QGC and keeps
        this equivalent to an operator mode selection.
        """
        try:
            from mavsdk.mavlink_direct import MavlinkMessage
        except ImportError as exc:
            raise RuntimeError("mavsdk.mavlink_direct is required for PX4 external mode activation") from exc

        nav_state_int = int(nav_state)
        external1_nav_state = 23
        external8_nav_state = 30
        if nav_state_int < external1_nav_state or nav_state_int > external8_nav_state:
            raise ValueError(
                f"PX4 external mode activation requires nav_state in "
                f"[{external1_nav_state}, {external8_nav_state}], got {nav_state_int}"
            )

        px4_custom_main_mode_auto = 4
        px4_custom_sub_mode_external1 = 11
        external_index = nav_state_int - external1_nav_state
        custom_sub_mode = px4_custom_sub_mode_external1 + external_index

        message = MavlinkMessage(
            message_name="COMMAND_LONG",
            system_id=int(source_system),
            component_id=int(source_component),
            target_system_id=int(target_system),
            target_component_id=int(target_component),
            fields_json=json.dumps(
                {
                    "target_system": int(target_system),
                    "target_component": int(target_component),
                    "command": 176,  # MAV_CMD_DO_SET_MODE
                    "confirmation": 0,
                    "param1": 1,  # MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
                    "param2": px4_custom_main_mode_auto,
                    "param3": custom_sub_mode,
                    "param4": 0,
                    "param5": 0,
                    "param6": 0,
                    "param7": 0,
                }
            ),
        )
        await self._require_drone().mavlink_direct.send_message(message)
        return {
            "nav_state": nav_state_int,
            "px4_custom_main_mode": px4_custom_main_mode_auto,
            "px4_custom_sub_mode": custom_sub_mode,
            "external_mode_index": external_index + 1,
        }

    def set_external_nav_state_mavlink(
        self,
        nav_state: int,
        *,
        target_system: int = 1,
        target_component: int | None = None,
        source_system: int = 255,
        source_component: int = 0,
        timeout_sec: float = 8.0,
    ) -> dict[str, Any]:
        try:
            from pymavlink import mavutil
        except ImportError as exc:
            raise RuntimeError("pymavlink is required for PX4 external mode activation") from exc

        nav_state_int = int(nav_state)
        external1_nav_state = 23
        external8_nav_state = 30
        if nav_state_int < external1_nav_state or nav_state_int > external8_nav_state:
            raise ValueError(
                f"PX4 external mode activation requires nav_state in "
                f"[{external1_nav_state}, {external8_nav_state}], got {nav_state_int}"
            )

        px4_custom_main_mode_auto = 4
        px4_custom_sub_mode_external1 = 11
        external_index = nav_state_int - external1_nav_state
        custom_sub_mode = px4_custom_sub_mode_external1 + external_index

        connection = mavutil.mavlink_connection(
            self._pymavlink_address(),
            source_system=int(source_system),
            source_component=int(source_component),
        )
        try:
            if connection.wait_heartbeat(timeout=timeout_sec) is None:
                raise TimeoutError(f"PX4 heartbeat was not received within {timeout_sec}s")
            mav_target_system = int(connection.target_system or target_system)
            mav_target_component = int(
                connection.target_component if target_component is None else target_component
            )
            connection.mav.command_long_send(
                mav_target_system,
                mav_target_component,
                mavutil.mavlink.MAV_CMD_DO_SET_MODE,
                0,
                mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                px4_custom_main_mode_auto,
                custom_sub_mode,
                0,
                0,
                0,
                0,
            )
            return {
                "nav_state": nav_state_int,
                "px4_custom_main_mode": px4_custom_main_mode_auto,
                "px4_custom_sub_mode": custom_sub_mode,
                "external_mode_index": external_index + 1,
                "mavlink_target_system": mav_target_system,
                "mavlink_target_component": mav_target_component,
                "mavlink_address": self._pymavlink_address(),
            }
        finally:
            connection.close()

    def get_param(self, name: str, *, timeout_sec: float = 5.0) -> dict[str, Any]:
        try:
            from pymavlink import mavutil
        except ImportError as exc:
            raise RuntimeError("pymavlink is required for PX4 parameter operations") from exc

        connection = mavutil.mavlink_connection(self._pymavlink_address())
        try:
            if connection.wait_heartbeat(timeout=timeout_sec) is None:
                raise TimeoutError(f"PX4 heartbeat was not received within {timeout_sec}s")
            return self._request_param(connection, name, timeout_sec=timeout_sec)
        finally:
            connection.close()

    def set_param(
        self,
        name: str,
        value: float,
        *,
        param_type: int | None = None,
        timeout_sec: float = 5.0,
    ) -> dict[str, Any]:
        try:
            from pymavlink import mavutil
        except ImportError as exc:
            raise RuntimeError("pymavlink is required for PX4 parameter operations") from exc

        connection = mavutil.mavlink_connection(self._pymavlink_address())
        try:
            if connection.wait_heartbeat(timeout=timeout_sec) is None:
                raise TimeoutError(f"PX4 heartbeat was not received within {timeout_sec}s")
            if param_type is None:
                current = self._request_param(connection, name, timeout_sec=timeout_sec)
                param_type = int(current["param_type"])
            encoded_name = name.encode("utf-8")
            connection.mav.param_set_send(
                connection.target_system,
                connection.target_component,
                encoded_name,
                self._encode_param_value(float(value), int(param_type)),
                int(param_type),
            )
            updated = self._wait_param_value(connection, name, timeout_sec=timeout_sec)
            updated["requested_value"] = float(value)
            return updated
        finally:
            connection.close()

    async def telemetry_snapshot(self) -> Px4CommandTelemetry:
        drone = self._require_drone()

        armed = None
        flight_mode = None
        in_air = None

        async for armed_state in drone.telemetry.armed():
            armed = bool(armed_state)
            break

        async for mode in drone.telemetry.flight_mode():
            flight_mode = str(mode)
            break

        async for air_state in drone.telemetry.in_air():
            in_air = bool(air_state)
            break

        return Px4CommandTelemetry(armed=armed, flight_mode=flight_mode, in_air=in_air)

    async def get_param_mavsdk(self, name: str) -> dict[str, Any]:
        drone = self._require_drone()
        try:
            value = await drone.param.get_param_int(name)
            return {
                "param_name": name,
                "param_value": float(value),
                "raw_param_value": float(value),
                "param_type": self._mav_param_type_int32(),
                "source": "mavsdk_int",
            }
        except Exception as int_exc:
            try:
                value = await drone.param.get_param_float(name)
                return {
                    "param_name": name,
                    "param_value": float(value),
                    "raw_param_value": float(value),
                    "param_type": self._mav_param_type_real32(),
                    "source": "mavsdk_float",
                }
            except Exception as float_exc:
                raise RuntimeError(
                    f"PX4 parameter {name!r} could not be read as int or float: "
                    f"int_error={int_exc!r}; float_error={float_exc!r}"
                ) from float_exc

    async def set_param_mavsdk(
        self,
        name: str,
        value: float,
        *,
        param_type: int | None = None,
    ) -> dict[str, Any]:
        drone = self._require_drone()
        if self._param_type_is_integer(param_type) or (param_type is None and float(value).is_integer()):
            int_value = int(round(float(value)))
            await drone.param.set_param_int(name, int_value)
            updated = await self.get_param_mavsdk(name)
            updated["requested_value"] = float(value)
            return updated
        await drone.param.set_param_float(name, float(value))
        updated = await self.get_param_mavsdk(name)
        updated["requested_value"] = float(value)
        return updated

    async def wait_until(
        self,
        predicate,
        *,
        timeout_sec: float,
        poll_period_sec: float = 0.25,
    ) -> Px4CommandTelemetry:
        deadline = time.monotonic() + timeout_sec
        last_snapshot = await self.telemetry_snapshot()
        while time.monotonic() < deadline:
            if predicate(last_snapshot):
                return last_snapshot
            await asyncio.sleep(poll_period_sec)
            last_snapshot = await self.telemetry_snapshot()
        if predicate(last_snapshot):
            return last_snapshot
        raise TimeoutError(f"PX4 post-condition not reached within {timeout_sec}s; last telemetry={last_snapshot}")

    def close(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.close_async())
            return
        if loop.is_running():
            loop.create_task(self.close_async())
            return
        asyncio.run(self.close_async())

    async def close_async(self) -> None:
        drone = self._drone
        self._drone = None
        plugin_manager = self._plugin_manager
        self._plugin_manager = None
        if drone is not None:
            # MAVSDK-Python does not expose a public async channel close. Drop
            # plugin references while the caller's asyncio loop is still alive
            # so grpc.aio completion queues are not finalized after loop close.
            try:
                drone._plugins.clear()
            except AttributeError:
                pass
        if plugin_manager is not None:
            channel = getattr(plugin_manager, "_channel", None)
            if channel is not None:
                await channel.close(grace=0.1)
        if not self._owns_server:
            return
        process = self._server_process
        self._server_process = None
        self._owns_server = False
        if process is None:
            return
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)

    @staticmethod
    def find_existing_server_port(system_address: str) -> int | None:
        try:
            completed = subprocess.run(
                ["ps", "-eo", "pid=,stat=,args="],
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError:
            return None
        for line in completed.stdout.splitlines():
            parts = line.strip().split(maxsplit=2)
            if len(parts) != 3:
                continue
            _pid, stat, command = parts
            if "Z" in stat or "mavsdk_server" not in command or system_address not in command:
                continue
            tokens = command.split()
            for index, token in enumerate(tokens):
                if token == "-p" and index + 1 < len(tokens):
                    try:
                        return int(tokens[index + 1])
                    except ValueError:
                        return None
        return None

    @staticmethod
    def cleanup_stale_servers(system_address: str) -> None:
        port = system_address.rsplit(":", 1)[-1]
        try:
            completed = subprocess.run(
                ["pgrep", "-af", "mavsdk_server"],
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError:
            return
        current_pid = os.getpid()
        for line in completed.stdout.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) != 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            command = parts[1]
            if pid == current_pid or port not in command:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except PermissionError:
                continue
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def _require_drone(self):
        if self._drone is None:
            raise RuntimeError("PX4 command client is not connected")
        return self._drone

    def _request_param(self, connection: Any, name: str, *, timeout_sec: float) -> dict[str, Any]:
        connection.mav.param_request_read_send(
            connection.target_system,
            connection.target_component,
            name.encode("utf-8"),
            -1,
        )
        return self._wait_param_value(connection, name, timeout_sec=timeout_sec)

    def _wait_param_value(self, connection: Any, name: str, *, timeout_sec: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            message = connection.recv_match(type="PARAM_VALUE", blocking=True, timeout=remaining)
            if message is None:
                continue
            param_id = message.param_id
            if isinstance(param_id, bytes):
                param_name = param_id.decode("utf-8", errors="replace").rstrip("\x00")
            else:
                param_name = str(param_id).rstrip("\x00")
            if param_name != name:
                continue
            return {
                "param_name": param_name,
                "param_value": self._decode_param_value(float(message.param_value), int(message.param_type)),
                "raw_param_value": float(message.param_value),
                "param_type": int(message.param_type),
                "param_count": int(message.param_count),
                "param_index": int(message.param_index),
            }
        raise TimeoutError(f"PX4 parameter {name!r} was not received within {timeout_sec}s")

    @staticmethod
    def _decode_param_value(value: float, param_type: int) -> float:
        try:
            from pymavlink import mavutil
        except ImportError as exc:
            raise RuntimeError("pymavlink is required for PX4 parameter operations") from exc

        raw = struct.pack(">f", float(value))
        if param_type == mavutil.mavlink.MAV_PARAM_TYPE_UINT8:
            return float(struct.unpack(">xxxB", raw)[0])
        if param_type == mavutil.mavlink.MAV_PARAM_TYPE_INT8:
            return float(struct.unpack(">xxxb", raw)[0])
        if param_type == mavutil.mavlink.MAV_PARAM_TYPE_UINT16:
            return float(struct.unpack(">xxH", raw)[0])
        if param_type == mavutil.mavlink.MAV_PARAM_TYPE_INT16:
            return float(struct.unpack(">xxh", raw)[0])
        if param_type == mavutil.mavlink.MAV_PARAM_TYPE_UINT32:
            return float(struct.unpack(">I", raw)[0])
        if param_type == mavutil.mavlink.MAV_PARAM_TYPE_INT32:
            return float(struct.unpack(">i", raw)[0])
        return float(value)

    @staticmethod
    def _encode_param_value(value: float, param_type: int) -> float:
        try:
            from pymavlink import mavutil
        except ImportError as exc:
            raise RuntimeError("pymavlink is required for PX4 parameter operations") from exc

        if param_type == mavutil.mavlink.MAV_PARAM_TYPE_UINT8:
            raw = struct.pack(">xxxB", int(value))
        elif param_type == mavutil.mavlink.MAV_PARAM_TYPE_INT8:
            raw = struct.pack(">xxxb", int(value))
        elif param_type == mavutil.mavlink.MAV_PARAM_TYPE_UINT16:
            raw = struct.pack(">xxH", int(value))
        elif param_type == mavutil.mavlink.MAV_PARAM_TYPE_INT16:
            raw = struct.pack(">xxh", int(value))
        elif param_type == mavutil.mavlink.MAV_PARAM_TYPE_UINT32:
            raw = struct.pack(">I", int(value))
        elif param_type == mavutil.mavlink.MAV_PARAM_TYPE_INT32:
            raw = struct.pack(">i", int(value))
        else:
            return float(value)
        return float(struct.unpack(">f", raw)[0])

    @staticmethod
    def _mav_param_type_int32() -> int:
        try:
            from pymavlink import mavutil
            return int(mavutil.mavlink.MAV_PARAM_TYPE_INT32)
        except ImportError:
            return 6

    @staticmethod
    def _mav_param_type_real32() -> int:
        try:
            from pymavlink import mavutil
            return int(mavutil.mavlink.MAV_PARAM_TYPE_REAL32)
        except ImportError:
            return 9

    @staticmethod
    def _param_type_is_integer(param_type: int | None) -> bool:
        if param_type is None:
            return False
        try:
            from pymavlink import mavutil
            integer_types = {
                mavutil.mavlink.MAV_PARAM_TYPE_UINT8,
                mavutil.mavlink.MAV_PARAM_TYPE_INT8,
                mavutil.mavlink.MAV_PARAM_TYPE_UINT16,
                mavutil.mavlink.MAV_PARAM_TYPE_INT16,
                mavutil.mavlink.MAV_PARAM_TYPE_UINT32,
                mavutil.mavlink.MAV_PARAM_TYPE_INT32,
            }
            return int(param_type) in {int(item) for item in integer_types}
        except ImportError:
            return int(param_type) in {1, 2, 3, 4, 5, 6}

    def _pymavlink_address(self) -> str:
        if self._system_address.startswith("udpin://"):
            port = self._system_address.rsplit(":", 1)[-1]
            if port == "14540":
                return "udpin:0.0.0.0:14550"
            return "udpin:0.0.0.0:" + port
        if self._system_address.startswith("udp://:"):
            port = self._system_address.rsplit(":", 1)[-1]
            if port == "14540":
                return "udpin:0.0.0.0:14550"
            return "udpout:127.0.0.1:" + port
        if self._system_address.startswith("udp://"):
            return "udpout:" + self._system_address.removeprefix("udp://")
        return self._system_address
