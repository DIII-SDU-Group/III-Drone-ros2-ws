from __future__ import annotations

from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def test_lock_scripts_use_checked_out_submodule_head(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    _git(child, "init", "-q")
    _git(child, "config", "user.name", "III Test")
    _git(child, "config", "user.email", "iii-test@example.invalid")
    (child / "value.txt").write_text("one\n", encoding="utf-8")
    _git(child, "add", "value.txt")
    _git(child, "commit", "-qm", "one")
    first = _git(child, "rev-parse", "HEAD")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.name", "III Test")
    _git(workspace, "config", "user.email", "iii-test@example.invalid")
    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(child),
            "modules/child",
        ],
        cwd=workspace,
        check=True,
    )
    _git(workspace, "commit", "-qam", "add child")

    scripts = workspace / "scripts/git"
    scripts.mkdir(parents=True)
    (workspace / "deps").mkdir()
    for name in ("update_submodule_lock.sh", "verify_submodule_lock.sh"):
        shutil.copy2(ROOT / "scripts/git" / name, scripts / name)

    checkout = workspace / "modules/child"
    _git(checkout, "config", "user.name", "III Test")
    _git(checkout, "config", "user.email", "iii-test@example.invalid")
    (checkout / "value.txt").write_text("two\n", encoding="utf-8")
    _git(checkout, "add", "value.txt")
    _git(checkout, "commit", "-qm", "two")
    second = _git(checkout, "rev-parse", "HEAD")
    assert first != second
    assert _git(workspace, "ls-files", "-s", "modules/child").split()[1] == first

    subprocess.run([scripts / "update_submodule_lock.sh"], cwd=workspace, check=True)
    lock = (workspace / "deps/submodule-lock.txt").read_text(encoding="utf-8")
    assert f"modules/child {second}" in lock
    subprocess.run([scripts / "verify_submodule_lock.sh"], cwd=workspace, check=True)
