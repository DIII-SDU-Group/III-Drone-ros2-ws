from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "deployment/scripts/bootstrap_gc_controller.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_gc_controller", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)
VERIFY_SPEC = importlib.util.spec_from_file_location(
    "verify_python_lock", ROOT / "deployment/scripts/verify_python_lock.py"
)
assert VERIFY_SPEC is not None and VERIFY_SPEC.loader is not None
verify_lock = importlib.util.module_from_spec(VERIFY_SPEC)
VERIFY_SPEC.loader.exec_module(verify_lock)
INSTALL_SPEC = importlib.util.spec_from_file_location(
    "install_local_python_sources",
    ROOT / "deployment/scripts/install_local_python_sources.py",
)
assert INSTALL_SPEC is not None and INSTALL_SPEC.loader is not None
install_sources = importlib.util.module_from_spec(INSTALL_SPEC)
INSTALL_SPEC.loader.exec_module(install_sources)


def _cache(root: Path) -> tuple[Path, dict]:
    root.mkdir()
    artifacts = []
    for role in sorted(bootstrap.OFFLINE_ROLES):
        path = root / f"{role}.tar"
        path.write_bytes((role + "\n").encode())
        artifacts.append(
            {
                "role": role,
                "path": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
            }
        )
    value = {
        "schema": bootstrap.OFFLINE_SCHEMA,
        "platform_id": "ubuntu-24.04-x86_64",
        "created_at": "2026-08-27T00:00:00Z",
        "artifacts": artifacts,
    }
    value["cache_id"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    (root / "gc-offline-cache.json").write_text(
        json.dumps(value, sort_keys=True) + "\n", encoding="utf-8"
    )
    return root, value


def test_bootstrap_authenticates_entire_offline_cache_before_selection(
    tmp_path, monkeypatch
):
    root, value = _cache(tmp_path / "cache")
    monkeypatch.setattr(bootstrap, "_platform_id", lambda: "ubuntu-24.04-x86_64")

    assert bootstrap._offline_manifest(root) == value

    (root / "gc-runtime-wheelhouse.tar").write_text("changed\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="gc-runtime-wheelhouse"):
        bootstrap._offline_manifest(root)


def test_bootstrap_rejects_cache_identity_platform_and_role_ambiguity(
    tmp_path, monkeypatch
):
    root, value = _cache(tmp_path / "cache")
    monkeypatch.setattr(bootstrap, "_platform_id", lambda: "ubuntu-22.04-x86_64")
    with pytest.raises(SystemExit, match="platform"):
        bootstrap._offline_manifest(root)

    monkeypatch.setattr(bootstrap, "_platform_id", lambda: "ubuntu-24.04-x86_64")
    value["cache_id"] = "0" * 64
    (root / "gc-offline-cache.json").write_text(json.dumps(value) + "\n")
    with pytest.raises(SystemExit, match="identity"):
        bootstrap._offline_manifest(root)

    _, value = _cache(tmp_path / "duplicate-cache")
    duplicate_root = tmp_path / "duplicate-cache"
    value["artifacts"][-1]["role"] = value["artifacts"][0]["role"]
    unsigned = {key: item for key, item in value.items() if key != "cache_id"}
    value["cache_id"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    (duplicate_root / "gc-offline-cache.json").write_text(json.dumps(value) + "\n")
    with pytest.raises(SystemExit, match="role"):
        bootstrap._offline_manifest(duplicate_root)


def test_bootstrap_safe_extract_refuses_links_and_path_escape(tmp_path):
    archive = tmp_path / "unsafe.tar"
    with tarfile.open(archive, "w") as stream:
        payload = b"escape\n"
        info = tarfile.TarInfo("../escape")
        info.size = len(payload)
        stream.addfile(info, io.BytesIO(payload))

    with pytest.raises(SystemExit, match="unsafe"):
        bootstrap._safe_extract(archive, tmp_path / "destination")


def test_controller_selection_never_relocates_generated_entry_points(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "tools/III-Drone-CLI").mkdir(parents=True)
    (workspace / "tools/III-Drone-CLI/pyproject.toml").write_text(
        "[build-system]\n", encoding="utf-8"
    )
    (workspace / "deployment").mkdir()
    (workspace / "deployment/pyproject.toml").write_text(
        "[build-system]\n", encoding="utf-8"
    )
    lock = workspace / "lock.txt"
    lock.write_text("setuptools==80.9.0 --hash=sha256:" + "a" * 64 + "\n")
    wheel = tmp_path / bootstrap.PIP_WHEEL
    wheel.write_bytes(b"authenticated-test-wheel")
    identity = "b" * 64
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(bootstrap, "_workspace", lambda: workspace)
    monkeypatch.setattr(bootstrap, "_identity", lambda _root: identity)
    monkeypatch.setattr(bootstrap, "_controller_lock", lambda _root: lock)
    monkeypatch.setattr(bootstrap, "_download_pip_wheel", lambda _root: wheel)

    def create(destination, *, pip_wheel):
        assert destination.name == identity
        assert pip_wheel == wheel
        binary = destination / "bin"
        binary.mkdir(parents=True)
        for name in ("pip", "iii", "ansible-playbook"):
            path = binary / name
            path.write_text(f"#!{destination}/bin/python\n")
        return binary / "pip"

    monkeypatch.setattr(bootstrap, "_create_environment", create)
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )

    selected = bootstrap.bootstrap(None)

    selector = home / ".local/share/iii/controller/venv"
    assert selected == home / ".local/share/iii/controller/environments" / identity
    assert selector.is_symlink()
    assert selector.resolve() == selected
    assert str(selected) in (selected / "bin/iii").read_text()
    assert not list(selected.parent.glob("*.partial"))


def test_controller_subprocess_environment_excludes_host_python_and_pip_state(
    monkeypatch,
):
    monkeypatch.setenv("PYTHONPATH", "/untrusted/source")
    monkeypatch.setenv("PYTHONHOME", "/untrusted/python")
    monkeypatch.setenv("VIRTUAL_ENV", "/untrusted/venv")
    monkeypatch.setenv("PIP_INDEX_URL", "https://untrusted.invalid/simple")

    environment = bootstrap._clean_environment()

    assert "PYTHONPATH" not in environment
    assert "PYTHONHOME" not in environment
    assert "VIRTUAL_ENV" not in environment
    assert "PIP_INDEX_URL" not in environment
    assert environment["PIP_CONFIG_FILE"] == "/dev/null"


def test_local_source_copy_excludes_generated_content_and_refuses_links(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    (source / "build").mkdir()
    (source / "build/generated.txt").write_text("stale\n", encoding="utf-8")
    destination = tmp_path / "copied"

    install_sources._copy_source(source, destination)

    assert (destination / "pyproject.toml").is_file()
    assert not (destination / "build").exists()

    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "escape").symlink_to(source / "pyproject.toml")
    with pytest.raises(ValueError, match="contains a link"):
        install_sources._copy_source(linked, tmp_path / "linked-copy")


def test_exact_environment_verifier_rejects_extra_missing_and_changed_packages(
    tmp_path, monkeypatch
):
    lock = tmp_path / "requirements.txt"
    lock.write_text(
        "alpha_pkg==1.2.3 \\\n+    --hash=sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )

    def distributions(values):
        return [
            SimpleNamespace(metadata={"Name": name}, version=version)
            for name, version in values
        ]

    monkeypatch.setattr(
        verify_lock.metadata,
        "distributions",
        lambda: distributions([("alpha-pkg", "1.2.3"), ("local_pkg", "2.0")]),
    )
    verify_lock.verify(lock, ["local-pkg==2.0"])

    monkeypatch.setattr(
        verify_lock.metadata,
        "distributions",
        lambda: distributions(
            [("alpha-pkg", "9.9"), ("local-pkg", "2.0"), ("extra", "1")]
        ),
    )
    with pytest.raises(ValueError, match="unexpected=extra.*version=alpha-pkg"):
        verify_lock.verify(lock, ["local-pkg==2.0", "missing==1.0"])
