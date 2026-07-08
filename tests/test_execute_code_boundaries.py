from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from noisegate.plugin import transform_tool_result


def numbered(prefix: str, count: int) -> str:
    return "\n".join(f"{prefix} {index:03d}" for index in range(1, count + 1))


def run_cli(*args: str, input_text: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "noisegate.cli", *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def small_json_result() -> str:
    return json.dumps({"ok": True, "rows": [1, 2, 3]})


def huge_json_array() -> str:
    return json.dumps([{"row": index, "value": f"item-{index:04d}"} for index in range(350)])


def printed_source_code() -> str:
    return "\n".join(
        [
            "from __future__ import annotations",
            "",
            "def build_report(rows: list[dict[str, object]]) -> dict[str, object]:",
            "    return {",
            "        'status': 'ok',",
            "        'rows': rows,",
            "        'message': 'FAILED/ERROR are fixture strings, not a test log',",
            "    }",
            *[f"# exact source comment {index:03d}: ERROR FAILED" for index in range(120)],
        ]
    )


def printed_config() -> str:
    return "\n".join(
        [
            "[tool.noisegate]",
            "mode = \"auto\"",
            "max_chars = 4000",
            "preserve_diffs = true",
            *[
                f"fixture_option_{index:03d} = \"ERROR/FAILED literal\""
                for index in range(90)
            ],
        ]
    )


def printed_git_diff() -> str:
    return "\n".join(
        [
            "diff --git a/noisegate/example.py b/noisegate/example.py",
            "index 1111111..2222222 100644",
            "--- a/noisegate/example.py",
            "+++ b/noisegate/example.py",
            "@@ -1,3 +1,5 @@",
            " def example():",
            "-    return 'old'",
            "+    return 'new'",
            *[f"+    # added exact diff line {index:03d}" for index in range(90)],
        ]
    )


def printed_pytest_summary() -> str:
    return "\n".join(
        [
            *[f"tests/test_noise.py::test_ok_{index:03d} PASSED" for index in range(80)],
            "FAILED tests/test_execute_code.py::test_boundary - AssertionError: boom",
            "=========================== short test summary info ============================",
            "FAILED tests/test_execute_code.py::test_boundary - AssertionError: boom",
            "1 failed, 80 passed in 12.34s",
        ]
    )


def printed_web_extract_excerpt() -> str:
    return "\n".join(
        [
            "# Extracted page",
            "Source: https://example.invalid/docs/noisegate",
            "",
            *[
                f"Paragraph {index:03d}: exact quoted web context with value = {index}"
                for index in range(120)
            ],
        ]
    )


def printed_dependency_report() -> str:
    return "\n".join(
        [
            "Dependency report",
            "Package: noisegate-hermes",
            "ResolutionImpossible: conflicting dependencies detected",
            *[
                f"candidate package-{index:03d}=={index}.0.0 requires fixture"
                for index in range(120)
            ],
        ]
    )


def printed_apt_update_spam() -> str:
    return "\n".join(
        [
            *[
                "Get:"
                f"{index} http://archive.ubuntu.com/ubuntu resolute/main amd64 "
                f"pkg{index} [10 kB]"
                for index in range(120)
            ],
            "Fetched 12.3 MB in 3s (4,100 kB/s)",
            "Reading package lists... Done",
        ]
    )


def invalid_plain_text() -> str:
    return "this is not JSON\n" + numbered("plain text line", 120)


def execute_code_payloads() -> Iterable[tuple[str, str]]:
    return (
        ("small JSON result", small_json_result()),
        ("huge JSON array", huge_json_array()),
        ("printed source code", printed_source_code()),
        ("printed config", printed_config()),
        ("printed git diff", printed_git_diff()),
        ("printed pytest summary", printed_pytest_summary()),
        ("printed web_extract excerpts", printed_web_extract_excerpt()),
        ("printed parsed dependency report", printed_dependency_report()),
        ("printed apt update style spam", printed_apt_update_spam()),
        ("invalid/non-JSON plain text", invalid_plain_text()),
    )


def test_execute_code_hook_keeps_printed_payloads_raw_by_default() -> None:
    for label, printed in execute_code_payloads():
        raw = json.dumps({"output": printed, "exit_code": 0})

        transformed = transform_tool_result(
            raw,
            tool_name="execute_code",
            noisegate_max_chars=120,
            noisegate_mode="head_tail",
            noisegate_artifacts=True,
        )

        assert transformed is None, label


def test_execute_code_reduce_json_preserves_payloads_and_valid_json_by_default() -> None:
    for label, printed in execute_code_payloads():
        envelope = {
            "tool_name": "execute_code",
            "result": json.dumps({"output": printed, "exit_code": 0}),
            "noisegate": {"max_chars": 120, "mode": "head_tail"},
        }

        proc = run_cli("reduce-json", input_text=json.dumps(envelope))

        assert proc.returncode == 0, (label, proc.stderr)
        assert json.loads(proc.stdout) == envelope, label


def test_execute_code_direct_reduce_json_payload_is_preserved_by_default() -> None:
    payload = {
        "tool_name": "execute_code",
        "output": printed_apt_update_spam(),
        "exit_code": 0,
        "noisegate": {"max_chars": 120, "mode": "head_tail"},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_execute_code_invalid_reduce_json_input_fails_open_without_exception() -> None:
    raw = "not-json " + numbered("apt spam", 120)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0
    assert proc.stdout == raw
    assert proc.stderr == ""


def test_execute_code_mechanical_output_can_still_use_explicit_command_safe_route() -> None:
    pytest_output = printed_pytest_summary()
    envelope = {
        "tool_name": "terminal",
        "args": {"command": "pytest -q"},
        "result": json.dumps({"stdout": pytest_output, "exit_code": 1}),
        "noisegate": {"max_chars": 260, "head_lines": 2, "tail_lines": 2},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    outer = json.loads(proc.stdout)
    inner = json.loads(outer["result"])
    assert "FAILED tests/test_execute_code.py::test_boundary" in inner["stdout"]
    assert "[noisegate: omitted" in inner["stdout"]
    assert inner["noisegate"]["fields"]["stdout"]["reducer"] == "pytest"


def test_execute_code_boundary_tests_do_not_write_artifacts(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    raw = json.dumps({"output": printed_apt_update_spam(), "exit_code": 0})

    transformed = transform_tool_result(
        raw,
        tool_name="execute_code",
        noisegate_max_chars=120,
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(artifact_dir),
    )

    assert transformed is None
    assert not artifact_dir.exists()
