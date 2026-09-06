from __future__ import annotations

from pathlib import Path
import subprocess

from iii_deployment.field_impact import detailed_field_impact


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def commit(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)


def test_detailed_impact_binds_tree_dependencies_and_parameter_reconciliation(
    tmp_path: Path,
) -> None:
    mission = tmp_path / "src/III-Drone-Mission"
    write(
        mission / "CMakeLists.txt",
        """
iii_register_mission(
  ID experimental-one
  SPECIFICATION mission_specification/one.yaml
  CLASSIFICATION experimental
  PROFILES real opti_track
)
""",
    )
    write(
        mission / "mission_specification/one.yaml",
        "entries:\n  - behavior_tree_xml_file: behavior_trees/one.xml\n",
    )
    write(mission / "behavior_trees/one.xml", "<root/>\n")
    commit(mission)

    configuration = tmp_path / "src/III-Drone-Configuration"
    write(
        configuration / "config/parameters/parameter_manifest.yaml",
        "control:\n  speed:\n    type: float\n    value: 1.0\n",
    )
    write(
        configuration / "config/parameter_sets/real/tracked/default.yaml",
        "/**:\n  ros__parameters:\n    /control/speed: 1.0\n",
    )
    commit(configuration)

    write(
        mission / "mission_specification/one.yaml",
        "entries:\n  - behavior_tree_xml_file: behavior_trees/one.xml\n  - behavior_tree_xml_file: behavior_trees/two.xml\n",
    )
    write(mission / "behavior_trees/one.xml", "<root changed='true'/>\n")
    write(
        configuration / "config/parameters/parameter_manifest.yaml",
        "control:\n  speed:\n    type: float\n    value: 2.0\n  gain:\n    type: float\n    value: 0.5\n",
    )
    write(
        configuration / "config/parameter_sets/real/tracked/default.yaml",
        "/**:\n  ros__parameters:\n    /control/speed: 2.0\n",
    )
    changed = [
        "src/III-Drone-Mission/mission_specification/one.yaml",
        "src/III-Drone-Mission/behavior_trees/one.xml",
        "src/III-Drone-Configuration/config/parameters/parameter_manifest.yaml",
        "src/III-Drone-Configuration/config/parameter_sets/real/tracked/default.yaml",
    ]
    impact = detailed_field_impact(tmp_path, changed)
    assert impact["missions"]["entries"] == [
        {
            "id": "experimental-one",
            "state": "changed",
            "classification": "experimental",
            "profiles": ["opti_track", "real"],
            "behavior_trees": ["behavior_trees/one.xml", "behavior_trees/two.xml"],
        }
    ]
    assert impact["missions"]["behavior_trees"][0]["impacted_mission_ids"] == [
        "experimental-one"
    ]
    manifest = impact["parameters"]["manifest"]
    assert manifest["added"] == ["/control/gain"]
    assert manifest["reintroduction_candidates"] == ["/control/gain"]
    assert (
        manifest["reintroduction_determination"]
        == "requires-target-legacy-shadow-review"
    )
    assert manifest["defaults_changed"] == ["/control/speed"]
    assert impact["parameters"]["reconciliation_actions"] == [
        {"key": "/control/gain", "action": "add-or-block-reintroduction"},
        {
            "key": "/control/speed",
            "action": "preserve-live-value-and-review-change",
        },
    ]
    assert impact["parameters"]["parameter_sets"][0]["action"] == "review-and-reconcile"
