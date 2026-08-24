from unittest.mock import patch

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


@pytest.mark.parametrize("value", ["0", "65536"])
def test_mavsdk_server_port_rejects_out_of_range_values(value):
    with patch.dict("os.environ", {"III_MAVSDK_SERVER_PORT": value}, clear=True):
        with pytest.raises(ValueError, match="III_MAVSDK_SERVER_PORT"):
            Px4CommandClient()
