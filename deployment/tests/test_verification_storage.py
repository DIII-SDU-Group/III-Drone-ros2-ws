from __future__ import annotations

from pathlib import Path

import pytest

from iii_deployment.verification.storage import write_bytes_exclusive_atomic


def test_immutable_write_publishes_complete_bytes_and_refuses_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evidence/record.json"
    write_bytes_exclusive_atomic(path, b'{"complete":true}\n')
    assert path.read_bytes() == b'{"complete":true}\n'
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(path.parent.glob(".*.tmp")) == []

    with pytest.raises(FileExistsError):
        write_bytes_exclusive_atomic(path, b"replacement")
    assert path.read_bytes() == b'{"complete":true}\n'


def test_immutable_write_refuses_symlink_destination(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("preserve", encoding="utf-8")
    path = tmp_path / "record"
    path.symlink_to(target)
    with pytest.raises(FileExistsError):
        write_bytes_exclusive_atomic(path, b"replacement")
    assert target.read_text(encoding="utf-8") == "preserve"
