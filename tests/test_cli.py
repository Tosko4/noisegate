from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def numbered(prefix: str, count: int) -> str:
    return "\n".join(f"{prefix} {index:03d}" for index in range(1, count + 1))


def run_cli(
    *args: str,
    input_text: str = "",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    full_env = os.environ.copy()
    full_env.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "noisegate.cli", *args],
        input=input_text,
        text=True,
        capture_output=True,
        env=full_env,
        check=False,
    )


def test_reduce_cli_compacts_stdin() -> None:
    proc = run_cli(
        "reduce",
        "--command",
        "pytest",
        "--max-chars",
        "120",
        input_text=numbered("line", 100),
    )

    assert proc.returncode == 0, proc.stderr
    assert "[noisegate: omitted" in proc.stdout
    assert "line 001" in proc.stdout
    assert "line 100" in proc.stdout


def test_reduce_cli_preserves_protected_tool_output() -> None:
    raw = numbered("exact line", 100)
    proc = run_cli(
        "reduce",
        "--tool",
        "read_file",
        "--max-chars",
        "120",
        input_text=raw,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_reduce_json_rewrites_result_field() -> None:
    envelope = {
        "tool_name": "terminal",
        "args": {"command": "pytest"},
        "result": json.dumps({"stdout": numbered("line", 100), "exit": 0}),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    outer = json.loads(proc.stdout)
    inner = json.loads(outer["result"])
    assert "[noisegate: omitted" in inner["stdout"]
    assert inner["noisegate"]["compacted"] is True


def test_reduce_json_rewrites_plain_result_string() -> None:
    envelope = {
        "tool_name": "terminal",
        "args": {"command": "pytest"},
        "result": numbered("line", 100),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    outer = json.loads(proc.stdout)
    assert "[noisegate: omitted" in outer["result"]
    assert "line 001" in outer["result"]
    assert "line 100" in outer["result"]


def test_reduce_json_bad_input_fails_open() -> None:
    raw = "{not json"

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0
    assert proc.stdout == raw


def test_reduce_json_preserves_direct_protected_tool_payload() -> None:
    payload = {
        "tool_name": "read_file",
        "content": numbered("exact line", 100),
        "noisegate": {"max_chars": 120},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_preserves_valid_json_envelope_when_inner_rewrite_has_no_gain() -> None:
    inner = json.dumps({"stdout": "A" * 4010})
    payload = {"tool_name": "terminal", "result": inner}

    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_returns_raw_envelope_when_disabled() -> None:
    raw = '{\n  "tool_name": "terminal",\n  "result": "short"\n}\n'

    proc = run_cli("reduce-json", input_text=raw, env={"NOISEGATE_DISABLE": "1"})

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_doctor_cli_reports_health(tmp_path: Path) -> None:
    proc = run_cli("doctor", env={"NOISEGATE_ARTIFACT_DIR": str(tmp_path / "artifacts")})

    assert proc.returncode == 0, proc.stderr
    assert "Noisegate doctor" in proc.stdout
    assert "package: ok" in proc.stdout
    assert "artifacts: disabled" in proc.stdout


def test_cat_cli_reads_artifact(tmp_path: Path) -> None:
    env = {
        "NOISEGATE_ARTIFACTS": "1",
        "NOISEGATE_ARTIFACT_DIR": str(tmp_path / "artifacts"),
    }
    reduce_proc = run_cli(
        "reduce-json",
        input_text=json.dumps(
            {
                "tool_name": "terminal",
                "args": {"command": "pytest"},
                "result": json.dumps({"stdout": numbered("line", 100), "exit": 0}),
                "noisegate": {"max_chars": 120},
            }
        ),
        env=env,
    )
    assert reduce_proc.returncode == 0, reduce_proc.stderr
    outer = json.loads(reduce_proc.stdout)
    inner = json.loads(outer["result"])
    artifact_id = inner["noisegate"]["fields"]["stdout"]["artifact"]["id"]
    assert artifact_id in inner["stdout"]

    cat_proc = run_cli("cat", artifact_id, env=env)

    assert cat_proc.returncode == 0, cat_proc.stderr
    assert cat_proc.stdout == numbered("line", 100)


def test_reduce_cli_store_artifact_prints_recovery_notice(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    proc = run_cli(
        "reduce",
        "--command",
        "pytest",
        "--max-chars",
        "120",
        "--store-artifact",
        "--artifact-dir",
        str(artifact_dir),
        input_text=numbered("line", 100),
    )

    assert proc.returncode == 0, proc.stderr
    assert "[noisegate artifact: id=ng_" in proc.stdout
    assert "sha256=" in proc.stdout
    assert len(proc.stdout) <= 120


def test_reduce_cli_does_not_store_artifact_when_recovery_notice_cannot_fit(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    proc = run_cli(
        "reduce",
        "--command",
        "pytest",
        "--max-chars",
        "40",
        "--store-artifact",
        "--artifact-dir",
        str(artifact_dir),
        input_text=numbered("line", 100),
    )

    assert proc.returncode == 0, proc.stderr
    assert "id=ng_" not in proc.stdout
    assert not artifact_dir.exists() or list(artifact_dir.iterdir()) == []


def test_cat_cli_accepts_custom_artifact_dir(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "custom-artifacts"
    reduce_proc = run_cli(
        "reduce",
        "--command",
        "pytest",
        "--max-chars",
        "120",
        "--store-artifact",
        "--artifact-dir",
        str(artifact_dir),
        input_text=numbered("line", 100),
    )
    assert reduce_proc.returncode == 0, reduce_proc.stderr
    marker = "id=ng_"
    start = reduce_proc.stdout.index(marker) + len("id=")
    artifact_id = reduce_proc.stdout[start:].split(";", 1)[0]

    cat_proc = run_cli("cat", "--artifact-dir", str(artifact_dir), artifact_id)

    assert cat_proc.returncode == 0, cat_proc.stderr
    assert cat_proc.stdout == numbered("line", 100)
