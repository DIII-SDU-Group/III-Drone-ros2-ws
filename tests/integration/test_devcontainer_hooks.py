from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
POST_START = ROOT / ".devcontainer" / "post_start.sh"
DEVCONTAINER = ROOT / ".devcontainer" / "devcontainer.json"


def test_post_start_installs_local_configuration_before_cli_package():
    source = POST_START.read_text(encoding="utf-8")

    retired_uninstall = "pip3 uninstall -y iii-deployment"
    requirements_install = "pip3 install -r ./requirements.txt"
    contracts_install = "pip3 install -e ./src/III-Drone-Contracts"
    configuration_install = "pip3 install -e ./src/III-Drone-Configuration"
    cli_install = "pip3 install -e ./tools/III-Drone-CLI"

    # deployment/ holds Ansible/systemd assets, not a Python distribution; the
    # retired iii-deployment package is removed before dependencies refresh.
    assert "pip3 install -e ./deployment" not in source
    assert retired_uninstall in source
    assert source.index(retired_uninstall) < source.index(requirements_install)

    # The CLI depends on the local contracts/configuration packages; install the
    # workspace copies first so pip never resolves them from an index.
    assert contracts_install in source
    assert configuration_install in source
    assert cli_install in source
    assert source.index(contracts_install) < source.index(cli_install)
    assert source.index(configuration_install) < source.index(cli_install)


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
