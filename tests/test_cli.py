from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
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


def test_wrap_cli_compacts_output_and_preserves_exit_code() -> None:
    script = (
        "import sys\n"
        "for index in range(100):\n"
        "    print(f'line {index:03d}')\n"
        "print('warning: noisy', file=sys.stderr)\n"
        "raise SystemExit(3)\n"
    )

    proc = run_cli(
        "wrap",
        "--command",
        "pytest",
        "--max-chars",
        "160",
        "--",
        sys.executable,
        "-c",
        script,
    )

    assert proc.returncode == 3
    assert "[noisegate: omitted" in proc.stdout
    assert "[noisegate: exit_code=3]" in proc.stdout
    assert "warning: noisy" in proc.stdout


def test_wrap_cli_raw_bypass_returns_captured_output_unchanged() -> None:
    script = "print('usage: cmd')\nprint('flag')\n"

    proc = run_cli(
        "wrap",
        "--raw",
        "--max-chars",
        "4",
        "--",
        sys.executable,
        "-c",
        script,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "usage: cmd\nflag\n"


def test_wrap_cli_full_is_raw_bypass_alias() -> None:
    script = "print('usage: cmd')\nprint('flag')\n"

    proc = run_cli(
        "wrap",
        "--full",
        "--max-chars",
        "4",
        "--",
        sys.executable,
        "-c",
        script,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "usage: cmd\nflag\n"


def test_wrap_cli_preserves_stdout_stderr_arrival_order() -> None:
    script = (
        "import sys, time\n"
        "sys.stdout.write('out-1\\n'); sys.stdout.flush(); time.sleep(0.02)\n"
        "sys.stderr.write('err-1\\n'); sys.stderr.flush(); time.sleep(0.02)\n"
        "sys.stdout.write('out-2\\n'); sys.stdout.flush(); time.sleep(0.02)\n"
        "sys.stderr.write('err-2\\n'); sys.stderr.flush()\n"
    )

    proc = run_cli(
        "wrap",
        "--raw",
        "--",
        sys.executable,
        "-c",
        script,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "out-1\nerr-1\nout-2\nerr-2\n"


def test_wrap_cli_preserves_fast_stdout_stderr_write_order() -> None:
    script = (
        "import sys\n"
        "sys.stdout.write('out-1\\n'); sys.stdout.flush()\n"
        "sys.stderr.write('err-1\\n'); sys.stderr.flush()\n"
        "sys.stdout.write('out-2\\n'); sys.stdout.flush()\n"
        "sys.stderr.write('err-2\\n'); sys.stderr.flush()\n"
    )

    proc = run_cli(
        "wrap",
        "--raw",
        "--",
        sys.executable,
        "-c",
        script,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "out-1\nerr-1\nout-2\nerr-2\n"


def test_wrap_cli_limits_captured_output() -> None:
    script = "import sys\nsys.stdout.write('a' * 2000)\n"

    proc = run_cli(
        "wrap",
        "--raw",
        "--max-capture-bytes",
        "128",
        "--",
        sys.executable,
        "-c",
        script,
    )

    assert proc.returncode == 0, proc.stderr
    assert len(proc.stdout) < 300
    assert "[noisegate: capture truncated" in proc.stdout


def test_wrap_cli_store_artifact_prints_recovery_notice(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    script = "for index in range(100): print(f'line {index:03d}')\n"

    proc = run_cli(
        "wrap",
        "--command",
        "pytest",
        "--max-chars",
        "160",
        "--store-artifact",
        "--artifact-dir",
        str(artifact_dir),
        "--",
        sys.executable,
        "-c",
        script,
    )

    assert proc.returncode == 0, proc.stderr
    assert "[noisegate artifact: id=ng_" in proc.stdout
    marker = "id=ng_"
    start = proc.stdout.index(marker) + len("id=")
    artifact_id = proc.stdout[start:].split(";", 1)[0]

    cat_proc = run_cli("cat", "--artifact-dir", str(artifact_dir), artifact_id)

    assert cat_proc.returncode == 0, cat_proc.stderr
    assert cat_proc.stdout.startswith("line 000\nline 001\n")
    assert cat_proc.stdout.endswith("line 099\n")


def test_wrap_cli_preserves_exact_file_reads(tmp_path: Path) -> None:
    path = tmp_path / "exact.txt"
    raw = numbered("exact line", 100) + "\n"
    path.write_text(raw)

    proc = run_cli(
        "wrap",
        "--max-chars",
        "120",
        "--",
        "cat",
        str(path),
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_wrap_cli_does_not_hang_when_descendant_keeps_pipe_open() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "noisegate.cli",
            "wrap",
            "--raw",
            "--",
            "sh",
            "-c",
            "sleep 5 &",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=2,
    )

    assert proc.returncode == 0, proc.stderr


def test_wrap_cli_does_not_hang_when_detached_descendant_keeps_pipe_open() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "noisegate.cli",
            "wrap",
            "--raw",
            "--",
            "sh",
            "-c",
            "setsid sleep 5 &",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=2,
    )

    assert proc.returncode == 0, proc.stderr


def test_wrap_cli_cleans_up_child_process_group_on_interrupt(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "sleeper.py"
    script.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "noisegate.cli",
            "wrap",
            "--raw",
            "--",
            sys.executable,
            str(script),
            str(pid_file),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 3
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pid_file.exists()
    child_pid = int(pid_file.read_text())

    proc.send_signal(signal.SIGINT)
    stdout, stderr = proc.communicate(timeout=3)

    assert proc.returncode == 130, stderr
    assert stdout == ""
    _assert_process_exited(child_pid)


def test_wrap_cli_kills_sigterm_ignoring_descendant_on_interrupt(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "ignore_term.py"
    script.write_text(
        "import os, signal, sys, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "noisegate.cli",
            "wrap",
            "--raw",
            "--",
            "sh",
            "-c",
            f"{sh_quote(sys.executable)} {sh_quote(str(script))} {sh_quote(str(pid_file))} & wait",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 3
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pid_file.exists()
    grandchild_pid = int(pid_file.read_text())

    proc.send_signal(signal.SIGINT)
    stdout, stderr = proc.communicate(timeout=3)

    assert proc.returncode == 130, stderr
    assert stdout == ""
    _assert_process_exited(grandchild_pid)


def _assert_process_exited(pid: int) -> None:
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    raise AssertionError(f"process still alive: {pid}")


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


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


def test_cat_cli_rejects_non_regular_artifact_nodes_without_hanging(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "ng_000000000000000000000000.txt").mkdir()

    cat_proc = run_cli("cat", "--artifact-dir", str(artifact_dir), "ng_000000000000000000000000")

    assert cat_proc.returncode == 2
    assert "regular file" in cat_proc.stderr


def test_artifacts_cli_lists_and_summarizes_private_store(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    reduce_proc = run_cli(
        "reduce",
        "--command",
        "pytest",
        "--max-chars",
        "160",
        "--store-artifact",
        "--artifact-dir",
        str(artifact_dir),
        input_text=numbered("line", 100),
    )
    assert reduce_proc.returncode == 0, reduce_proc.stderr
    marker = "id=ng_"
    start = reduce_proc.stdout.index(marker) + len("id=")
    artifact_id = reduce_proc.stdout[start:].split(";", 1)[0]

    list_proc = run_cli("artifacts", "list", "--artifact-dir", str(artifact_dir))
    stats_proc = run_cli("artifacts", "stats", "--artifact-dir", str(artifact_dir))

    assert list_proc.returncode == 0, list_proc.stderr
    assert artifact_id in list_proc.stdout
    assert "size_bytes=" in list_proc.stdout
    assert stats_proc.returncode == 0, stats_proc.stderr
    assert "artifacts: 1" in stats_proc.stdout
    assert "total_size_bytes:" in stats_proc.stdout


def test_artifacts_cli_verify_detects_tampering(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    reduce_proc = run_cli(
        "reduce",
        "--command",
        "pytest",
        "--max-chars",
        "160",
        "--store-artifact",
        "--artifact-dir",
        str(artifact_dir),
        input_text=numbered("line", 100),
    )
    assert reduce_proc.returncode == 0, reduce_proc.stderr
    marker = "id=ng_"
    start = reduce_proc.stdout.index(marker) + len("id=")
    artifact_id = reduce_proc.stdout[start:].split(";", 1)[0]
    (artifact_dir / f"{artifact_id}.txt").write_text("tampered")

    verify_proc = run_cli("artifacts", "verify", "--artifact-dir", str(artifact_dir))

    assert verify_proc.returncode == 2
    assert artifact_id in verify_proc.stdout
    assert "sha_mismatch" in verify_proc.stdout


def test_artifacts_cli_verify_rejects_non_regular_nodes_without_hanging(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "ng_000000000000000000000000.txt").mkdir()

    verify_proc = run_cli("artifacts", "verify", "--artifact-dir", str(artifact_dir))

    assert verify_proc.returncode == 2
    assert "non_regular" in verify_proc.stdout


def test_artifacts_cli_verify_rejects_oversized_nodes_without_reading(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "ng_000000000000000000000000.txt").write_text("x" * 1_000_001)

    verify_proc = run_cli("artifacts", "verify", "--artifact-dir", str(artifact_dir))

    assert verify_proc.returncode == 2
    assert "too_large" in verify_proc.stdout
