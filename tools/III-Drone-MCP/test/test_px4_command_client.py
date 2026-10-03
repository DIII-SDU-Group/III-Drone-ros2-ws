import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from iii_drone_mcp.px4_command_client import Px4CommandClient


def test_mavsdk_server_port_defaults_to_standard_port():
    with patch.dict("os.environ", {}, clear=True):
        client = Px4CommandClient()

    assert client._server_port == 50051


def test_mavsdk_server_port_can_be_isolated_by_environment():
    with patch.dict("os.environ", {"III_MAVSDK_SERVER_PORT": "50081"}, clear=True):
        client = Px4CommandClient("udpin://0.0.0.0:14548")

    assert client._server_port == 50081


def test_mavsdk_server_uses_gcs_identity_by_default():
    with patch.dict("os.environ", {}, clear=True):
        client = Px4CommandClient()

    assert client._server_system_id == 255
    assert client._server_component_id == 190


def test_targeted_takeoff_uses_explicit_px4_identity():
    client = Px4CommandClient()
    plugin = MagicMock()
    plugin.send_message = AsyncMock()
    client._drone = MagicMock()
    client._drone.mavlink_direct = plugin

    with patch("mavsdk.mavlink_direct.MavlinkMessage") as message_type:
        result = asyncio.run(
            client.takeoff_target(
                target_system=8,
                target_component=1,
                takeoff_altitude_m=2.5,
                latitude_deg=55.47,
                longitude_deg=10.33,
                takeoff_altitude_amsl_m=17.5,
            )
        )

    assert result == {
        "command": "MAV_CMD_NAV_TAKEOFF",
        "target_system": 8,
        "target_component": 1,
        "takeoff_altitude_m": 2.5,
        "takeoff_altitude_amsl_m": 17.5,
        "latitude_deg": 55.47,
        "longitude_deg": 10.33,
    }
    assert message_type.call_args.kwargs["target_system_id"] == 8
    assert message_type.call_args.kwargs["target_component_id"] == 1
    assert '"command": 22' in message_type.call_args.kwargs["fields_json"]
    assert '"param5": 55.47' in message_type.call_args.kwargs["fields_json"]
    assert '"param7": 17.5' in message_type.call_args.kwargs["fields_json"]
    plugin.send_message.assert_awaited_once()


def test_targeted_land_uses_explicit_px4_identity():
    client = Px4CommandClient()
    plugin = MagicMock()
    plugin.send_message = AsyncMock()
    client._drone = MagicMock()
    client._drone.mavlink_direct = plugin

    with patch("mavsdk.mavlink_direct.MavlinkMessage") as message_type:
        result = asyncio.run(client.land_target(target_system=8, target_component=1))

    assert result == {
        "command": "MAV_CMD_NAV_LAND",
        "target_system": 8,
        "target_component": 1,
    }
    assert message_type.call_args.kwargs["target_system_id"] == 8
    assert message_type.call_args.kwargs["target_component_id"] == 1
    assert '"command": 21' in message_type.call_args.kwargs["fields_json"]
    plugin.send_message.assert_awaited_once()


def test_targeted_hold_uses_auto_loiter_on_explicit_px4_identity():
    client = Px4CommandClient()
    plugin = MagicMock()
    plugin.send_message = AsyncMock()
    client._drone = MagicMock()
    client._drone.mavlink_direct = plugin

    with patch("mavsdk.mavlink_direct.MavlinkMessage") as message_type:
        result = asyncio.run(client.hold_target(target_system=8, target_component=1))

    assert result["mode"] == "AUTO_LOITER"
    assert message_type.call_args.kwargs["target_system_id"] == 8
    assert '"command": 176' in message_type.call_args.kwargs["fields_json"]
    assert '"param3": 3' in message_type.call_args.kwargs["fields_json"]
    plugin.send_message.assert_awaited_once()


@pytest.mark.parametrize("value", ["0", "65536"])
def test_mavsdk_server_port_rejects_out_of_range_values(value):
    with patch.dict("os.environ", {"III_MAVSDK_SERVER_PORT": value}, clear=True):
        with pytest.raises(ValueError, match="III_MAVSDK_SERVER_PORT"):
            Px4CommandClient()
