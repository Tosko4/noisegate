from __future__ import annotations

import os
import stat
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from noisegate.artifacts import (
    DEFAULT_SIZE_CAP,
    ArtifactSecurityError,
    ArtifactStore,
    ArtifactTooLarge,
)
from noisegate.engine import NoisegateOptions, reduce_text


def numbered(prefix: str, count: int) -> str:
    return "\n".join(f"{prefix} {index:03d}" for index in range(1, count + 1))


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_artifacts_are_disabled_by_default(tmp_path: Path) -> None:
    raw = numbered("line", 100)
    options = NoisegateOptions(max_chars=120, artifact_dir=tmp_path / "artifacts")

    result = reduce_text(raw, command="pytest", options=options)

    assert result.changed is True
    assert "artifact" not in result.metadata
    assert not (tmp_path / "artifacts").exists()


def test_negative_artifact_size_cap_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOISEGATE_ARTIFACT_SIZE_CAP", "-1")

    assert NoisegateOptions.from_env().artifact_size_cap == DEFAULT_SIZE_CAP


def test_artifact_store_uses_private_directory_and_file_modes(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store", size_cap=1024)

    artifact = store.store("raw terminal output")

    assert artifact.artifact_id.startswith("ng_")
    assert artifact.sha256
    assert mode(tmp_path / "store") == 0o700
    assert mode(tmp_path / "store" / f"{artifact.artifact_id}.txt") == 0o600
    assert store.read(artifact.artifact_id) == "raw terminal output"


def test_reduce_text_records_artifact_metadata_when_enabled(tmp_path: Path) -> None:
    raw = numbered("line", 100)
    options = NoisegateOptions(
        max_chars=120,
        artifact_enabled=True,
        artifact_dir=tmp_path / "store",
    )

    result = reduce_text(raw, command="pytest", options=options)

    artifact = result.metadata["artifact"]
    assert isinstance(artifact, dict)
    assert artifact["id"].startswith("ng_")
    assert artifact["sha256"]
    assert str(artifact["id"]) in result.text
    assert str(artifact["sha256"])[:16] in result.text
    assert ArtifactStore(tmp_path / "store").read(str(artifact["id"])) == raw


def test_artifact_store_refuses_outputs_over_size_cap(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store", size_cap=4)

    with pytest.raises(ArtifactTooLarge):
        store.store("too large")


def test_reduce_text_reports_too_large_artifact_without_failing(tmp_path: Path) -> None:
    raw = numbered("line", 100)
    options = NoisegateOptions(
        max_chars=120,
        artifact_enabled=True,
        artifact_dir=tmp_path / "store",
        artifact_size_cap=10,
    )

    result = reduce_text(raw, command="pytest", options=options)

    assert result.changed is True
    assert result.metadata["artifact"] == {
        "stored": False,
        "reason": "too_large",
        "size_bytes": len(raw.encode("utf-8")),
        "size_cap": 10,
    }


def test_artifact_store_rejects_symlink_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)

    with pytest.raises(ArtifactSecurityError):
        ArtifactStore(link).store("raw")


def test_artifact_store_rejects_path_traversal_ids(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store")
    _ = store.store("raw")

    with pytest.raises(ArtifactSecurityError):
        store.read("../ng_bad")


def test_artifact_store_rejects_symlink_artifact_file(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store")
    artifact = store.store("raw")
    path = tmp_path / "store" / f"{artifact.artifact_id}.txt"
    path.unlink()
    os.symlink(tmp_path / "outside", path)

    with pytest.raises(ArtifactSecurityError):
        store.read(artifact.artifact_id)


def test_artifact_store_handles_concurrent_same_content_writes(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store", size_cap=10_000)
    raw = numbered("concurrent terminal output", 200)

    with ThreadPoolExecutor(max_workers=16) as executor:
        artifacts = list(executor.map(lambda _: store.store(raw), range(64)))

    artifact_ids = {artifact.artifact_id for artifact in artifacts}
    assert len(artifact_ids) == 1
    artifact_id = artifact_ids.pop()
    assert store.read(artifact_id) == raw
    assert mode(tmp_path / "store") == 0o700
    assert mode(tmp_path / "store" / f"{artifact_id}.txt") == 0o600
    assert len(list((tmp_path / "store").glob("ng_*.txt"))) == 1
    assert list((tmp_path / "store").glob(".ng_*.tmp")) == []


def test_artifact_store_reuses_existing_artifact_without_temp_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ArtifactStore(tmp_path / "store", size_cap=10_000)
    raw = "raw terminal output"
    first = store.store(raw)

    def fail_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        raise OSError("mkstemp should not be called for existing artifacts")

    monkeypatch.setattr(tempfile, "mkstemp", fail_mkstemp)

    second = store.store(raw)

    assert second == first
    assert store.read(second.artifact_id) == raw
    assert list((tmp_path / "store").glob(".ng_*.tmp")) == []

def test_artifact_verify_reports_temp_files_without_exposing_raw_content(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "store", size_cap=10_000)
    root = store._ensure_root()
    temp_path = root / f".ng_{'a' * 24}.leftover.tmp"
    temp_path.write_text("raw terminal output", encoding="utf-8")
    os.chmod(temp_path, 0o600)

    checks = store.verify()

    assert len(checks) == 1
    check = checks[0]
    assert check.ok is False
    assert check.reason == "temp_file"
    assert "raw terminal output" not in check.artifact_id
    assert "raw terminal output" not in check.path


def test_artifact_store_cleans_stale_temp_files_before_writes(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store", size_cap=10_000)
    root = store._ensure_root()
    temp_path = root / f".ng_{'a' * 24}.leftover.tmp"
    temp_path.write_text("stale raw terminal output", encoding="utf-8")
    os.chmod(temp_path, 0o600)
    stale_time = time.time() - 7_200
    os.utime(temp_path, (stale_time, stale_time))

    artifact = store.store("new raw output")

    assert store.read(artifact.artifact_id) == "new raw output"
    assert not temp_path.exists()
