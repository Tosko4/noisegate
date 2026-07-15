from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import noisegate.cli as cli
import noisegate.engine as engine
import noisegate.plugin as plugin
from noisegate.artifacts import ArtifactStore


def numbered(prefix: str, count: int) -> str:
    return "\n".join(f"{prefix} {index:03d}" for index in range(1, count + 1))


def alignment_exhaustion_text() -> str:
    return "\n".join(
        [
            "[noisegate: omitted 1 lines]",
            *(["externalized_ref=dup"] * 20),
            *[f"ValueError: diagnostic-{index}" for index in range(50)],
        ]
    )


def mixed_nested_exhaustion_payload() -> dict[str, object]:
    return {
        "tool_name": "terminal",
        "command": "pytest -q",
        "exit_code": 1,
        "result": json.dumps({"stdout": numbered("simple", 100)}),
        "stderr": alignment_exhaustion_text(),
    }


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
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    full_env = os.environ.copy()
    full_env.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "noisegate.cli", *args],
        input=input_text,
        text=True,
        capture_output=True,
        env=full_env,
        cwd=cwd,
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


def test_reduce_json_compacts_direct_and_wrapped_write_diagnostics() -> None:
    diagnostic = numbered("src/file.py:10:2: error E100 useful diagnostic", 120)
    write_result = {"diagnostics": diagnostic, "content": "exact source\n"}
    envelopes = (
        {
            "tool_name": "write_file",
            **write_result,
            "noisegate": {"max_chars": 240, "max_lines": 7},
        },
        {
            "tool_name": "tool_call",
            "args": {"name": "apply_patch", "arguments": {"path": "src/file.py"}},
            "result": write_result,
            "noisegate": {"max_chars": 240, "max_lines": 7},
        },
        {
            "tool_name": "tool_call",
            "args": {"name": "edit_file", "arguments": {"path": "src/file.py"}},
            "result": json.dumps(write_result),
            "noisegate": {"max_chars": 240, "max_lines": 7},
        },
    )

    for index, envelope in enumerate(envelopes):
        raw = json.dumps(envelope)

        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout != raw, index
        payload = json.loads(proc.stdout)
        result = payload.get("result", payload)
        if isinstance(result, str):
            result = json.loads(result)
        assert result["content"] == "exact source\n", index
        assert result["diagnostics"] != diagnostic, index
        assert "src/file.py:10:2" in result["diagnostics"], index


def test_reduce_json_validates_root_and_nested_write_scopes_atomically() -> None:
    root_diagnostic = numbered("root.py:10:2: error E100 root diagnostic", 120)
    nested_diagnostic = numbered("nested.py:20:3: warning W200 nested diagnostic", 120)

    def envelopes(
        result: dict[str, object],
        root_fields: dict[str, object],
    ) -> list[tuple[str, dict[str, object], object]]:
        cases: list[tuple[str, dict[str, object], object]] = []
        for identity, identity_fields in (
            ("direct", {"tool_name": "write_file"}),
            (
                "wrapper",
                {
                    "tool_name": "tool_call",
                    "args": {
                        "name": "write_file",
                        "arguments": {"path": "src/file.py"},
                    },
                },
            ),
        ):
            for result_form, result_value in (
                ("object", result),
                ("json-string", json.dumps(result, separators=(",", ":"))),
            ):
                envelope = {
                    **identity_fields,
                    **root_fields,
                    "result": result_value,
                    "noisegate": {"max_chars": 240, "max_lines": 7},
                }
                cases.append((f"{identity}-{result_form}", envelope, result_value))
        return cases

    nested_valid = {
        "diagnostics": nested_diagnostic,
        "source": "exact nested source",
    }
    for invalid_root in ("ok", ["unsupported"]):
        for label, envelope, _ in envelopes(
            nested_valid,
            {"diagnostics": invalid_root, "source": "exact root source"},
        ):
            raw = json.dumps(envelope)
            proc = run_cli("reduce-json", input_text=raw)

            assert proc.returncode == 0, proc.stderr
            assert proc.stdout == raw, (label, invalid_root)

    scenarios = (
        (
            "root-only",
            {"source": "exact nested source"},
            {"diagnostics": root_diagnostic, "source": "exact root source"},
            True,
            False,
        ),
        (
            "nested-only",
            nested_valid,
            {"source": "exact root source"},
            False,
            True,
        ),
        (
            "both",
            nested_valid,
            {"diagnostics": root_diagnostic, "source": "exact root source"},
            True,
            True,
        ),
    )
    for scenario, result, root_fields, root_changes, nested_changes in scenarios:
        for label, envelope, original_result in envelopes(result, root_fields):
            raw = json.dumps(envelope)
            proc = run_cli("reduce-json", input_text=raw)

            assert proc.returncode == 0, proc.stderr
            assert proc.stdout != raw, (scenario, label)
            payload = json.loads(proc.stdout)
            if root_changes:
                assert payload["diagnostics"] != root_diagnostic, (scenario, label)
            if not nested_changes:
                assert payload["result"] == original_result, (scenario, label)
                continue
            nested = payload["result"]
            if isinstance(nested, str):
                nested = json.loads(nested)
            assert nested["diagnostics"] != nested_diagnostic, (scenario, label)
            assert nested["source"] == "exact nested source", (scenario, label)


def test_reduce_json_unsafe_nested_write_diagnostic_aborts_direct_compaction() -> None:
    diagnostic = numbered("outer.py:10:2: error E100 useful diagnostic", 120)
    nested = {"warnings": ["unsupported"], "content": "exact nested source\n"}

    for result in (nested, json.dumps(nested)):
        envelope = {
            "tool_name": "tool_call",
            "args": {
                "name": "write_file",
                "arguments": {"path": "src/file.py"},
            },
            "result": result,
            "diagnostics": diagnostic,
            "content": "exact outer source\n",
            "noisegate": {"max_chars": 240, "max_lines": 7},
        }
        raw = json.dumps(envelope)

        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw, type(result).__name__


def test_reduce_json_write_diagnostics_keep_direct_strings_and_no_gain_exact() -> None:
    envelopes = (
        {
            "tool_name": "write_file",
            "result": numbered("exact direct write result", 100),
            "noisegate": {"max_chars": 120},
        },
        {
            "tool_name": "write_file",
            "diagnostics": "ok",
            "content": "exact source\n",
            "noisegate": {"max_chars": 120},
        },
    )

    for envelope in envelopes:
        raw = json.dumps(envelope)

        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw


def test_reduce_json_same_object_no_gain_diagnostic_aborts_all_siblings() -> None:
    warnings = numbered("src/file.py:10:2: warning W100 useful diagnostic", 120)
    envelope = {
        "tool_name": "write_file",
        "diagnostics": "ok",
        "warnings": warnings,
        "content": "exact source\n",
        "noisegate": {"max_chars": 240, "max_lines": 7},
    }
    raw = json.dumps(envelope)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_reduce_json_write_diagnostics_preserve_exit_hints() -> None:
    diagnostics = numbered("src/file.py:10:2: error E100 useful diagnostic", 120)
    envelopes = (
        (
            {
                "tool_name": "write_file",
                "diagnostics": diagnostics,
                "exit_code": 4,
                "source": "exact root source",
                "noisegate": {"max_chars": 240, "max_lines": 7},
            },
            4,
            False,
        ),
        (
            {
                "tool_name": "write_file",
                "status": "failed",
                "result": {
                    "diagnostics": diagnostics,
                    "source": "exact nested source",
                },
                "noisegate": {"max_chars": 240, "max_lines": 7},
            },
            1,
            True,
        ),
    )

    for envelope, expected_exit_code, nested in envelopes:
        raw = json.dumps(envelope)
        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout != raw
        payload = json.loads(proc.stdout)
        diagnostic_payload = payload["result"] if nested else payload
        assert (
            f"[noisegate: exit_code={expected_exit_code}]"
            in diagnostic_payload["diagnostics"]
        )
        metadata_key = "noisegate" if nested else "_noisegate"
        assert (
            diagnostic_payload[metadata_key]["fields"]["diagnostics"]["exit_code"]
            == expected_exit_code
        )
        assert diagnostic_payload["source"].startswith("exact ")


def test_reduce_json_write_diagnostics_never_plan_or_store_artifacts(tmp_path: Path) -> None:
    diagnostic = numbered("src/file.py:10:2: error E100 useful diagnostic", 120)
    nested = {"diagnostics": diagnostic, "content": "exact source\n"}
    envelope = {
        "tool_name": "tool_call",
        "args": {"name": "apply_patch", "arguments": {"path": "src/file.py"}},
        "result": json.dumps(nested),
        "noisegate": {"max_chars": 240, "max_lines": 7},
    }
    raw = json.dumps(envelope)
    artifact_dir = tmp_path / "artifacts"
    options = engine.NoisegateOptions(
        max_chars=240,
        max_lines=7,
        artifact_enabled=True,
        artifact_dir=artifact_dir,
    )
    plans: list[plugin._ArtifactPreviewPlan] = []

    output = cli._reduce_json_value(
        envelope,
        raw,
        options,
        defer_artifact_store=True,
        artifact_plans_out=plans,
    )

    assert output != raw
    assert plans == []
    assert not artifact_dir.exists()


def test_reduce_json_write_diagnostic_nonfinite_values_fail_open_exactly() -> None:
    diagnostic = numbered("src/file.py:10:2: error E100 useful diagnostic", 120)

    for constant in ("1e400", "-1e400", "NaN"):
        raw = (
            '{"tool_name":"write_file","diagnostics":'
            f'{json.dumps(diagnostic)},"unknown":{constant},'
            '"noisegate":{"max_chars":240,"max_lines":7}}'
        )

        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw


def test_reduce_json_write_diagnostic_lone_surrogates_fail_open_exactly() -> None:
    diagnostic = numbered("src/file.py:10:2: error E100 useful diagnostic", 120)

    for escaped_surrogate in (r"\ud800", r"\udfff"):
        raw = (
            '{"tool_name":"write_file","diagnostics":'
            f'{json.dumps(diagnostic)},"source":"{escaped_surrogate}",'
            '"noisegate":{"max_chars":240,"max_lines":7}}'
        )

        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw


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
        nested = {**exit_hints, "output": numbered("ERROR nested", 100)}
        payloads = (
            {
                "tool_name": "process",
                "args": {"action": "wait"},
                "result": nested,
                "noisegate": {"max_chars": 120},
            },
            {
                "tool_name": "process",
                "args": {"action": "wait"},
                "result": json.dumps(nested),
                "noisegate": {"max_chars": 120},
            },
            {
                "tool_name": "process",
                "args": {"action": "wait"},
                **exit_hints,
                "output": numbered("ERROR direct", 100),
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
        "args": {"action": "wait"},
        "status": "failed",
        "result": {"output": numbered("ERROR inner", 100)},
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
    nested = {"output": numbered("ERROR inner", 100)}
    payload = {
        "tool_name": "process",
        "args": {"action": "wait"},
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
        "args": {"action": "wait"},
        "status": "failed",
        "exit_code": 0,
        "result": {"output": numbered("ERROR inner", 100)},
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
        "args": {"action": "wait"},
        "status": "failed",
        "returncode": 7,
        "result": {"output": numbered("ERROR inner", 100)},
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
        "args": {"action": "wait"},
        "exit_code": 0,
        "result": {"status": "failed", "output": numbered("ERROR inner", 100)},
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
    nested = {"status": "failed", "output": numbered("ERROR inner", 100)}
    payload = {
        "tool_name": "process",
        "args": {"action": "wait"},
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


def test_reduce_json_nested_success_and_direct_exhaustion_abort_atomically(
    monkeypatch,
) -> None:
    created_budgets: list[engine._SourceAlignmentWorkBudget] = []

    def new_budget() -> engine._SourceAlignmentWorkBudget:
        budget = engine._SourceAlignmentWorkBudget(500)
        created_budgets.append(budget)
        return budget

    monkeypatch.setattr(engine, "_new_source_alignment_work_budget", new_budget)
    payload = mixed_nested_exhaustion_payload()
    raw = json.dumps(payload, separators=(",", ":"))
    metadata: dict[str, object] = {}

    output = cli._reduce_json_value(
        payload,
        raw,
        engine.NoisegateOptions(
            max_chars=500,
            max_lines=22,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
        ),
        metadata_out=metadata,
    )

    assert output == raw
    assert metadata == {}
    assert len(created_budgets) == 1
    assert created_budgets[0].exhausted is True
    assert engine._SOURCE_ALIGNMENT_WORK_BUDGET.get() is None


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
    # At 41 lines the compacted inner value plus metadata is exactly no smaller.
    nested = {"stdout": numbered("line", 41)}
    payload = {
        "tool_name": "terminal",
        "result": json.dumps(nested, separators=(",", ":")),
        "noisegate": {"max_chars": 80},
    }
    raw = json.dumps(payload, separators=(",", ":"))
    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_reduce_json_nested_json_string_rejects_whitespace_only_artifact_gain(
    tmp_path: Path,
) -> None:
    text = "\n".join(
        [
            "head",
            "[noisegate: omitted 8 lines]",
            "x" * 80,
            "y" * 80,
            "tail",
        ]
    )
    payload = {
        "tool_name": "terminal",
        "status": "failed",
        "result": " " + json.dumps(text) + " ",
        "noisegate": {
            "max_chars": 10_000,
            "max_lines": 4,
            "head_lines": 1,
            "tail_lines": 1,
        },
    }
    raw = json.dumps(payload)
    artifact_dir = tmp_path / "artifacts"

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


def test_reduce_json_nested_json_string_stores_planned_artifact_before_delivery(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    text = "\n".join(
        [
            "head",
            "[noisegate: omitted 8 lines]",
            *[f"middle-{index}-" + ("x" * 80) for index in range(30)],
            "tail",
        ]
    )
    raw = json.dumps(
        {
            "tool_name": "terminal",
            "command": "pytest -q",
            "status": "failed",
            "result": json.dumps(text),
        }
    )
    artifact_dir = tmp_path / "artifacts"
    store_calls: list[str] = []
    real_store = plugin._store_artifact

    def tracking_store(raw_text: str, options):
        store_calls.append(raw_text)
        return real_store(raw_text, options)

    monkeypatch.setattr(plugin, "_store_artifact", tracking_store)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))

    return_code = cli.main(
        [
            "reduce-json",
            "--store-artifact",
            "--artifact-dir",
            str(artifact_dir),
            "--max-chars",
            "1000",
            "--max-lines",
            "5",
        ]
    )

    output = capsys.readouterr().out
    nested_result = json.loads(json.loads(output)["result"])
    artifact_files = list(artifact_dir.glob("ng_*.txt"))
    assert return_code == 0
    assert store_calls == [text]
    assert len(artifact_files) == 1
    assert artifact_files[0].stem in nested_result
    assert nested_result.splitlines().count("[noisegate: exit_code=1]") == 1
    assert ArtifactStore(artifact_dir).read(artifact_files[0].stem) == text


def test_reduce_json_rejects_unresolvable_successful_artifact_store(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    text = "\n".join(
        [
            "head",
            "[noisegate: omitted 8 lines]",
            *[f"middle-{index}-" + ("x" * 80) for index in range(30)],
            "tail",
        ]
    )
    raw = json.dumps(
        {
            "tool_name": "terminal",
            "command": "pytest -q",
            "status": "failed",
            "result": json.dumps(text),
        }
    )
    artifact_dir = tmp_path / "artifacts"
    store_calls: list[str] = []

    def fake_store(raw_text: str, options):
        store_calls.append(raw_text)
        return plugin._plan_artifact(raw_text, options)

    monkeypatch.setattr(plugin, "_store_artifact", fake_store)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))

    return_code = cli.main(
        [
            "reduce-json",
            "--store-artifact",
            "--artifact-dir",
            str(artifact_dir),
            "--max-chars",
            "1000",
            "--max-lines",
            "5",
        ]
    )

    output = capsys.readouterr().out
    assert return_code == 0
    assert store_calls == [text]
    assert output == raw
    assert "id=ng_" not in output
    assert not artifact_dir.exists()


def test_reduce_json_nested_json_string_plan_mismatch_fails_open(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    text = "\n".join(
        [
            "head",
            "[noisegate: omitted 8 lines]",
            *[f"middle-{index}-" + ("x" * 80) for index in range(30)],
            "tail",
        ]
    )
    raw = json.dumps(
        {
            "tool_name": "terminal",
            "command": "pytest -q",
            "status": "failed",
            "result": json.dumps(text),
        }
    )
    artifact_dir = tmp_path / "artifacts"
    real_plan = plugin._plan_artifact

    def mismatched_plan(raw_text: str, options):
        planned = real_plan(raw_text, options)
        return {**planned, "id": "ng_" + ("0" * 24)}

    def unexpected_store(_text: str, _options) -> dict[str, object]:
        raise AssertionError("a mismatched preview plan must not be stored")

    monkeypatch.setattr(plugin, "_plan_artifact", mismatched_plan)
    monkeypatch.setattr(plugin, "_store_artifact", unexpected_store)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))

    return_code = cli.main(
        [
            "reduce-json",
            "--store-artifact",
            "--artifact-dir",
            str(artifact_dir),
            "--max-chars",
            "1000",
            "--max-lines",
            "5",
        ]
    )

    output = capsys.readouterr().out
    assert return_code == 0
    assert output == raw
    assert "id=ng_" not in output
    assert not artifact_dir.exists()


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
    for first_line in (
        "{not json",
        "2026-07-14 build started",
        "- warning",
        "[2026-07-14 09:04:09] build started",
        "[123] build started",
    ):
        envelope = {
            "tool_name": "terminal",
            "args": {"command": "pytest"},
            "result": first_line + "\n" + numbered("line", 100),
            "noisegate": {"max_chars": 120},
        }
        proc = run_cli("reduce-json", input_text=json.dumps(envelope))

        assert proc.returncode == 0, proc.stderr
        outer = json.loads(proc.stdout)
        assert outer["result"].startswith(first_line)
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


def test_reduce_json_tool_call_uses_real_tool_name_for_plain_result() -> None:
    envelope = {
        "tool_name": "tool_call",
        "args": {"name": "terminal", "arguments": {"command": "pytest -q"}},
        "result": numbered("pytest wrapper output", 100),
        "noisegate": {"max_chars": 120},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    outer = json.loads(proc.stdout)
    assert "[noisegate: omitted" in outer["result"]
    assert outer["result"] != envelope["result"]


def test_reduce_json_tool_call_preserves_mcp_plain_result() -> None:
    envelope = {
        "tool_name": "tool_call",
        "args": {"name": "mcp_github_list_issues", "arguments": {"repo": "Tosko4/noisegate"}},
        "result": numbered("github issue", 100),
        "noisegate": {"max_chars": 120},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == envelope


def test_reduce_json_tool_call_preserves_json_encoded_mcp_result() -> None:
    nested = json.dumps(
        {"tool_name": "mcp_github_get_file", "content": numbered("source", 100)}
    )
    envelope = {
        "tool_name": "tool_call",
        "args": {"name": "browser_console", "arguments": {}},
        "result": nested,
        "logs": numbered("browser noise", 100),
        "noisegate": {"max_chars": 600},
    }
    raw = json.dumps(envelope)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    outer = json.loads(proc.stdout)
    assert outer["result"] == nested
    assert json.loads(outer["result"])["tool_name"].startswith("mcp_")
    assert "[noisegate: omitted" in outer["logs"]


def test_reduce_json_tool_call_preserves_generic_json_result_with_noisy_sibling() -> None:
    nested = json.dumps({"items": [f"value {index:03d}" for index in range(100)]})
    envelope = {
        "tool_name": "tool_call",
        "args": {"name": "browser_console", "arguments": {}},
        "result": nested,
        "logs": numbered("browser noise", 100),
        "noisegate": {"max_chars": 600},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    outer = json.loads(proc.stdout)
    assert outer["result"] == nested
    assert "[noisegate: omitted" in outer["logs"]


def test_reduce_json_tool_call_compacts_json_encoded_terminal_result() -> None:
    nested = json.dumps(
        {
            "tool_name": "terminal",
            "name": "pytest shard",
            "stdout": numbered("pytest output", 100),
            "exit": 0,
        }
    )
    envelope = {
        "tool_name": "tool_call",
        "args": {"name": "terminal", "arguments": {"command": "pytest -q"}},
        "result": nested,
        "noisegate": {"max_chars": 600},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    outer = json.loads(proc.stdout)
    nested_result = json.loads(outer["result"])
    assert nested_result["exit"] == 0
    assert nested_result["name"] == "pytest shard"
    assert "[noisegate: omitted" in nested_result["stdout"]


def test_reduce_json_tool_call_preserves_scalar_json_results() -> None:
    for nested in ("null   ", "false\n", "123.5e-2\t"):
        envelope = {
            "tool_name": "tool_call",
            "args": {"name": "browser_console", "arguments": {}},
            "result": nested,
            "logs": numbered("browser noise", 100),
            "noisegate": {"max_chars": 600},
        }

        proc = run_cli("reduce-json", input_text=json.dumps(envelope))

        assert proc.returncode == 0, proc.stderr
        outer = json.loads(proc.stdout)
        assert outer["result"] == nested
        assert "[noisegate: omitted" in outer["logs"]


def test_reduce_json_tool_call_rejects_invalid_deep_json_result() -> None:
    duplicate = (
        '{"tool_name":"mcp_github_get_file",'
        '"tool_name":"browser_console",'
        f'"content":{json.dumps(numbered("source", 100))}}}'
    )
    malformed = json.dumps('{"tool_name":"mcp_github_get_file"')
    nonstandard = tuple(
        json.dumps(value)
        for value in ("NaN", "Infinity", "{foo: 1}", "['x']", "[undefined]")
    )
    direct_invalid = (
        '{"tool_name":"mcp_github_get_file"',
        '{tool_name: "mcp_github_get_file"}',
        '{1: "non-standard"}',
        '{1e2: "non-standard"}',
        "[1,]",
        "['x']",
        "[undefined]",
        "NaN",
        '\ufeff{"tool_name":"mcp_github_get_file"}',
    )
    for result in (json.dumps(duplicate), malformed, *nonstandard, *direct_invalid):
        envelope = {
            "tool_name": "tool_call",
            "args": {"name": "browser_console", "arguments": {}},
            "result": result,
            "logs": numbered("browser noise", 100),
            "noisegate": {"max_chars": 600},
        }
        raw = json.dumps(envelope)

        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw


def test_reduce_json_tool_call_preserves_unwrapped_exact_command() -> None:
    envelope = {
        "tool_name": "tool_call",
        "command": "pytest -q",
        "args": {
            "name": "terminal",
            "arguments": {"command": "cat important.py"},
        },
        "result": numbered("source", 100),
        "noisegate": {"max_chars": 120},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == envelope


def test_reduce_json_tool_call_ignores_result_identity_without_wrapper_identity() -> None:
    envelope = {
        "tool_name": "tool_call",
        "args": {},
        "result": {"tool_name": "terminal", "stdout": numbered("exact", 100)},
        "noisegate": {"max_chars": 120},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(envelope))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == envelope


def test_reduce_json_tool_call_container_preserves_exact_command() -> None:
    for container in ("call", "request"):
        envelope = {
            "tool_name": "tool_call",
            "args": {
                container: {
                    "name": "terminal",
                    "arguments": {"command": "cat important.py"},
                }
            },
            "result": numbered("source", 100),
            "noisegate": {"max_chars": 120},
        }

        proc = run_cli("reduce-json", input_text=json.dumps(envelope))

        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == envelope


def test_reduce_json_tool_call_container_compacts_noisy_command() -> None:
    for container in ("call", "request"):
        envelope = {
            "tool_name": "tool_call",
            "args": {
                container: {
                    "name": "terminal",
                    "arguments": {"command": "pytest -q"},
                }
            },
            "result": numbered("pytest output", 100),
            "noisegate": {"max_chars": 120},
        }

        proc = run_cli("reduce-json", input_text=json.dumps(envelope))

        assert proc.returncode == 0, proc.stderr
        assert "[noisegate: omitted" in json.loads(proc.stdout)["result"]


def test_reduce_json_wrapper_preserves_exact_command_against_nested_stale_hint() -> None:
    source = numbered("source", 100)
    nested = {"args": {"command": "pytest -q"}, "stdout": source, "exit_code": 0}

    for result in (nested, json.dumps(nested)):
        envelope = {
            "tool_name": "tool_call",
            "args": {
                "name": "terminal",
                "arguments": {"command": "cat important.py"},
            },
            "result": result,
            "noisegate": {"max_chars": 120},
        }
        raw = json.dumps(envelope)

        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw


def test_reduce_json_duplicate_wrapper_identity_keys_fail_open() -> None:
    result = json.dumps(numbered("source", 100))
    raw = (
        '{"tool_name":"tool_call","args":'
        '{"name":"mcp_github_get_file","name":"terminal",'
        '"arguments":{"command":"pytest -q"}},'
        f'"result":{result},"noisegate":{{"max_chars":120}}}}'
    )

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_reduce_json_duplicate_nested_result_keys_fail_open() -> None:
    first = json.dumps(numbered("exact source", 100))
    second = json.dumps(numbered("pytest output", 100))
    nested = f'{{"stdout":{first},"stdout":{second},"exit":0}}'
    envelope = {
        "tool_name": "tool_call",
        "args": {"name": "terminal", "arguments": {"command": "pytest -q"}},
        "result": nested,
        "noisegate": {"max_chars": 120},
    }
    raw = json.dumps(envelope)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_reduce_json_conflicting_nested_result_identity_aliases_fail_open() -> None:
    envelope = {
        "tool_name": "tool_call",
        "args": {"name": "terminal", "arguments": {"command": "pytest -q"}},
        "result": {
            "tool_name": "terminal",
            "toolName": "mcp_github_get_file",
            "stdout": numbered("exact source", 100),
        },
        "noisegate": {"max_chars": 120},
    }
    raw = json.dumps(envelope)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_reduce_json_secondary_result_command_conflict_fails_open() -> None:
    envelope = {
        "tool_name": "tool_call",
        "args": {"name": "terminal", "arguments": {"command": "pytest -q"}},
        "result": {
            "args": {"command": "pytest -q", "cmd": "git status --short"},
            "stdout": numbered("exact source", 100),
            "exit": 0,
        },
        "noisegate": {"max_chars": 120},
    }
    raw = json.dumps(envelope)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_reduce_json_wrapper_and_nested_identity_mismatch_fails_open() -> None:
    envelope = {
        "tool_name": "tool_call",
        "args": {"name": "terminal", "arguments": {"command": "pytest -q"}},
        "result": {
            "tool_name": "browser_console",
            "content": numbered("conflicting owner", 100),
        },
        "noisegate": {"max_chars": 120},
    }
    raw = json.dumps(envelope)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_reduce_json_object_form_protected_result_identity_fails_open() -> None:
    envelope = {
        "tool_name": "tool_call",
        "args": {"name": "terminal", "arguments": {"command": "pytest -q"}},
        "result": {
            "tool": {"name": "mcp_github_get_file"},
            "stdout": numbered("exact source", 100),
        },
        "noisegate": {"max_chars": 120},
    }
    raw = json.dumps(envelope)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_reduce_json_result_input_command_conflict_fails_open() -> None:
    envelope = {
        "tool_name": "tool_call",
        "args": {"name": "terminal", "arguments": {"command": "pytest -q"}},
        "result": {
            "input": {"command": "cat important.py"},
            "stdout": numbered("exact source", 100),
            "exit": 0,
        },
        "noisegate": {"max_chars": 120},
    }
    raw = json.dumps(envelope)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_reduce_json_nonstandard_numeric_constants_fail_open() -> None:
    for constant in ("NaN", "Infinity", "-Infinity"):
        raw = (
            '{"tool_name":"tool_call","args":{"name":"terminal",'
            '"arguments":{"command":"pytest -q"}},"result":{"stdout":'
            f'{json.dumps(numbered("pytest output", 100))},"exit":{constant}}},'
            '"noisegate":{"max_chars":120}}'
        )

        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw


def test_reduce_json_nested_protected_result_identity_fails_open() -> None:
    envelope = {
        "tool_name": "tool_call",
        "args": {"name": "terminal", "arguments": {"command": "pytest -q"}},
        "result": {
            "input": {"tool": {"name": "mcp_github_get_file"}},
            "stdout": numbered("exact source", 100),
            "exit": 0,
        },
        "noisegate": {"max_chars": 120},
    }
    raw = json.dumps(envelope)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_reduce_json_top_level_name_wrapper_preserves_protected_tool() -> None:
    envelope = {
        "name": "tool_call",
        "args": {"name": "mcp_github_get_file", "arguments": {}},
        "command": "pytest -q",
        "result": numbered("exact source", 100),
        "noisegate": {"max_chars": 120},
    }
    raw = json.dumps(envelope)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_reduce_json_does_not_promote_ordinary_top_level_name_to_tool_identity() -> None:
    for name in ("terminal", "browser_console"):
        envelope = {
            "name": name,
            "output": numbered("exact payload", 100),
            "noisegate": {"max_chars": 120},
        }
        raw = json.dumps(envelope)

        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw


def test_reduce_json_top_level_name_wrapper_propagates_nested_exact_command() -> None:
    envelope = {
        "name": "tool_call",
        "args": {
            "name": "terminal",
            "arguments": {"input": {"command": "cat important.py"}},
        },
        "stdout": numbered("exact source", 100),
        "noisegate": {"max_chars": 120},
    }
    raw = json.dumps(envelope)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_reduce_json_direct_tool_name_wrapper_compacts_and_preserves_identity() -> None:
    envelope = {
        "tool_name": "tool_call",
        "args": {"name": "terminal", "arguments": {"command": "pytest -q"}},
        "stdout": numbered("pytest output", 100),
        "exit": 0,
        "noisegate": {"max_chars": 120},
    }
    raw = json.dumps(envelope)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout != raw
    transformed = json.loads(proc.stdout)
    assert transformed["tool_name"] == "tool_call"
    assert transformed["args"] == envelope["args"]
    assert "[noisegate: omitted" in transformed["stdout"]


def test_reduce_json_explicit_terminal_preserves_ordinary_name_label() -> None:
    envelope = {
        "tool_name": "terminal",
        "name": "pytest shard",
        "command": "pytest -q",
        "stdout": numbered("pytest output", 100),
        "exit": 0,
        "noisegate": {"max_chars": 120},
    }
    raw = json.dumps(envelope)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout != raw
    transformed = json.loads(proc.stdout)
    assert transformed["tool_name"] == "terminal"
    assert transformed["name"] == "pytest shard"
    assert "[noisegate: omitted" in transformed["stdout"]


def test_reduce_json_explicit_terminal_preserves_ordinary_result_name_label() -> None:
    envelope = {
        "tool_name": "terminal",
        "command": "pytest -q",
        "result": {
            "name": "pytest shard",
            "stdout": numbered("pytest output", 100),
            "exit": 0,
        },
        "noisegate": {"max_chars": 120},
    }
    raw = json.dumps(envelope)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout != raw
    transformed = json.loads(proc.stdout)
    assert transformed["result"]["name"] == "pytest shard"
    assert "[noisegate: omitted" in transformed["result"]["stdout"]


def test_reduce_json_wrapper_rejects_protected_identity_in_resolved_arguments() -> None:
    for direct in (False, True):
        envelope = {
            "tool_name": "tool_call",
            "args": {
                "name": "terminal",
                "arguments": {
                    "tool_name": "mcp_github_get_file",
                    "command": "pytest -q",
                },
            },
            "noisegate": {"max_chars": 120},
        }
        if direct:
            envelope.update({"stdout": numbered("exact source", 100), "exit": 0})
        else:
            envelope["result"] = {
                "stdout": numbered("exact source", 100),
                "exit": 0,
            }
        raw = json.dumps(envelope)

        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw


def test_reduce_json_wrapper_rejects_root_name_identity_in_arguments() -> None:
    envelope = {
        "tool_name": "tool_call",
        "args": {
            "name": "terminal",
            "arguments": {
                "name": "mcp_github_get_file",
                "command": "pytest -q",
            },
        },
        "result": {"stdout": numbered("exact source", 100), "exit": 0},
        "noisegate": {"max_chars": 120},
    }
    raw = json.dumps(envelope)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_reduce_json_wrapper_treats_root_result_name_as_label() -> None:
    envelope = {
        "tool_name": "tool_call",
        "args": {"name": "terminal", "arguments": {"command": "pytest -q"}},
        "result": {
            "name": "pytest shard",
            "stdout": numbered("pytest output", 100),
            "exit": 0,
        },
        "noisegate": {"max_chars": 120},
    }
    raw = json.dumps(envelope)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    transformed = json.loads(proc.stdout)
    assert transformed["result"]["name"] == "pytest shard"
    assert "[noisegate: omitted" in transformed["result"]["stdout"]


def test_reduce_json_rejects_explicit_wrapper_conflict_or_bare_name() -> None:
    cases = (
        {
            "tool_name": "terminal",
            "name": "tool_call",
            "command": "pytest -q",
            "result": numbered("exact source", 100),
            "noisegate": {"max_chars": 120},
        },
        {
            "name": "mcp_github_get_file",
            "command": "pytest -q",
            "result": numbered("exact source", 100),
            "noisegate": {"max_chars": 120},
        },
    )
    for envelope in cases:
        raw = json.dumps(envelope)

        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw


def test_reduce_json_wrapper_rejects_sibling_call_ownership() -> None:
    for direct in (False, True):
        envelope = {
            "tool_name": "tool_call",
            "args": {
                "name": "terminal",
                "arguments": {"command": "pytest -q"},
                "call": {"name": "mcp_github_get_file", "arguments": {}},
            },
            "noisegate": {"max_chars": 120},
        }
        if direct:
            envelope.update({"stdout": numbered("exact source", 100), "exit": 0})
        else:
            envelope["result"] = {
                "stdout": numbered("exact source", 100),
                "exit": 0,
            }
        raw = json.dumps(envelope)

        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw


def test_reduce_json_wrapper_rejects_identityless_sibling_ownership() -> None:
    for sibling in (
        {"tool_name": "mcp_github_get_file"},
        {"command": "cat important.py"},
    ):
        envelope = {
            "tool_name": "tool_call",
            "args": {
                "call": {
                    "name": "terminal",
                    "arguments": {"command": "pytest -q"},
                },
                "arguments": sibling,
            },
            "result": numbered("exact source", 100),
            "noisegate": {"max_chars": 120},
        }
        raw = json.dumps(envelope)

        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw


def test_reduce_json_rejects_malformed_explicit_identity_types() -> None:
    for malformed in (
        {"tool_name": {"name": "mcp_github_get_file"}},
        {"toolName": ["terminal"]},
        {"tool": 7},
    ):
        envelope = {
            **malformed,
            "command": "pytest -q",
            "stdout": numbered("exact source", 100),
            "exit": 0,
            "noisegate": {"max_chars": 120},
        }
        raw = json.dumps(envelope)

        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw


def test_reduce_json_wrapper_rejects_malformed_name_identity_in_arguments() -> None:
    envelope = {
        "tool_name": "tool_call",
        "args": {
            "name": "terminal",
            "arguments": {
                "name": ["mcp_github_get_file"],
                "command": "pytest -q",
            },
        },
        "result": numbered("exact source", 100),
        "noisegate": {"max_chars": 120},
    }
    raw = json.dumps(envelope)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_reduce_json_wrapper_treats_nonstring_root_result_name_as_label() -> None:
    envelope = {
        "tool_name": "tool_call",
        "args": {"name": "terminal", "arguments": {"command": "pytest -q"}},
        "result": {
            "name": ["pytest shard"],
            "stdout": numbered("pytest output", 100),
            "exit": 0,
        },
        "noisegate": {"max_chars": 120},
    }
    raw = json.dumps(envelope)

    proc = run_cli("reduce-json", input_text=raw)

    assert proc.returncode == 0, proc.stderr
    transformed = json.loads(proc.stdout)
    assert transformed["result"]["name"] == ["pytest shard"]
    assert "[noisegate: omitted" in transformed["result"]["stdout"]


def test_reduce_json_identityless_result_root_name_stays_exact() -> None:
    for result in (
        {
            "name": "pytest shard",
            "stdout": numbered("exact payload", 100),
            "exit": 0,
        },
        json.dumps(
            {
                "name": "pytest shard",
                "stdout": numbered("exact payload", 100),
                "exit": 0,
            }
        ),
        {
            "name": ["pytest shard"],
            "stdout": numbered("exact payload", 100),
            "exit": 0,
        },
    ):
        envelope = {
            "args": {"command": "pytest -q"},
            "result": result,
            "noisegate": {"max_chars": 120},
        }
        raw = json.dumps(envelope)

        proc = run_cli("reduce-json", input_text=raw)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw


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


def test_reduce_json_preview_does_not_export_plan_when_plain_result_notice_drops() -> None:
    result = "\n".join(["ValueError: boom", *["x" * 40 for _ in range(30)]])
    payload = {
        "tool_name": "terminal",
        "command": "pytest",
        "result": result,
    }
    raw = json.dumps(payload)
    options = cli.NoisegateOptions(
        max_chars=139,
        max_lines=3,
        artifact_enabled=True,
        artifact_dir=Path("a"),
    )
    plans: list[plugin._ArtifactPreviewPlan] = []

    output = cli._reduce_json_value(
        payload,
        raw,
        options,
        defer_artifact_store=True,
        artifact_plans_out=plans,
    )

    assert output != raw
    assert "ng_" not in output
    assert plans == []


def test_reduce_json_preview_discards_sibling_plan_on_alignment_exhaustion(
    monkeypatch,
    tmp_path: Path,
) -> None:
    created_budgets: list[engine._SourceAlignmentWorkBudget] = []

    def new_budget() -> engine._SourceAlignmentWorkBudget:
        budget = engine._SourceAlignmentWorkBudget(500)
        created_budgets.append(budget)
        return budget

    monkeypatch.setattr(engine, "_new_source_alignment_work_budget", new_budget)
    payload = mixed_nested_exhaustion_payload()
    raw = json.dumps(payload, separators=(",", ":"))
    metadata: dict[str, object] = {}
    plans: list[plugin._ArtifactPreviewPlan] = []

    output = cli._reduce_json_value(
        payload,
        raw,
        engine.NoisegateOptions(
            max_chars=500,
            max_lines=22,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
            artifact_enabled=True,
            artifact_dir=tmp_path / "artifacts",
        ),
        metadata_out=metadata,
        defer_artifact_store=True,
        artifact_plans_out=plans,
    )

    assert output == raw
    assert metadata == {}
    assert plans == []
    assert len(created_budgets) == 1
    assert created_budgets[0].exhausted is True
    assert not (tmp_path / "artifacts").exists()


def test_reduce_json_command_never_stores_preview_plan_after_sibling_exhaustion(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    artifact_dir = tmp_path / "artifacts"
    payload = mixed_nested_exhaustion_payload()
    payload["noisegate"] = {
        "max_chars": 500,
        "max_lines": 22,
        "head_lines": 0,
        "tail_lines": 0,
        "important_context_lines": 0,
    }
    raw = json.dumps(payload, separators=(",", ":"))
    store_calls: list[plugin._ArtifactPreviewPlan] = []

    def new_budget() -> engine._SourceAlignmentWorkBudget:
        return engine._SourceAlignmentWorkBudget(500)

    def unexpected_store(
        plan: plugin._ArtifactPreviewPlan,
        _options: engine.NoisegateOptions,
    ) -> bool:
        store_calls.append(plan)
        return True

    monkeypatch.setattr(engine, "_new_source_alignment_work_budget", new_budget)
    monkeypatch.setattr(cli, "_store_artifact_preview_plan", unexpected_store)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))

    return_code = cli.main(
        [
            "reduce-json",
            "--store-artifact",
            "--artifact-dir",
            str(artifact_dir),
        ]
    )

    captured = capsys.readouterr()
    assert return_code == 0
    assert captured.out == raw
    assert captured.err == ""
    assert store_calls == []
    assert not artifact_dir.exists()


def test_reduce_json_exhaustion_creates_no_real_artifact_or_id(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    artifact_dir = tmp_path / "artifacts"
    payload = mixed_nested_exhaustion_payload()
    payload["noisegate"] = {
        "max_chars": 500,
        "max_lines": 22,
        "head_lines": 0,
        "tail_lines": 0,
        "important_context_lines": 0,
    }
    raw = json.dumps(payload, separators=(",", ":"))

    def new_budget() -> engine._SourceAlignmentWorkBudget:
        return engine._SourceAlignmentWorkBudget(500)

    monkeypatch.setattr(engine, "_new_source_alignment_work_budget", new_budget)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))

    return_code = cli.main(
        [
            "reduce-json",
            "--store-artifact",
            "--artifact-dir",
            str(artifact_dir),
        ]
    )

    output = capsys.readouterr().out
    assert return_code == 0
    assert output == raw
    assert "id=ng_" not in output
    assert not artifact_dir.exists()


def test_reduce_json_preview_plans_bind_notices_to_exact_accepted_owners() -> None:
    artifact_dir = Path("a")
    options = cli.NoisegateOptions(
        max_chars=500,
        artifact_enabled=True,
        artifact_dir=artifact_dir,
    )
    cases = {
        "plain": (
            {
                "tool_name": "terminal",
                "command": "pytest",
                "returncode": 1,
                "result": numbered("plain", 200),
            },
            [(("result", False),)],
        ),
        "nested": (
            {
                "tool_name": "terminal",
                "command": "pytest",
                "returncode": 1,
                "result": json.dumps({"stdout": numbered("inner", 200)}),
            },
            [(("result", True), ("stdout", False))],
        ),
        "direct": (
            {
                "tool_name": "terminal",
                "command": "make noisy",
                "stdout": numbered("direct", 600),
                "noisegate": {
                    "artifact": {"stored": True, "id": "ng_metadata_collision"}
                },
                "_noisegate": {"prior": "metadata"},
            },
            [(("stdout", False),)],
        ),
        "mixed": (
            {
                "tool_name": "terminal",
                "command": "make noisy",
                "stdout": numbered("outer", 600),
                "result": json.dumps({"stdout": numbered("inner", 600)}),
            },
            [
                (("result", True), ("stdout", False)),
                (("stdout", False),),
            ],
        ),
    }
    previews: dict[str, tuple[str, list[plugin._ArtifactPreviewPlan]]] = {}

    for name, (payload, expected_owners) in cases.items():
        raw = json.dumps(payload, separators=(",", ":"))
        plans: list[plugin._ArtifactPreviewPlan] = []
        preview = cli._reduce_json_value(
            payload,
            raw,
            options,
            defer_artifact_store=True,
            artifact_plans_out=plans,
        )

        assert preview != raw, name
        assert [
            tuple((step.field, step.parse_json) for step in plan.owner_path)
            for plan in plans
        ] == expected_owners
        assert all(
            plugin._artifact_preview_plan_matches_serialized_output(plan, preview)
            for plan in plans
        )
        previews[name] = (preview, plans)

    plain_preview, plain_plans = previews["plain"]
    plain_payload = json.loads(plain_preview)
    plain_payload["result"] = plain_payload["result"].replace(
        plain_plans[0].recovery_notice,
        "",
    )
    plain_payload["metadata_collision"] = {
        "stored": True,
        "id": plain_plans[0].artifact_id,
        "notice": plain_plans[0].recovery_notice,
    }
    assert not plugin._artifact_preview_plan_matches_serialized_output(
        plain_plans[0],
        json.dumps(plain_payload),
    )

    nested_preview, nested_plans = previews["nested"]
    nested_payload = json.loads(nested_preview)
    nested_result = json.loads(nested_payload["result"])
    nested_result["stdout"] = nested_result["stdout"].replace(
        nested_plans[0].recovery_notice,
        "",
    )
    nested_payload["result"] = json.dumps(nested_result)
    nested_payload["stdout"] = nested_plans[0].recovery_notice
    assert not plugin._artifact_preview_plan_matches_serialized_output(
        nested_plans[0],
        json.dumps(nested_payload),
    )

    direct_preview, direct_plans = previews["direct"]
    direct_payload = json.loads(direct_preview)
    direct_payload["stdout"] = direct_payload["stdout"].replace(
        direct_plans[0].recovery_notice,
        "",
    )
    direct_payload["noisegate"]["notice"] = direct_plans[0].recovery_notice
    assert not plugin._artifact_preview_plan_matches_serialized_output(
        direct_plans[0],
        json.dumps(direct_payload),
    )

    mixed_preview, mixed_plans = previews["mixed"]
    mixed_payload = json.loads(mixed_preview)
    mixed_result = json.loads(mixed_payload["result"])
    mixed_result["stdout"] = mixed_result["stdout"].replace(
        mixed_plans[0].recovery_notice,
        "",
    )
    mixed_payload["result"] = json.dumps(mixed_result)
    mixed_payload["stderr"] = mixed_plans[0].recovery_notice
    mixed_tampered = json.dumps(mixed_payload)
    assert not plugin._artifact_preview_plan_matches_serialized_output(
        mixed_plans[0],
        mixed_tampered,
    )
    assert plugin._artifact_preview_plan_matches_serialized_output(
        mixed_plans[1],
        mixed_tampered,
    )


def test_reduce_json_command_revalidates_plan_owner_before_store(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    artifact_dir = tmp_path / "artifacts"
    original = numbered("plain", 200)
    raw = json.dumps({"tool_name": "terminal", "result": original})
    options = cli.NoisegateOptions(
        artifact_enabled=True,
        artifact_dir=artifact_dir,
    )
    plan = plugin._artifact_preview_plan(
        original,
        {"artifact": plugin._plan_artifact(original, options)},
        artifact_dir=artifact_dir,
        owner_path=(plugin._ArtifactNoticeOwnerStep("result"),),
    )
    assert plan is not None
    preview = json.dumps(
        {
            "result": "inline result without recovery",
            "metadata_collision": {
                "stored": True,
                "id": plan.artifact_id,
                "notice": plan.recovery_notice,
            },
        }
    )
    inline_fallback = json.dumps({"result": "artifact-disabled inline result"})
    reduce_calls: list[bool] = []

    def fake_reduce(
        _parsed,
        _raw: str,
        current_options: cli.NoisegateOptions,
        *,
        metadata_out=None,
        defer_artifact_store: bool = False,
        artifact_plans_out=None,
    ) -> str:
        reduce_calls.append(current_options.artifact_enabled)
        if defer_artifact_store:
            assert artifact_plans_out is not None
            artifact_plans_out.append(plan)
            return preview
        return inline_fallback

    def unexpected_store(
        _plan: plugin._ArtifactPreviewPlan,
        _options: cli.NoisegateOptions,
    ) -> bool:
        raise AssertionError("mismatched owner must be rejected before store")

    monkeypatch.setattr(cli, "_reduce_json_value", fake_reduce)
    monkeypatch.setattr(cli, "_store_artifact_preview_plan", unexpected_store)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))

    return_code = cli.main(
        [
            "reduce-json",
            "--store-artifact",
            "--artifact-dir",
            str(artifact_dir),
        ]
    )

    assert return_code == 0
    assert capsys.readouterr().out == inline_fallback
    assert reduce_calls == [True, False]
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


def test_reduce_json_direct_field_artifact_preserves_noisegate_collision(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "artifacts"
    stdout = numbered("direct", 600)
    payload = {
        "tool_name": "terminal",
        "command": "make noisy",
        "stdout": stdout,
        "noisegate": {"prior": "metadata"},
        "_noisegate": {"prior": "fallback"},
    }

    proc = run_cli(
        "reduce-json",
        "--store-artifact",
        "--artifact-dir",
        str(artifact_dir),
        "--max-chars",
        "500",
        input_text=json.dumps(payload),
    )

    assert proc.returncode == 0, proc.stderr
    output = json.loads(proc.stdout)
    artifact = output["__noisegate"]["fields"]["stdout"]["artifact"]
    artifact_id = artifact["id"]
    assert output["noisegate"] == {"prior": "metadata"}
    assert output["_noisegate"] == {"prior": "fallback"}
    assert artifact_id in output["stdout"]
    assert ArtifactStore(artifact_dir).read(artifact_id) == stdout


def test_reduce_json_plain_result_store_failure_fails_open_without_id(tmp_path: Path) -> None:
    artifact_file = tmp_path / "not-a-dir"
    artifact_file.write_text("x", encoding="utf-8")
    payload = {
        "tool_name": "terminal",
        "command": "pytest",
        "returncode": 1,
        "result": numbered("plain", 200),
        "noisegate": {"max_chars": 300},
    }
    raw = json.dumps(payload)

    proc = run_cli(
        "reduce-json",
        "--store-artifact",
        "--artifact-dir",
        str(artifact_file),
        input_text=raw,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw
    assert "id=ng_" not in proc.stdout


def test_reduce_json_post_write_read_failure_has_no_file_or_delivered_id(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    artifact_dir = tmp_path / "artifacts"
    payload = {
        "tool_name": "terminal",
        "command": "pytest",
        "returncode": 1,
        "result": numbered("plain", 200),
        "noisegate": {"max_chars": 300},
    }
    raw = json.dumps(payload)

    def fail_read(_store: plugin.ArtifactStore, _artifact_id: str) -> str:
        raise plugin.ArtifactError("verification failed")

    monkeypatch.setattr(plugin.ArtifactStore, "read", fail_read)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))

    return_code = cli.main(
        [
            "reduce-json",
            "--store-artifact",
            "--artifact-dir",
            str(artifact_dir),
        ]
    )

    output = capsys.readouterr().out
    assert return_code == 0
    assert output == raw
    assert "id=ng_" not in output
    assert not list(artifact_dir.glob("*"))


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
    artifact_dir = Path("custom-artifacts")
    pythonpath = str(Path(__file__).resolve().parents[1])
    existing_pythonpath = os.environ.get("PYTHONPATH")
    if existing_pythonpath is not None:
        pythonpath = os.pathsep.join((pythonpath, existing_pythonpath))
    env = {"PYTHONPATH": pythonpath}
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
        env=env,
        cwd=tmp_path,
    )
    assert reduce_proc.returncode == 0, reduce_proc.stderr
    marker = "id=ng_"
    start = reduce_proc.stdout.index(marker) + len("id=")
    artifact_id = reduce_proc.stdout[start:].split(";", 1)[0]

    cat_proc = run_cli(
        "cat",
        "--artifact-dir",
        str(artifact_dir),
        artifact_id,
        env=env,
        cwd=tmp_path,
    )

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
