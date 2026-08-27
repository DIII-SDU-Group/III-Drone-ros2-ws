from __future__ import annotations

import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts/git"


def _fake_adapters(tmp_path: Path) -> tuple[dict[str, str], Path]:
    adapter_dir = tmp_path / "adapters"
    adapter_dir.mkdir()
    log = tmp_path / "adapter.log"
    git = adapter_dir / "git"
    git.write_text(
        "#!/bin/sh\n"
        'printf \'git %s\\n\' "$*" >> "$ADAPTER_LOG"\n'
        "if [ \"$1\" = '-C' ]; then shift 2; fi\n"
        "if [ \"$1 $2\" = 'rev-parse --show-toplevel' ]; then printf '%s\\n' \"$FAKE_ROOT\"; exit 0; fi\n"
        "if [ \"$1 $2 $3\" = 'symbolic-ref --quiet --short' ]; then printf '%s\\n' develop; exit 0; fi\n"
        "if [ \"$1 $2 $3\" = 'remote get-url origin' ]; then printf '%s\\n' https://github.com/DIII-SDU-Group/III-Drone-ros2-ws.git; exit 0; fi\n"
        'if [ "$1 $2 $3" = \'ls-remote --heads origin\' ]; then case "$4" in *main) printf \'%040d  %s\\n\' 1 "$4";; *release) printf \'%040d  %s\\n\' 2 "$4";; esac; exit 0; fi\n'
        "exit 97\n",
        encoding="utf-8",
    )
    gh = adapter_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'printf \'gh %s\\n\' "$*" >> "$ADAPTER_LOG"\n'
        "if [ \"$1 $2\" = 'auth status' ]; then exit 0; fi\n"
        "if [ \"$1 $2\" = 'repo view' ]; then printf '%s\\n' DIII-SDU-Group/III-Drone-ros2-ws; exit 0; fi\n"
        "if [ \"$1 $2\" = 'pr list' ]; then [ -f \"$ADAPTER_STATE\" ] && printf '%s\\n' https://example.invalid/pull/1; exit 0; fi\n"
        "if [ \"$1 $2\" = 'pr create' ]; then : > \"$ADAPTER_STATE\"; printf '%s\\n' https://example.invalid/pull/1; exit 0; fi\n"
        "if [ \"$1 $2\" = 'pr edit' ]; then exit 0; fi\n"
        "exit 98\n",
        encoding="utf-8",
    )
    git.chmod(0o755)
    gh.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{adapter_dir}:{env['PATH']}",
            "ADAPTER_LOG": str(log),
            "ADAPTER_STATE": str(tmp_path / "adapter.state"),
            "FAKE_ROOT": str(ROOT),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        }
    )
    return env, log


def _run(
    script: str, *arguments: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPTS / script), *arguments],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_promotion_shell_scripts_parse(tmp_path: Path) -> None:
    for script in (
        "create_stack_prs.sh",
        "push_stack.sh",
        "refresh_workspace_submodule_pointers.sh",
        "create_develop_to_main_prs.sh",
        "create_main_to_release_pr.sh",
    ):
        subprocess.run(["bash", "-n", str(SCRIPTS / script)], check=True)
    compile_environment = os.environ.copy()
    compile_environment["PYTHONPYCACHEPREFIX"] = str(tmp_path / "pycache")
    subprocess.run(
        ["python3", "-m", "py_compile", str(SCRIPTS / "create_stack_plan.py")],
        check=True,
        env=compile_environment,
    )
    subprocess.run(
        [
            "python3",
            "-m",
            "py_compile",
            str(SCRIPTS / "create_release_pr_plan.py"),
        ],
        check=True,
        env=compile_environment,
    )


def test_develop_to_main_prepare_is_dry_run_with_fixed_namespace(
    tmp_path: Path,
) -> None:
    env, log = _fake_adapters(tmp_path)
    process = _run(
        "create_develop_to_main_prs.sh",
        "--promotion-id",
        "qual-2026-08",
        env=env,
    )
    assert process.returncode == 0, process.stderr
    assert "promote/develop-to-main/qual-2026-08" in process.stdout
    assert "create_stack_prs.sh --base main" in process.stdout
    assert "DRY-RUN complete" in process.stdout
    calls = log.read_text(encoding="utf-8")
    assert calls.count("\n") == 1
    assert "gh " not in calls


def test_develop_to_main_refresh_plan_is_idempotent_and_audited(tmp_path: Path) -> None:
    env, log = _fake_adapters(tmp_path)
    process = _run(
        "create_develop_to_main_prs.sh",
        "--promotion-id",
        "qual-2026-08",
        "--phase",
        "refresh",
        env=env,
    )
    assert process.returncode == 0, process.stderr
    assert "refresh_workspace_submodule_pointers.sh --base main" in process.stdout
    assert "verify_submodule_lock.sh" in process.stdout
    assert "verify_promotion_source.py" in process.stdout
    assert "git commit" in process.stdout and "git push" in process.stdout
    assert "gh " not in log.read_text(encoding="utf-8")

    rerun = _run(
        "create_develop_to_main_prs.sh",
        "--promotion-id",
        "qual-2026-08",
        "--phase",
        "refresh",
        env=env,
    )
    assert rerun.returncode == 0
    assert rerun.stdout == process.stdout


def test_main_to_release_is_workspace_only_and_dry_run(tmp_path: Path) -> None:
    env, log = _fake_adapters(tmp_path)
    process = _run("create_main_to_release_pr.sh", env=env)
    assert process.returncode == 0, process.stderr
    assert "Source branch: main" in process.stdout
    assert "Target branch: release" in process.stdout
    assert "Submodule release branches: prohibited" in process.stdout
    assert "Retained automation plan:" in process.stdout
    assert "gh " not in log.read_text(encoding="utf-8")


def test_main_to_release_apply_upserts_instead_of_duplicating(tmp_path: Path) -> None:
    env, log = _fake_adapters(tmp_path)
    planned = _run("create_main_to_release_pr.sh", env=env)
    assert planned.returncode == 0, planned.stderr
    first = _run("create_main_to_release_pr.sh", "--yes", env=env)
    second = _run("create_main_to_release_pr.sh", "--yes", env=env)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    calls = log.read_text(encoding="utf-8")
    assert calls.count("gh pr create") == 1
    assert calls.count("gh pr edit") == 1


def test_main_stack_rejects_non_promotion_branch_before_github(tmp_path: Path) -> None:
    env, log = _fake_adapters(tmp_path)
    process = _run(
        "create_stack_prs.sh",
        "--base",
        "main",
        "--feature",
        "feature/wrong",
        env=env,
    )
    assert process.returncode != 0
    assert "promote/develop-to-main/<id>" in process.stderr
    assert "gh " not in log.read_text(encoding="utf-8")


def test_pointer_refresh_rejects_ambiguous_main_branch(tmp_path: Path) -> None:
    env, _log = _fake_adapters(tmp_path)
    process = _run(
        "refresh_workspace_submodule_pointers.sh",
        "--base",
        "main",
        "--feature",
        "release/develop-to-main-old",
        env=env,
    )
    assert process.returncode != 0
    assert "promote/develop-to-main/<id>" in process.stderr


def test_legacy_promotion_namespace_and_staging_branch_are_absent() -> None:
    governed = [
        ROOT / "README.md",
        ROOT / "docs/dependency-governance.md",
        ROOT / "docs/repo-boundary-map.md",
        SCRIPTS / "create_develop_to_main_prs.sh",
        SCRIPTS / "create_stack_prs.sh",
        SCRIPTS / "push_stack.sh",
        ROOT / ".github/workflows/refresh-submodule-pointers.yml",
    ]
    content = "\n".join(path.read_text(encoding="utf-8") for path in governed)
    assert "release/develop-to-main" not in content
    assert "Workspace `staging`" not in content


def test_stack_pr_flow_pushes_exact_local_head_when_remote_feature_is_stale() -> None:
    source = (SCRIPTS / "create_stack_prs.sh").read_text(encoding="utf-8")
    assert 'remote_feature_sha="$(git -C "$p" ls-remote' in source
    assert '[[ "$remote_feature_sha" != "$local_feature_sha" ]]' in source
    assert '"$local_feature_sha:refs/heads/$feature_branch"' in source
    assert "iii-pr-transport-v1" in source
    assert "Operation ID:" in source
    assert "create_stack_plan.py" in source
    assert "--verify-existing" in source


def test_stack_pr_rejects_invalid_operation_id_before_github(tmp_path: Path) -> None:
    env, log = _fake_adapters(tmp_path)
    process = _run(
        "create_stack_prs.sh",
        "--base",
        "develop",
        "--feature",
        "deployment-redesign",
        "--operation-id",
        "BAD",
        env=env,
    )
    assert process.returncode != 0
    assert "--operation-id must match" in process.stderr
    assert "gh " not in log.read_text(encoding="utf-8")
