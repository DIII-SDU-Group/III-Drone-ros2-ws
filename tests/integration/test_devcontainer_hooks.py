from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
POST_START = ROOT / ".devcontainer" / "post_start.sh"
DEVCONTAINER = ROOT / ".devcontainer" / "devcontainer.json"


def test_post_start_installs_local_configuration_before_deployment_package():
    source = POST_START.read_text(encoding="utf-8")

    contracts_install = "pip3 install -e ./src/III-Drone-Contracts"
    configuration_install = "pip3 install -e ./src/III-Drone-Configuration"
    deployment_install = "pip3 install -e ./deployment"

    assert contracts_install in source
    assert configuration_install in source
    assert source.index(contracts_install) < source.index(deployment_install)
    assert source.index(configuration_install) < source.index(deployment_install)


def test_devcontainer_exposes_canonical_worktree_git_metadata():
    source = DEVCONTAINER.read_text(encoding="utf-8")

    assert (
        "source=${localWorkspaceFolder}/../III-Drone-ros2-ws,"
        "target=/home/iii/III-Drone-ros2-ws,type=bind,consistency=cached"
    ) in source
    assert (
        "source=.,target=/home/iii/${localWorkspaceFolderBasename},"
        "type=bind,consistency=cached"
    ) in source
