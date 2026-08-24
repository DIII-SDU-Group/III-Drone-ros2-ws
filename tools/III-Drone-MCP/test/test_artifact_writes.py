from iii_drone_mcp.agent_tools import DroneAgentTools


def test_artifact_write_replaces_read_only_existing_file(tmp_path):
    existing = tmp_path / "capture.txt"
    existing.write_text("old", encoding="utf-8")
    existing.chmod(0o444)
    tools = object.__new__(DroneAgentTools)
    tools.artifact_dir = tmp_path

    written = tools._write_artifact("capture.txt", "new")

    assert written.read_text(encoding="utf-8") == "new"
