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


def write_hermes_console_script(
    path: Path,
    *,
    python: str = "/opt/hermes/.venv/bin/python",
) -> None:
    path.write_text(
        f"#!{python}\n"
        "from hermes_cli.cli import main\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )


def source_like_payload() -> str:
    return "\n".join(
        [
            "# Exact source-like payload that resembles noisy logs.",
            "config = {'FAILED': 'fixture', 'ERROR': 'fixture'}",
            "traceback_literal = 'Traceback (most recent call last):'",
            "npm_literal = 'npm ERR! code ELIFECYCLE'",
            "docker_literal = 'Dockerfile'",
            *[
                f"literal fixture line {index:03d}: FAILED ERROR Traceback npm ERR!"
                for index in range(90)
            ],
        ]
    )


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


def test_reduce_cli_preserves_unknown_tool_output() -> None:
    raw = numbered("future exact context", 100)
    proc = run_cli(
        "reduce",
        "--tool",
        "future_tool",
        "--max-chars",
        "120",
        input_text=raw,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_reduce_cli_metadata_reports_reducer_command_class_and_savings() -> None:
    raw = numbered("line", 100)
    proc = run_cli(
        "reduce",
        "--command",
        "make noisy",
        "--max-chars",
        "120",
        "--metadata",
        input_text=raw,
    )

    assert proc.returncode == 0, proc.stderr
    metadata = json.loads(proc.stderr)
    assert metadata["compacted"] is True
    assert metadata["command_class"] == "generic"
    assert metadata["reducer"] == "generic_head_tail"
    assert metadata["saved_chars"] > 0
    assert metadata["saved_lines"] > 0


def test_reduce_cli_metadata_reports_unchanged_reason() -> None:
    raw = numbered("exact line", 100)
    proc = run_cli(
        "reduce",
        "--tool",
        "terminal",
        "--command",
        "cat exact-output.txt",
        "--metadata",
        "--max-chars",
        "120",
        input_text=raw,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw
    metadata = json.loads(proc.stderr)
    assert metadata["compacted"] is False
    assert metadata["command_class"] == "file_read"
    assert metadata["reducer"] == "protected_file_read"
    assert metadata["reason"] == "file_read_passthrough"
    assert metadata["saved_chars"] == 0
    assert metadata["saved_lines"] == 0


def test_reduce_cli_metadata_stderr_failure_does_not_corrupt_stdout() -> None:
    dev_full = Path("/dev/full")
    if not dev_full.exists():
        return
    raw = numbered("line", 100)
    with dev_full.open("wb") as stderr:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "noisegate.cli",
                "reduce",
                "--command",
                "make noisy",
                "--max-chars",
                "120",
                "--metadata",
            ],
            input=raw,
            text=True,
            stdout=subprocess.PIPE,
            stderr=stderr,
            check=False,
        )

    assert proc.returncode == 0
    assert "[noisegate: omitted" in proc.stdout
    assert proc.stdout != raw
    assert not proc.stdout.endswith(raw)


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


def test_reduce_json_plain_result_string_with_numeric_exit_is_terminal_like() -> None:
    envelope = {
        "returncode": 7,
        "result": numbered("line", 100),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    outer = json.loads(proc.stdout)
    assert "[noisegate: omitted" in outer["result"]
    assert "[noisegate: exit_code=7]" in outer["result"]
    assert "line 001" in outer["result"]
    assert "line 100" in outer["result"]


def test_reduce_json_plain_result_string_prefers_actionable_exit_hint() -> None:
    cases = (
        ({"status": "failed", "exit_code": 0}, "[noisegate: exit_code=1]"),
        ({"exit_code": 0, "returncode": 7}, "[noisegate: exit_code=7]"),
    )

    for exit_hints, expected_notice in cases:
        envelope = {
            **exit_hints,
            "result": numbered("line", 100),
            "noisegate": {"max_chars": 120},
        }

        proc = run_cli("reduce-json", input_text=json.dumps(envelope))

        assert proc.returncode == 0, proc.stderr
        outer = json.loads(proc.stdout)
        assert "[noisegate: omitted" in outer["result"]
        assert expected_notice in outer["result"]


def test_reduce_json_nested_and_direct_results_prefer_actionable_exit_hint() -> None:
    hint_cases = (
        ({"status": "failed", "exit_code": 0}, "[noisegate: exit_code=1]"),
        ({"exit_code": 0, "returncode": 7}, "[noisegate: exit_code=7]"),
    )

    for exit_hints, expected_notice in hint_cases:
        nested = {**exit_hints, "output": numbered("nested", 100)}
        payloads = (
            {"tool_name": "process", "result": nested, "noisegate": {"max_chars": 120}},
            {
                "tool_name": "process",
                "result": json.dumps(nested),
                "noisegate": {"max_chars": 120},
            },
            {
                "tool_name": "process",
                **exit_hints,
                "output": numbered("direct", 100),
                "noisegate": {"max_chars": 120},
            },
        )

        for payload in payloads:
            proc = run_cli("reduce-json", input_text=json.dumps(payload))

            assert proc.returncode == 0, proc.stderr
            output = proc.stdout
            assert "[noisegate: omitted" in output
            assert expected_notice in output


def test_reduce_json_plain_result_string_with_bool_exit_stays_exact() -> None:
    envelope = {
        "returncode": False,
        "result": numbered("exact", 100),
        "noisegate": {"max_chars": 120},
    }
    raw = json.dumps(envelope)
    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_reduce_json_metadata_reports_plain_result_reducer_details() -> None:
    envelope = {
        "tool_name": "terminal",
        "args": {"command": "pytest"},
        "result": numbered("line", 100),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", "--metadata", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    metadata = json.loads(proc.stderr)
    assert metadata["compacted"] is True
    assert metadata["command_class"] == "pytest"
    assert metadata["reducer"] == "pytest"
    assert metadata["source"] == "reduce-json"
    assert metadata["output_chars"] == len(proc.stdout)
    assert metadata["field_output_chars"] < metadata["output_chars"]
    assert metadata["saved_chars"] > 0
    assert metadata["omitted_chars"] == metadata["saved_chars"]
    assert metadata["saved_lines"] == 0
    assert metadata["omitted_lines"] == 0
    assert metadata["field_saved_chars"] > 0
    assert metadata["field_omitted_chars"] == metadata["field_saved_chars"]
    assert metadata["field_omitted_lines"] > 0


def test_reduce_json_rewrites_result_dict_payload() -> None:
    envelope = {
        "tool_name": "process",
        "command": "pytest",
        "result": {"output": numbered("line", 100), "status": "failed"},
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    outer = json.loads(proc.stdout)
    result = outer["result"]
    assert isinstance(result, dict)
    assert "[noisegate: omitted" in result["output"]
    assert "[noisegate: exit_code=1]" in result["output"]
    assert result["noisegate"]["compacted"] is True


def test_reduce_json_nested_result_object_uses_envelope_status() -> None:
    payload = {
        "tool_name": "process",
        "status": "failed",
        "result": {"output": numbered("inner", 100)},
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert "status" not in parsed["result"]
    assert "[noisegate: omitted" in parsed["result"]["output"]
    assert "[noisegate: exit_code=1]" in parsed["result"]["output"]
    assert parsed["result"]["noisegate"]["fields"]["output"]["exit_code"] == 1


def test_reduce_json_nested_json_string_uses_envelope_status() -> None:
    nested = {"output": numbered("inner", 100)}
    payload = {
        "tool_name": "process",
        "status": "failed",
        "result": json.dumps(nested),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    nested_result = json.loads(parsed["result"])
    assert "status" not in nested_result
    assert "[noisegate: omitted" in nested_result["output"]
    assert "[noisegate: exit_code=1]" in nested_result["output"]
    assert nested_result["noisegate"]["fields"]["output"]["exit_code"] == 1


def test_reduce_json_nested_result_object_prefers_envelope_failed_status_over_exit_zero() -> None:
    payload = {
        "tool_name": "process",
        "status": "failed",
        "exit_code": 0,
        "result": {"output": numbered("inner", 100)},
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert "status" not in parsed["result"]
    assert "exit_code" not in parsed["result"]
    assert "[noisegate: omitted" in parsed["result"]["output"]
    assert "[noisegate: exit_code=1]" in parsed["result"]["output"]
    assert parsed["result"]["noisegate"]["fields"]["output"]["exit_code"] == 1


def test_reduce_json_nested_result_object_prefers_nonzero_envelope_exit_over_status() -> None:
    payload = {
        "tool_name": "process",
        "status": "failed",
        "returncode": 7,
        "result": {"output": numbered("inner", 100)},
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert "status" not in parsed["result"]
    assert "returncode" not in parsed["result"]
    assert "[noisegate: omitted" in parsed["result"]["output"]
    assert "[noisegate: exit_code=7]" in parsed["result"]["output"]
    assert parsed["result"]["noisegate"]["fields"]["output"]["exit_code"] == 7


def test_reduce_json_nested_result_object_keeps_nested_status_over_envelope_exit_code() -> None:
    payload = {
        "tool_name": "process",
        "exit_code": 0,
        "result": {"status": "failed", "output": numbered("inner", 100)},
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert parsed["result"]["status"] == "failed"
    assert "[noisegate: omitted" in parsed["result"]["output"]
    assert "[noisegate: exit_code=1]" in parsed["result"]["output"]
    assert parsed["result"]["noisegate"]["fields"]["output"]["exit_code"] == 1


def test_reduce_json_nested_json_string_keeps_nested_status_over_envelope_exit_code() -> None:
    nested = {"status": "failed", "output": numbered("inner", 100)}
    payload = {
        "tool_name": "process",
        "exit_code": 0,
        "result": json.dumps(nested),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    nested_result = json.loads(parsed["result"])
    assert nested_result["status"] == "failed"
    assert "[noisegate: omitted" in nested_result["output"]
    assert "[noisegate: exit_code=1]" in nested_result["output"]
    assert nested_result["noisegate"]["fields"]["output"]["exit_code"] == 1


def test_reduce_json_leaves_result_list_and_null_valid() -> None:
    for result in ([numbered("line", 5)], None):
        envelope = {"tool_name": "terminal", "result": result, "noisegate": {"max_chars": 120}}
        proc = run_cli("reduce-json", input_text=json.dumps(envelope))

        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == envelope


def test_reduce_json_plain_result_string_falls_back_from_empty_args_argv() -> None:
    envelope = {
        "tool_name": "terminal",
        "args": {"argv": []},
        "arguments": {"command": "cat important.txt"},
        "result": numbered("exact line", 100),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == envelope


def test_reduce_json_direct_payload_with_non_text_result_still_compacts_output() -> None:
    payload = {
        "tool_name": "terminal",
        "command": "pytest",
        "stdout": numbered("line", 100),
        "result": None,
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert parsed["result"] is None
    assert "[noisegate: omitted" in parsed["stdout"]
    assert parsed["_noisegate"]["compacted"] is True


def test_reduce_json_plain_result_string_honors_command_aliases_for_protection() -> None:
    envelope = {
        "tool_name": "terminal",
        "cmd": "cat important.txt",
        "result": numbered("exact line", 100),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == envelope


def test_reduce_json_plain_result_string_ignores_invalid_nested_command() -> None:
    envelope = {
        "tool_name": "terminal",
        "args": {"command": None},
        "command": "cat important.txt",
        "result": numbered("exact line", 100),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == envelope


def test_reduce_json_plain_result_string_honors_top_level_argv_for_protection() -> None:
    envelope = {
        "tool_name": "terminal",
        "argv": ["cat", "important.txt"],
        "result": numbered("exact line", 100),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == envelope


def test_reduce_json_plain_result_with_direct_payload_preserves_args_command() -> None:
    envelope = {
        "tool_name": "terminal",
        "args": {"command": "cat important.txt"},
        "result": "ok",
        "stdout": numbered("exact line", 100),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == envelope


def test_reduce_json_direct_payload_preserves_args_command_without_result() -> None:
    payload = {
        "tool_name": "terminal",
        "args": {"command": "cat important.txt"},
        "stdout": numbered("exact line", 100),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_direct_payload_prefers_args_over_top_level_command() -> None:
    payload = {
        "tool_name": "terminal",
        "command": "pytest",
        "args": {"command": "cat important.txt"},
        "stdout": numbered("exact line", 100),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_direct_payload_preserves_arguments_command_without_result() -> None:
    payload = {
        "tool_name": "terminal",
        "arguments": {"command": "cat important.txt"},
        "stdout": numbered("exact line", 100),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_direct_payload_compacts_args_command_without_result_or_exit() -> None:
    payload = {
        "args": {"command": "pytest"},
        "stdout": numbered("line", 100),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert "[noisegate: omitted" in parsed["stdout"]
    assert parsed["_noisegate"]["compacted"] is True


def test_reduce_json_direct_payload_with_dict_result_still_compacts_top_level_output() -> None:
    payload = {
        "tool_name": "terminal",
        "command": "pytest",
        "stdout": numbered("line", 100),
        "result": {"value": "ok"},
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert parsed["result"] == {"value": "ok"}
    assert "[noisegate: omitted" in parsed["stdout"]
    assert parsed["_noisegate"]["compacted"] is True


def test_reduce_json_mixed_result_dict_and_top_level_output_compacts_both() -> None:
    payload = {
        "tool_name": "terminal",
        "command": "pytest",
        "stdout": numbered("outer", 100),
        "result": {"output": numbered("inner", 100), "exit_code": 1},
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert "[noisegate: omitted" in parsed["stdout"]
    assert "[noisegate: omitted" in parsed["result"]["output"]
    assert parsed["result"]["noisegate"]["compacted"] is True
    assert parsed["_noisegate"]["compacted"] is True


def test_reduce_json_mixed_terminal_like_payload_without_tool_name_compacts_both() -> None:
    payload = {
        "stdout": numbered("outer", 100),
        "returncode": 1,
        "result": {"output": numbered("inner", 100), "exit_code": 1},
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert "[noisegate: omitted" in parsed["stdout"]
    assert "[noisegate: omitted" in parsed["result"]["output"]
    assert parsed["result"]["noisegate"]["compacted"] is True
    assert parsed["_noisegate"]["compacted"] is True


def test_reduce_json_nested_result_object_without_tool_name_compacts_from_command() -> None:
    payload = {
        "command": "pytest",
        "result": {"output": numbered("inner", 100), "exit_code": 1},
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert "[noisegate: omitted" in parsed["result"]["output"]
    assert "[noisegate: exit_code=1]" in parsed["result"]["output"]
    assert parsed["result"]["noisegate"]["compacted"] is True


def test_reduce_json_nested_json_string_without_tool_name_compacts_from_command() -> None:
    nested = {"output": numbered("inner", 100), "exit_code": 1}
    payload = {
        "command": "pytest",
        "result": json.dumps(nested),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    nested_result = json.loads(parsed["result"])
    assert "[noisegate: omitted" in nested_result["output"]
    assert "[noisegate: exit_code=1]" in nested_result["output"]
    assert nested_result["noisegate"]["compacted"] is True


def test_reduce_json_preserves_nested_json_when_outer_envelope_would_grow() -> None:
    nested = {"stdout": numbered("line", 44)}
    payload = {
        "tool_name": "terminal",
        "result": json.dumps(nested, separators=(",", ":")),
        "noisegate": {"max_chars": 80},
    }
    raw = json.dumps(payload, separators=(",", ":"))
    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_reduce_json_preserves_nested_json_string_when_compacting_generic_top_level() -> None:
    nested = {"items": [f"value {index:03d}" for index in range(100)]}
    payload = {
        "tool_name": "browser_console",
        "logs": numbered("log", 100),
        "result": json.dumps(nested, separators=(",", ":")),
        "noisegate": {"max_chars": 180},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload, separators=(",", ":")))

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert "[noisegate: omitted" in parsed["logs"]
    assert json.loads(parsed["result"]) == nested


def test_reduce_json_nested_result_object_without_tool_name_compacts_from_args_command() -> None:
    payload = {
        "args": {"command": "pytest"},
        "result": {"output": numbered("inner", 100)},
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert "[noisegate: omitted" in parsed["result"]["output"]
    assert parsed["result"]["noisegate"]["compacted"] is True


def test_reduce_json_nested_json_string_without_tool_name_compacts_from_args_command() -> None:
    nested = {"output": numbered("inner", 100)}
    payload = {
        "args": {"command": "pytest"},
        "result": json.dumps(nested),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    nested_result = json.loads(parsed["result"])
    assert "[noisegate: omitted" in nested_result["output"]
    assert nested_result["noisegate"]["compacted"] is True


def test_reduce_json_preserves_nested_protected_result_object_without_tool_name() -> None:
    payload = {
        "args": {"command": "pytest"},
        "result": {
            "tool_name": "read_file",
            "output": numbered("exact", 100),
            "status": "ok",
        },
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_preserves_protected_outer_tool_with_nested_terminal_payload() -> None:
    nested = {
        "tool_name": "terminal",
        "args": {"command": "pytest"},
        "stdout": numbered("exact", 100),
    }
    cases = []
    for tool_name in (
        "read_file",
        "web_extract",
        "lcm_expand",
        "mcp__server__read_resource",
        "future_tool",
    ):
        cases.append({"tool_name": tool_name, "result": nested, "noisegate": {"max_chars": 120}})
        cases.append(
            {
                "tool_name": tool_name,
                "result": json.dumps(nested),
                "noisegate": {"max_chars": 120},
            }
        )

    for payload in cases:
        raw = json.dumps(payload)
        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw


def test_reduce_json_preserves_nested_args_command_over_outer_args() -> None:
    payload = {
        "tool_name": "terminal",
        "args": {"command": "pytest"},
        "result": {
            "args": {"command": "cat important.txt"},
            "stdout": numbered("exact", 100),
        },
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_preserves_nested_json_args_command_over_outer_args() -> None:
    nested = {
        "args": {"command": "cat important.txt"},
        "stdout": numbered("exact", 100),
    }
    payload = {
        "tool_name": "terminal",
        "args": {"command": "pytest"},
        "result": json.dumps(nested),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_preserves_nested_protected_json_string_without_tool_name() -> None:
    nested = {
        "tool_name": "read_file",
        "output": numbered("exact", 100),
        "status": "ok",
    }
    payload = {
        "args": {"command": "pytest"},
        "result": json.dumps(nested),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_direct_payload_with_returncode_is_terminal_like() -> None:
    payload = {
        "stdout": numbered("line", 100),
        "returncode": 1,
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert "[noisegate: omitted" in parsed["stdout"]
    assert "[noisegate: exit_code=1]" in parsed["stdout"]


def test_reduce_json_preserves_ambiguous_blank_tool_direct_payloads() -> None:
    cases = [
        {"output": numbered("exact", 100), "status": "ok"},
        {"output": numbered("exact", 100), "status": "failed"},
        {"stdout": numbered("exact", 100), "command": ""},
        {"stdout": numbered("exact", 100), "argv": []},
        {"stdout": numbered("exact", 100), "argv": [""]},
        {"stdout": numbered("exact", 100), "argv": ["", "file.txt"]},
        {"stdout": numbered("exact", 100), "exit": True},
        {"stdout": numbered("exact", 100), "returncode": False},
    ]

    for payload in cases:
        payload["noisegate"] = {"max_chars": 120}
        raw = json.dumps(payload)
        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw


def test_reduce_json_preserves_ambiguous_blank_tool_nested_payloads() -> None:
    cases = [
        {
            "status": "ok",
            "result": {"output": numbered("exact", 100)},
            "noisegate": {"max_chars": 120},
        },
        {
            "exit": True,
            "result": {"output": numbered("exact", 100)},
            "noisegate": {"max_chars": 120},
        },
        {
            "result": json.dumps({"output": numbered("exact", 100), "status": "failed"}),
            "noisegate": {"max_chars": 120},
        },
    ]

    for payload in cases:
        raw = json.dumps(payload)
        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw


def test_reduce_json_compacts_invalid_json_result_string_as_plain_output() -> None:
    envelope = {
        "tool_name": "terminal",
        "args": {"command": "pytest"},
        "result": "{not json\n" + numbered("line", 100),
        "noisegate": {"max_chars": 120},
    }
    proc = run_cli("reduce-json", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    outer = json.loads(proc.stdout)
    assert outer["result"].startswith("{not json")
    assert "[noisegate: omitted" in outer["result"]


def test_reduce_json_preserves_plain_result_for_non_noisy_tool() -> None:
    envelope = {
        "tool_name": "web_extract",
        "result": numbered("important article context", 100),
        "noisegate": {"max_chars": 120},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == envelope


def test_reduce_json_bad_input_fails_open() -> None:
    raw = "{not json"

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0
    assert proc.stdout == raw


def test_reduce_json_pathological_json_fails_open() -> None:
    cases = (
        "[" * 2000 + "]" * 2000,
        "1" * 5000,
    )

    for raw in cases:
        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw


def test_reduce_json_metadata_reports_bad_input_reason() -> None:
    raw = "{not json"

    proc = run_cli("reduce-json", "--metadata", input_text=raw)

    assert proc.returncode == 0
    assert proc.stdout == raw
    metadata = json.loads(proc.stderr)
    assert metadata["compacted"] is False
    assert metadata["source"] == "reduce-json"
    assert metadata["reason"] == "invalid_json"


def test_reduce_json_preserves_direct_protected_tool_payload() -> None:
    payload = {
        "tool_name": "read_file",
        "content": numbered("exact line", 100),
        "noisegate": {"max_chars": 120},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_preserves_source_like_protected_tool_payloads() -> None:
    exact = source_like_payload()
    for tool_name in (
        "read_file",
        "write_file",
        "patch",
        "apply_patch",
        "skill_view",
        "web_extract",
        "memory",
        "hindsight_recall",
        "lcm_expand",
        "mcp__mindlyos__get_note_page",
    ):
        payload = {
            "tool_name": tool_name,
            "result": exact,
            "noisegate": {"max_chars": 200, "max_lines": 20},
        }

        proc = run_cli("reduce-json", input_text=json.dumps(payload))

        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == payload


def test_reduce_json_preserves_source_like_terminal_file_display_payload() -> None:
    exact = source_like_payload()
    payload = {
        "tool_name": "terminal",
        "command": "nl -ba src/source_fixture.py",
        "stdout": exact,
        "exit_code": 0,
        "noisegate": {"max_chars": 200, "max_lines": 20},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_args_command_alias_wins_over_direct_payload_command() -> None:
    exact = source_like_payload()
    payload = {
        "tool_name": "terminal",
        "args": {"cmd": "cat src/source_fixture.py"},
        "command": "pytest -q",
        "stdout": exact,
        "exit_code": 0,
        "noisegate": {"max_chars": 200, "max_lines": 20},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_preserves_direct_terminal_payload_with_args_command_alias() -> None:
    exact = source_like_payload()
    payload = {
        "tool_name": "terminal",
        "args": {"cmd": "cat src/source_fixture.py"},
        "stdout": exact,
        "exit_code": 0,
        "noisegate": {"max_chars": 200, "max_lines": 20},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_preserves_argv_file_display_with_metachar_paths() -> None:
    exact = source_like_payload()

    for path in ("src/A&B.py", "src/A>B.py", "src/A;B.py", "src/$(fixture).py"):
        payload = {
            "tool_name": "terminal",
            "args": {"argv": ["cat", path]},
            "stdout": exact,
            "exit_code": 0,
            "noisegate": {"max_chars": 200, "max_lines": 20},
        }

        proc = run_cli("reduce-json", input_text=json.dumps(payload))

        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == payload


def test_reduce_json_uses_arguments_command_when_args_has_no_command() -> None:
    exact = source_like_payload()
    payload = {
        "tool_name": "terminal",
        "args": {"timeout": 10},
        "arguments": {"cmd": "cat src/source_fixture.py"},
        "stdout": exact,
        "exit_code": 0,
        "noisegate": {"max_chars": 200, "max_lines": 20},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_args_command_alias_wins_over_arguments_command() -> None:
    exact = source_like_payload()
    payload = {
        "tool_name": "terminal",
        "args": {"cmd": "cat src/source_fixture.py"},
        "arguments": {"command": "pytest -q"},
        "stdout": exact,
        "exit_code": 0,
        "noisegate": {"max_chars": 200, "max_lines": 20},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_preserves_file_display_patch_when_diff_passthrough_is_disabled() -> None:
    exact = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: src/source_fixture.py",
            "+value = 'FAILED ERROR Traceback npm ERR! Dockerfile'",
            *[f"+literal patch line {index:03d}: FAILED ERROR" for index in range(90)],
            "*** End Patch",
        ]
    )
    payload = {
        "tool_name": "terminal",
        "command": "cat patches/source.patch",
        "stdout": exact,
        "exit_code": 0,
        "noisegate": {"max_chars": 200, "max_lines": 20, "preserve_diffs": False},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_preserves_json_result_file_display_from_top_level_command() -> None:
    exact = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: src/source_fixture.py",
            "+value = 'FAILED ERROR Traceback npm ERR! Dockerfile'",
            *[f"+literal patch line {index:03d}: FAILED ERROR" for index in range(90)],
            "*** End Patch",
        ]
    )
    payload = {
        "tool_name": "terminal",
        "command": "cat patches/source.patch",
        "result": json.dumps({"stdout": exact, "exit_code": 0}),
        "noisegate": {"max_chars": 200, "max_lines": 20, "preserve_diffs": False},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_prefers_existing_args_command_alias_over_top_level_command() -> None:
    exact = source_like_payload()
    payload = {
        "tool_name": "terminal",
        "args": {"cmd": "cat src/source_fixture.py"},
        "command": "pytest -q",
        "result": json.dumps({"stdout": exact, "exit_code": 0}),
        "noisegate": {"max_chars": 200, "max_lines": 20},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_protected_top_level_command_wins_over_stale_args_command() -> None:
    exact = source_like_payload()
    payload = {
        "tool_name": "terminal",
        "args": {"command": "pytest -q"},
        "command": "cat src/source_fixture.py",
        "result": json.dumps({"stdout": exact, "exit_code": 0}),
        "noisegate": {"max_chars": 200, "max_lines": 20},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_uses_nested_or_outer_exit_code_when_ranking_commands() -> None:
    exact = "\n".join(
        [
            "diff --git a/app.py b/app.py",
            "--- a/app.py",
            "+++ b/app.py",
            "@@ -1 +1 @@",
            "-old",
            "+new",
            *[f"+exact diff line {index:03d}" for index in range(180)],
        ]
    )
    nested = {
        "stdout": exact,
        "stderr": "cat: missing: No such file or directory",
    }
    payloads = (
        {
            "tool_name": "terminal",
            "args": {"command": "pytest -q && cat README"},
            "command": "cat patches/change.diff missing",
            "result": json.dumps({**nested, "exit_code": 1}),
            "noisegate": {"max_chars": 200, "max_lines": 20, "preserve_diffs": False},
        },
        {
            "tool_name": "terminal",
            "args": {"command": "pytest -q && cat README"},
            "command": "cat patches/change.diff missing",
            "exit_code": 1,
            "result": json.dumps(nested),
            "noisegate": {"max_chars": 200, "max_lines": 20, "preserve_diffs": False},
        },
    )

    for payload in payloads:
        proc = run_cli("reduce-json", input_text=json.dumps(payload))

        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == payload


def test_reduce_json_decodes_nested_result_for_output_assisted_command_selection() -> None:
    search_output = "\n".join(
        f"tests/test_{index}.py::test_function_{index} PASSED" for index in range(180)
    )
    nested_results = (
        json.dumps(search_output),
        json.dumps({"output": search_output, "exit_code": 0}),
    )

    for nested_result in nested_results:
        payload = {
            "tool_name": "terminal",
            "args": {"command": "pytest -q"},
            "command": 'rg "$(printf target)" src',
            "result": nested_result,
            "noisegate": {"max_chars": 200, "max_lines": 20},
        }

        proc = run_cli("reduce-json", input_text=json.dumps(payload))

        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == payload, nested_result[:80]


def test_reduce_json_plain_result_uses_existing_args_command_alias() -> None:
    exact = source_like_payload()
    payload = {
        "tool_name": "terminal",
        "args": {"cmd": "cat src/source_fixture.py"},
        "result": exact,
        "noisegate": {"max_chars": 200, "max_lines": 20},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_plain_result_uses_top_level_command_alias() -> None:
    exact = source_like_payload()
    payload = {
        "tool_name": "terminal",
        "cmd": "cat src/source_fixture.py",
        "result": exact,
        "noisegate": {"max_chars": 200, "max_lines": 20},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(payload))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload


def test_reduce_json_inner_blank_command_falls_back_to_outer_file_display_command() -> None:
    exact = source_like_payload()
    payload = {
        "tool_name": "terminal",
        "command": "cat src/source_fixture.py",
        "result": json.dumps({"command": "", "stdout": exact, "exit_code": 0}),
        "noisegate": {"max_chars": 200, "max_lines": 20},
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


def test_reduce_json_no_gain_artifact_request_has_no_side_effect(
    tmp_path: Path,
) -> None:
    cases = {
        "plain": {"returncode": 0, "result": "A" * 4001, "context": ""},
        "nested": {
            "tool_name": "terminal",
            "result": json.dumps({"stdout": "A" * 4010}),
        },
        "mixed": {
            "tool_name": "terminal",
            "command": "pytest",
            "stdout": "B" * 4010,
            "result": json.dumps({"stdout": "A" * 4010}),
        },
    }

    for name, payload in cases.items():
        artifact_dir = tmp_path / name / "artifacts"
        raw = json.dumps(payload, separators=(",", ":"))
        proc = run_cli(
            "reduce-json",
            "--store-artifact",
            "--artifact-dir",
            str(artifact_dir),
            input_text=raw,
        )

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw
        assert not artifact_dir.exists()


def test_reduce_json_no_gain_metadata_does_not_claim_artifact(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "artifacts"
    payload = {"returncode": 0, "result": "A" * 4001, "context": ""}
    raw = json.dumps(payload, separators=(",", ":"))

    proc = run_cli(
        "reduce-json",
        "--metadata",
        "--store-artifact",
        "--artifact-dir",
        str(artifact_dir),
        input_text=raw,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw
    metadata = json.loads(proc.stderr)
    assert metadata["artifact"] == {
        "stored": False,
        "reason": "outer_no_gain",
        "size_bytes": 4001,
    }
    assert not artifact_dir.exists()


def test_reduce_json_multi_artifact_envelopes_stay_inline_only(tmp_path: Path) -> None:
    cases = {
        "multi-field": {
            "tool_name": "terminal",
            "command": "make noisy",
            "stdout": numbered("stdout", 600),
            "stderr": numbered("stderr", 600),
            "noisegate": {"max_chars": 500},
        },
        "mixed": {
            "tool_name": "terminal",
            "command": "make noisy",
            "stdout": numbered("outer", 600),
            "result": json.dumps({"stdout": numbered("inner", 600)}),
            "noisegate": {"max_chars": 500},
        },
    }

    for name, payload in cases.items():
        artifact_dir = tmp_path / name / "artifacts"
        raw = json.dumps(payload, separators=(",", ":"))
        proc = run_cli(
            "reduce-json",
            "--store-artifact",
            "--artifact-dir",
            str(artifact_dir),
            input_text=raw,
        )

        assert proc.returncode == 0, proc.stderr
        assert len(proc.stdout) < len(raw)
        assert "[noisegate: omitted" in proc.stdout
        assert "[noisegate artifact:" not in proc.stdout
        assert not artifact_dir.exists()


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
    assert "environment: ok" in proc.stdout
    assert "config: enabled=True mode=auto" in proc.stdout
    assert "artifacts: disabled" in proc.stdout
    assert "artifact_size_cap: 1000000" in proc.stdout


def test_doctor_cli_reports_invalid_environment_values(tmp_path: Path) -> None:
    proc = run_cli(
        "doctor",
        env={
            "NOISEGATE_ARTIFACT_DIR": str(tmp_path / "artifacts"),
            "NOISEGATE_DISABLE": "definitely",
            "NOISEGATE_ARTIFACTS": "sometimes",
            "NOISEGATE_ARTIFACT_SIZE_CAP": "-1",
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert "environment: warnings" in proc.stdout
    assert "NOISEGATE_DISABLE='definitely' is not recognized" in proc.stdout
    assert "NOISEGATE_ARTIFACTS='sometimes' is not recognized" in proc.stdout
    assert "NOISEGATE_ARTIFACT_SIZE_CAP='-1' is invalid" in proc.stdout
    assert "config: enabled=True mode=auto" in proc.stdout
    assert "artifacts: disabled" in proc.stdout
    assert "artifact_size_cap: 1000000" in proc.stdout


def test_install_hermes_cli_dry_run_reports_plan(tmp_path: Path) -> None:
    hermes = tmp_path / "hermes"
    write_hermes_console_script(hermes)

    proc = run_cli(
        "install-hermes",
        "--dry-run",
        "--hermes",
        str(hermes),
        "--package",
        "/tmp/noisegate-wheel.whl",
        "--installer",
        "uv",
    )

    assert proc.returncode == 0, proc.stderr
    assert "Noisegate Hermes install plan" in proc.stdout
    assert f"hermes: {hermes}" in proc.stdout
    assert "hermes_python: /opt/hermes/.venv/bin/python" in proc.stdout
    expected = "pip install --python /opt/hermes/.venv/bin/python /tmp/noisegate-wheel.whl"
    assert expected in proc.stdout


def test_install_hermes_cli_rejects_invalid_launcher(tmp_path: Path) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text("not a shebang\n", encoding="utf-8")

    proc = run_cli("install-hermes", "--dry-run", "--hermes", str(hermes))

    assert proc.returncode == 2
    assert "no Python shebang" in proc.stderr


def test_install_hermes_cli_dry_run_mentions_no_writes_or_restart(tmp_path: Path) -> None:
    hermes = tmp_path / "hermes"
    write_hermes_console_script(hermes)

    proc = run_cli("install-hermes", "--dry-run", "--hermes", str(hermes))

    assert proc.returncode == 0, proc.stderr
    assert "dry run; install/enable/doctor commands will not run" in proc.stdout
    assert "restart: not performed by Noisegate" in proc.stdout


def test_install_hermes_cli_reports_missing_hermes_on_path(tmp_path: Path) -> None:
    proc = run_cli(
        "install-hermes",
        "--dry-run",
        env={"PATH": str(tmp_path)},
    )

    assert proc.returncode == 2
    assert "Hermes executable not found on PATH: hermes" in proc.stderr


def test_reduce_json_plain_result_artifact_id_resolves(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    raw_result = numbered("plain", 200)
    payload = {
        "tool_name": "terminal",
        "command": "pytest",
        "returncode": 1,
        "result": raw_result,
        "noisegate": {"max_chars": 300},
    }

    proc = run_cli(
        "reduce-json",
        "--store-artifact",
        "--artifact-dir",
        str(artifact_dir),
        input_text=json.dumps(payload),
    )

    assert proc.returncode == 0, proc.stderr
    output = json.loads(proc.stdout)["result"]
    artifact_files = list(artifact_dir.glob("ng_*.txt"))
    assert len(artifact_files) == 1
    assert artifact_files[0].stem in output
    assert artifact_files[0].read_text(encoding="utf-8") == raw_result


def test_reduce_json_plain_result_store_failure_delivers_no_id(tmp_path: Path) -> None:
    artifact_file = tmp_path / "not-a-dir"
    artifact_file.write_text("x", encoding="utf-8")
    payload = {
        "tool_name": "terminal",
        "command": "pytest",
        "returncode": 1,
        "result": numbered("plain", 200),
        "noisegate": {"max_chars": 300},
    }

    proc = run_cli(
        "reduce-json",
        "--store-artifact",
        "--artifact-dir",
        str(artifact_file),
        input_text=json.dumps(payload),
    )

    assert proc.returncode == 0, proc.stderr
    output = json.loads(proc.stdout)["result"]
    assert "id=ng_" not in output
    assert "reason=artifact_error" in output


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
                "noisegate": {"max_chars": 240},
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
        "220",
        "--store-artifact",
        "--artifact-dir",
        str(artifact_dir),
        input_text=numbered("line", 100),
    )

    assert proc.returncode == 0, proc.stderr
    assert "[noisegate artifact: id=ng_" in proc.stdout
    assert "sha256=" in proc.stdout
    assert len(proc.stdout) <= 220


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


def test_artifacts_verify_reports_temp_files_without_raw_content(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(mode=0o700)
    temp_path = artifact_dir / f".ng_{'a' * 24}.leftover.tmp"
    temp_path.write_text("raw terminal output", encoding="utf-8")
    os.chmod(temp_path, 0o600)

    proc = run_cli("artifacts", "verify", "--artifact-dir", str(artifact_dir))

    assert proc.returncode == 2
    assert "temp_file" in proc.stdout
    assert "raw terminal output" not in proc.stdout
    assert "raw terminal output" not in proc.stderr


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


def test_wrap_cli_metadata_reports_reducer_and_preserves_exit_code() -> None:
    script = "for index in range(100): print(f'line {index:03d}')\nraise SystemExit(3)\n"

    proc = run_cli(
        "wrap",
        "--command",
        "pytest",
        "--max-chars",
        "160",
        "--debug",
        "--",
        sys.executable,
        "-c",
        script,
    )

    assert proc.returncode == 3
    metadata = json.loads(proc.stderr)
    assert metadata["command_class"] == "pytest"
    assert metadata["reducer"] == "pytest"
    assert metadata["exit_code"] == 3
    assert metadata["saved_chars"] > 0


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
        "240",
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
        if _process_is_zombie(pid):
            return
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    raise AssertionError(f"process still alive: {pid}")


def _process_is_zombie(pid: int) -> bool:
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        return stat_text.rsplit(")", 1)[1].lstrip().startswith("Z ")
    except IndexError:
        return False


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def test_cat_cli_accepts_custom_artifact_dir(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "custom-artifacts"
    reduce_proc = run_cli(
        "reduce",
        "--command",
        "pytest",
        "--max-chars",
        "220",
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
    artifact_dir.mkdir(mode=0o700)
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
        "240",
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
        "240",
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
    artifact_dir.mkdir(mode=0o700)
    (artifact_dir / "ng_000000000000000000000000.txt").mkdir()

    verify_proc = run_cli("artifacts", "verify", "--artifact-dir", str(artifact_dir))

    assert verify_proc.returncode == 2
    assert "non_regular" in verify_proc.stdout


def test_artifacts_cli_verify_rejects_oversized_nodes_without_reading(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(mode=0o700)
    (artifact_dir / "ng_000000000000000000000000.txt").write_text("x" * 1_000_001)

    verify_proc = run_cli("artifacts", "verify", "--artifact-dir", str(artifact_dir))

    assert verify_proc.returncode == 2
    assert "too_large" in verify_proc.stdout
