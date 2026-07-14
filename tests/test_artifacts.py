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
from noisegate.engine import (
    NoisegateOptions,
    _looks_secret_bearing_text,
    _strip_terminal_escape_sequences,
    reduce_text,
)


def numbered(prefix: str, count: int) -> str:
    return "\n".join(f"{prefix} {index:03d}" for index in range(1, count + 1))


def test_terminal_escape_normalization_handles_repeated_unterminated_osc_linearly() -> None:
    malformed = "\x1b]A" * 10_000

    assert not _strip_terminal_escape_sequences(malformed, preserve_osc_payload=False)


def test_secret_classifier_handles_keyword_heavy_nonmatch_in_bounded_time() -> None:
    keyword_heavy = "A" + "TOKEN" * 4_000

    started = time.perf_counter()
    assert not _looks_secret_bearing_text(keyword_heavy)

    assert time.perf_counter() - started < 1.0


def test_secret_classifier_handles_uri_like_nonmatch_in_bounded_time() -> None:
    uri_like = "a." * 16_000

    started = time.perf_counter()
    assert not _looks_secret_bearing_text(uri_like)

    assert time.perf_counter() - started < 1.0


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_artifact_store_env_rejects_negative_and_invalid_size_caps(monkeypatch) -> None:
    monkeypatch.setenv("NOISEGATE_ARTIFACT_SIZE_CAP", "-1")
    assert ArtifactStore.from_env().size_cap == DEFAULT_SIZE_CAP

    monkeypatch.setenv("NOISEGATE_ARTIFACT_SIZE_CAP", "not-an-int")
    assert ArtifactStore.from_env().size_cap == DEFAULT_SIZE_CAP

    monkeypatch.setenv("NOISEGATE_ARTIFACT_SIZE_CAP", "123")
    assert ArtifactStore.from_env().size_cap == 123


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


def test_artifact_store_refuses_permissive_existing_root_without_chmod(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)

    store = ArtifactStore(root, size_cap=1024)

    with pytest.raises(ArtifactSecurityError, match="owner-only"):
        store.store("raw terminal output")
    assert mode(root) == 0o755


def test_reduce_text_records_artifact_metadata_when_enabled(tmp_path: Path) -> None:
    raw = numbered("line", 100)
    options = NoisegateOptions(
        max_chars=220,
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
    assert f"noisegate cat --artifact-dir {tmp_path / 'store'} {artifact['id']}" in result.text
    assert ArtifactStore(tmp_path / "store").read(str(artifact["id"])) == raw


def test_reduce_text_refuses_secret_bearing_artifacts(tmp_path: Path) -> None:
    raw = "\n".join(
        [
            *[f"startup log {index:03d}" for index in range(80)],
            "EXAMPLE_" + "TOKEN=not-a-real-value",
            "Authorization: " + "Bearer " + "not-a-real-value",
            *[f"tail log {index:03d}" for index in range(80)],
        ]
    )
    options = NoisegateOptions(
        max_chars=260,
        artifact_enabled=True,
        artifact_dir=tmp_path / "store",
    )

    result = reduce_text(raw, command="make noisy", options=options)

    assert result.changed is True
    assert result.metadata["artifact"] == {
        "stored": False,
        "reason": "secret_detected",
        "size_bytes": len(raw.encode("utf-8")),
    }
    assert "id=ng_" not in result.text
    assert not (tmp_path / "store").exists()


def test_reduce_text_scans_full_artifact_for_late_secret(tmp_path: Path) -> None:
    raw = "\n".join(
        [
            "filler " + ("x" * 210_000),
            '"x-api-key": "not-a-real-value"',
            numbered("tail", 80),
        ]
    )
    options = NoisegateOptions(
        max_chars=260,
        artifact_enabled=True,
        artifact_dir=tmp_path / "store",
        artifact_size_cap=len(raw.encode("utf-8")) + 100,
    )

    result = reduce_text(raw, command="make noisy", options=options)

    assert result.changed is True
    assert result.metadata["artifact"] == {
        "stored": False,
        "reason": "secret_detected",
        "size_bytes": len(raw.encode("utf-8")),
    }
    assert not (tmp_path / "store").exists()


@pytest.mark.parametrize(
    "secret_line",
    [
        "token: not-a-real-value",
        '"credentials": "not-a-real-value"',
        "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
        "-----BEGIN " + "PGP PRIVATE KEY BLOCK-----",
        "    -----BEGIN " + "OPENSSH PRIVATE KEY-----",
        "> -----BEGIN " + "PGP PRIVATE KEY BLOCK-----",
        "'-----BEGIN " + "RSA PRIVATE KEY-----",
        "'-----BEGIN " + "RSA PRIVATE KEY-----'",
        '"-----BEGIN ' + 'OPENSSH PRIVATE KEY-----"',
        ">-----BEGIN " + "PGP PRIVATE KEY BLOCK-----",
        "1. -----BEGIN " + "EC PRIVATE KEY-----",
        "-----BEGIN " + "OPENSSH PRIVATE KEY-----\r\nbody",
        "X-Amz-Security-Token: not-a-real-value",
        "GITHUB_TOKEN: not-a-real-value",
        '"AWS_SECRET_ACCESS_KEY": "not-a-real-value"',
        "> Cookie: not-a-real-value",
        "HTTP_AUTHORIZATION=Basic not-a-real-value",
        "Cookie=sessionid=not-a-real-value",
        "--password not-a-real-value",
        "password not-a-real-value",
        "Author\x1b[31mization: Basic not-a-real-value",
        "\x1b]0;Authorization: Bearer not-a-real-value\x07visible output",
        "Author\x1b]0;ignored\x07ization: Basic not-a-real-value",
        "curl --user alice:not-a-real-value https://example.test",
        "client --auth not-a-real-value",
        "postgresql://alice:not-a-real-value@example.test/db",
        "redis://:not-a-real-value@example.test/0",
        "https://not-a-real-token@example.test/path",
        "MYSQL_PWD=not-a-real-value",
        "REDISCLI_AUTH=not-a-real-value",
        "DB_PASS=not-a-real-value",
        "Author\x9b31mization: Basic not-a-real-value",
        "Author\x1bPignored\x1b\\ization: Basic not-a-real-value",
        "AuthorizX\bation: Basic not-a-real-value",
        "X_API_TOKX\bEN=not-a-real-value",
        "Authoriz\x00ation: Basic not-a-real-value",
        "\x1b[31mordinary colored output\x1b[0m",
        "\x1b]52;c;bm90LWEtcmVhbC12YWx1ZQ==\x07",
        '<input type="password" value="not-a-real-value">',
        '<input value="not-a-real-value" type=password>',
        'Content-Disposition: form-data; name="password"\r\n\r\nnot-a-real-value',
        'Content-Disposition: form-data; name=client_secret\r\n\r\nnot-a-real-value',
        'Content-Disposition: form-data; name="user[password]"\r\n\r\nnot-a-real-value',
        '<input name="client_secret" value="not-a-real-value">',
        '<input value="not-a-real-value" name=refresh_token>',
    ],
)
def test_reduce_text_refuses_additional_secret_artifact_shapes(
    tmp_path: Path,
    secret_line: str,
) -> None:
    raw = "\n".join([numbered("before", 80), secret_line, numbered("after", 80)])
    options = NoisegateOptions(
        max_chars=260,
        artifact_enabled=True,
        artifact_dir=tmp_path / "store",
    )

    result = reduce_text(raw, command="make noisy", options=options)

    assert result.changed is True
    assert result.metadata["artifact"] == {
        "stored": False,
        "reason": "secret_detected",
        "size_bytes": len(raw.encode("utf-8")),
    }
    assert not (tmp_path / "store").exists()


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
