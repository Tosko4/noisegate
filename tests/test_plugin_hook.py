from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import noisegate.plugin as plugin
from noisegate import engine
from noisegate.plugin import register, transform_terminal_output, transform_tool_result

Hook = Callable[..., str | None]


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


def source_like_payload() -> str:
    return "\n".join(
        [
            "# Retrieved/source content that looks like a failing log.",
            "```python",
            "def fixture():",
            "    return 'FAILED ERROR Traceback npm ERR! Dockerfile'",
            "```",
            *[
                f"exact context line {index:03d}: FAILED ERROR Traceback npm ERR!"
                for index in range(90)
            ],
        ]
    )


def terminal_result(stdout: str, *, command: str = "pytest", exit_code: int = 0) -> str:
    return json.dumps(
        {
            "command": command,
            "stdout": stdout,
            "stderr": "",
            "exit": exit_code,
            "status": "failed" if exit_code else "ok",
        }
    )


def parse_hook_result(value: str | None) -> dict[str, Any]:
    assert isinstance(value, str)
    parsed = json.loads(value)
    assert isinstance(parsed, dict)
    return parsed


def test_register_adds_tool_and_terminal_output_hooks() -> None:
    class Host:
        def __init__(self) -> None:
            self.hooks: list[tuple[str, Hook]] = []

        def register_hook(self, name: str, callback: Hook) -> None:
            self.hooks.append((name, callback))

    host = Host()

    register(host)

    assert [name for name, _ in host.hooks] == [
        "transform_tool_result",
        "transform_terminal_output",
    ]
    assert host.hooks[0][1] is transform_tool_result
    assert host.hooks[1][1] is transform_terminal_output


def test_transform_tool_result_compacts_terminal_json() -> None:
    raw = terminal_result(numbered("stdout", 80), command="uv run pytest")

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        noisegate_max_chars=200,
        noisegate_head_lines=3,
        noisegate_tail_lines=2,
    )

    payload = parse_hook_result(transformed)
    stdout = payload["stdout"]
    assert isinstance(stdout, str)
    assert stdout.startswith("stdout 001\nstdout 002\nstdout 003")
    assert "stdout 080" in stdout
    assert payload["noisegate"]["compacted"] is True
    assert payload["noisegate"]["fields"]["stdout"]["reducer"] == "pytest"


def test_hermes_terminal_hooks_preserve_retrieval_output_exactly() -> None:
    output = numbered("expanded raw memory payload", 100)

    assert (
        transform_terminal_output(
            command="hermes lcm expand 123",
            output=output,
            noisegate_max_chars=120,
        )
        is None
    )
    raw = terminal_result(
        output,
        command="/opt/venv/bin/session_search retrieval",
    )
    assert (
        transform_tool_result(
            raw,
            tool_name="terminal",
            args={"command": "hermes lcm import archive.jsonl"},
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_result_payload_retrieval_beats_stale_pytest_args_and_diff_output() -> None:
    outputs = (
        "\n".join(
            f"tests/test_memory_{index}.py::test_recalled_{index} PASSED"
            for index in range(180)
        ),
        "\n".join(
            [
                "diff --git a/recalled.py b/recalled.py",
                "--- a/recalled.py",
                "+++ b/recalled.py",
                "@@ -1 +1 @@",
                "-old recalled evidence",
                "+new recalled evidence",
                *[f"+exact recalled diff line {index:03d}" for index in range(180)],
            ]
        ),
    )

    for output in outputs:
        raw = terminal_result(output, command="session_search git diff")
        assert (
            transform_tool_result(
                raw,
                tool_name="terminal",
                args={"command": "pytest -q"},
                noisegate_max_chars=160,
                noisegate_max_lines=20,
                noisegate_preserve_diffs=False,
            )
            is None
        )


def test_hermes_hooks_preserve_executed_retrieval_shell_forms_exactly() -> None:
    output = numbered("exact retrieved pytest-looking evidence PASSED", 180)
    commands = (
        "hermes 2>/dev/null lcm expand 123",
        "hermes lcm expand 123 <<<ignored",
        "exec /opt/bin/lcm_expand 123",
        "exec -a hermes /opt/hermes/bin/hermes --profile work lcm expand 123",
        "exec -aretrieval /opt/bin/lcm_expand 123",
        "exec -l /opt/bin/lcm_expand 123",
        "echo `echo \\`/opt/bin/session_search q\\``",
        "printf '%s' \"$(hermes lcm expand 123)\"",
        "printf '%s' \"$(value=`hermes lcm expand 123`; printf '%s' \"$value\")\"",
    )

    for command in commands:
        assert (
            transform_terminal_output(
                command=command,
                output=output,
                noisegate_max_chars=160,
            )
            is None
        ), command
        assert (
            transform_tool_result(
                terminal_result(output, command=command),
                tool_name="terminal",
                noisegate_max_chars=160,
            )
            is None
        ), command


def test_hermes_hooks_preserve_profiled_retrieval_output_exactly() -> None:
    output = numbered("exact profiled retrieval evidence PASSED", 180)
    commands = (
        "hermes --profile work lcm expand 123",
        "hermes --profile=work lcm expand 123",
        "hermes -p work lcm expand 123",
        "hermes -pwork lcm expand 123",
        "hermes --yolo --profile work lcm expand 123",
        "hermes --pass-session-id lcm expand 123",
        "hermes --ignore-user-config lcm expand 123",
        "hermes --ignore-rules lcm expand 123",
        "hermes --tui lcm expand 123",
        "hermes --resume=session-123 lcm expand 123",
        "hermes --continue --yolo lcm expand 123",
        "exec /opt/hermes/bin/hermes --profile work 2>/dev/null lcm expand 123 <<<ignored",
        "printf '%s' \"$(hermes --profile work lcm expand 123)\"",
    )

    for command in commands:
        assert (
            transform_terminal_output(
                command=command,
                output=output,
                noisegate_max_chars=160,
            )
            is None
        ), command
        assert (
            transform_tool_result(
                terminal_result(output, command=command),
                tool_name="terminal",
                noisegate_max_chars=160,
            )
            is None
        ), command


def test_hermes_hooks_keep_profiled_maintenance_output_compactable() -> None:
    output = numbered("Hermes maintenance progress", 180)

    for command in (
        "hermes --profile work lcm import archive.jsonl",
        "hermes -p work lcm doctor --reindex",
        "hermes --ignore-rules lcm import archive.jsonl",
        "hermes --pass-session-id lcm doctor --reindex",
    ):
        terminal_output = transform_terminal_output(
            command=command,
            output=output,
            noisegate_max_chars=160,
        )
        assert isinstance(terminal_output, str), command
        assert "[noisegate: omitted" in terminal_output, command

        tool_output = transform_tool_result(
            terminal_result(output, command=command),
            tool_name="terminal",
            noisegate_max_chars=160,
        )
        payload = parse_hook_result(tool_output)
        assert payload["noisegate"]["compacted"] is True, command
        assert "[noisegate: omitted" in payload["stdout"], command


def test_explicit_terminal_tool_preserves_ordinary_payload_name_label() -> None:
    raw = json.dumps(
        {
            "name": "pytest shard",
            "stdout": numbered("pytest output", 100),
            "exit": 0,
        }
    )

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        args={"command": "pytest -q"},
        noisegate_max_chars=120,
    )

    payload = parse_hook_result(transformed)
    assert payload["name"] == "pytest shard"
    assert "[noisegate: omitted" in payload["stdout"]


def test_bare_top_level_name_does_not_supply_tool_identity() -> None:
    raw = json.dumps(
        {
            "name": "terminal",
            "stdout": numbered("exact payload", 100),
            "exit": 0,
        }
    )

    assert transform_tool_result(raw, noisegate_max_chars=120) is None


def test_transform_tool_result_preserves_failure_lines_in_stderr() -> None:
    raw = json.dumps(
        {
            "command": "npm test",
            "stdout": numbered("ok", 60),
            "stderr": "\n".join(
                [
                    *[f"trace {index}" for index in range(40)],
                    "npm ERR! code ELIFECYCLE",
                    "Error: build failed",
                    *[f"more trace {index}" for index in range(40)],
                ]
            ),
            "exit_code": 1,
        }
    )

    transformed = transform_tool_result(
        raw,
        tool_name="process",
        noisegate_max_chars=500,
        noisegate_head_lines=2,
        noisegate_tail_lines=2,
    )

    payload = parse_hook_result(transformed)
    stderr = payload["stderr"]
    assert isinstance(stderr, str)
    assert "npm ERR! code ELIFECYCLE" in stderr
    assert "Error: build failed" in stderr
    assert payload["exit_code"] == 1


def test_transform_tool_result_alignment_exhaustion_aborts_every_field_order(
    monkeypatch,
) -> None:
    simple = numbered("simple", 100)
    expensive = alignment_exhaustion_text()
    created_budgets: list[engine._SourceAlignmentWorkBudget] = []

    def new_budget() -> engine._SourceAlignmentWorkBudget:
        budget = engine._SourceAlignmentWorkBudget(500)
        created_budgets.append(budget)
        return budget

    monkeypatch.setattr(engine, "_new_source_alignment_work_budget", new_budget)

    for stdout, stderr in ((simple, expensive), (expensive, simple)):
        created_budgets.clear()
        raw = json.dumps(
            {
                "command": "pytest -q",
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": 1,
            }
        )

        transformed = transform_tool_result(
            raw,
            tool_name="terminal",
            noisegate_max_chars=500,
            noisegate_max_lines=22,
            noisegate_head_lines=0,
            noisegate_tail_lines=0,
            noisegate_important_context_lines=0,
        )

        assert transformed is None
        assert len(created_budgets) == 1
        assert created_budgets[0].exhausted is True
        assert engine._SOURCE_ALIGNMENT_WORK_BUDGET.get() is None


def test_transform_tool_result_non_exhausted_mixed_fields_share_one_budget(
    monkeypatch,
) -> None:
    created_budgets: list[engine._SourceAlignmentWorkBudget] = []

    def new_budget() -> engine._SourceAlignmentWorkBudget:
        budget = engine._SourceAlignmentWorkBudget(engine._SOURCE_ALIGNMENT_WORK_LIMIT)
        created_budgets.append(budget)
        return budget

    monkeypatch.setattr(engine, "_new_source_alignment_work_budget", new_budget)
    raw = json.dumps(
        {
            "command": "make noisy",
            "stdout": numbered("stdout", 100),
            "stderr": numbered("stderr", 100),
        }
    )

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        noisegate_max_chars=200,
        noisegate_max_lines=20,
    )

    payload = parse_hook_result(transformed)
    assert set(payload["noisegate"]["fields"]) == {"stdout", "stderr"}
    assert "[noisegate: omitted" in payload["stdout"]
    assert "[noisegate: omitted" in payload["stderr"]
    assert len(created_budgets) == 1
    assert created_budgets[0].exhausted is False
    assert engine._SOURCE_ALIGNMENT_WORK_BUDGET.get() is None


def test_transform_tool_result_exhaustion_does_not_leak_to_next_call(
    monkeypatch,
) -> None:
    limits = iter((500, engine._SOURCE_ALIGNMENT_WORK_LIMIT))
    created_budgets: list[engine._SourceAlignmentWorkBudget] = []

    def new_budget() -> engine._SourceAlignmentWorkBudget:
        budget = engine._SourceAlignmentWorkBudget(next(limits))
        created_budgets.append(budget)
        return budget

    monkeypatch.setattr(engine, "_new_source_alignment_work_budget", new_budget)
    failed_raw = json.dumps(
        {
            "command": "pytest -q",
            "stdout": numbered("simple", 100),
            "stderr": alignment_exhaustion_text(),
            "exit_code": 1,
        }
    )
    independent_raw = terminal_result(numbered("independent", 100), command="make noisy")

    failed = transform_tool_result(
        failed_raw,
        tool_name="terminal",
        noisegate_max_chars=500,
        noisegate_max_lines=22,
        noisegate_head_lines=0,
        noisegate_tail_lines=0,
        noisegate_important_context_lines=0,
    )
    independent = transform_tool_result(
        independent_raw,
        tool_name="terminal",
        noisegate_max_chars=200,
        noisegate_max_lines=20,
    )

    assert failed is None
    assert independent is not None
    assert len(created_budgets) == 2
    assert created_budgets[0].exhausted is True
    assert created_budgets[1].exhausted is False
    assert engine._SOURCE_ALIGNMENT_WORK_BUDGET.get() is None


def test_execute_code_is_not_touched_by_default() -> None:
    raw = json.dumps({"output": numbered("computed result", 120), "exit_code": 0})

    assert transform_tool_result(raw, tool_name="execute_code", noisegate_max_chars=100) is None


def test_read_file_and_write_file_are_not_touched() -> None:
    raw = terminal_result(numbered("file content", 100), command="cat important.txt")

    assert transform_tool_result(raw, tool_name="read_file", noisegate_max_chars=100) is None
    assert transform_tool_result(raw, tool_name="write_file", noisegate_max_chars=100) is None


def test_write_file_short_diagnostics_and_direct_string_results_stay_exact() -> None:
    raw = json.dumps({"bytes_written": 18, "lint": "ok"})
    direct = json.dumps(numbered("direct write result", 100))

    assert transform_tool_result(raw, tool_name="write_file", noisegate_max_chars=120) is None
    for tool_name in ("write_file", "patch", "apply_patch", "edit_file", "replace_in_file"):
        assert (
            transform_tool_result(direct, tool_name=tool_name, noisegate_max_chars=120)
            is None
        )


def test_write_diagnostic_field_allowlist_is_explicit_and_complete() -> None:
    diagnostic = numbered("src/file.py:10:2: error E100 useful diagnostic", 120)

    for field in (
        "lsp_diagnostics",
        "diagnostics",
        "lint",
        "lint_output",
        "lint_errors",
        "typecheck",
        "typecheck_output",
        "pyright",
        "mypy",
        "tsc",
        "eslint",
        "errors",
        "warnings",
    ):
        transformed = transform_tool_result(
            json.dumps({field: diagnostic}),
            tool_name="write_file",
            noisegate_max_chars=240,
            noisegate_max_lines=7,
        )

        payload = parse_hook_result(transformed)
        assert payload[field] != diagnostic, field


def test_write_file_compacts_only_lsp_diagnostics_and_preserves_content() -> None:
    content = "def generated():\n    return 'exact ä\nsource'\n"
    target = (
        '/workspace/src/service_000.py:42:17 - error: Argument of type "str" cannot be '
        'assigned to parameter "count" of type "int" (reportArgumentType)'
    )
    diagnostics = "\n".join(
        [
            target,
            *(
                f"/workspace/src/service_{index:03d}.py:{42 + index}:17 - error: "
                'Argument of type "str" cannot be assigned to parameter "count" '
                'of type "int" (reportArgumentType)'
                for index in range(1, 120)
            ),
        ]
    )
    raw_payload = {
        "content": content,
        "lsp_diagnostics": diagnostics,
        "bytes_written": len(content.encode()),
        "path": "src/generated.py",
        "request_id": "write-123",
    }

    transformed = transform_tool_result(
        json.dumps(raw_payload, ensure_ascii=False),
        tool_name="write_file",
        noisegate_max_chars=300,
        noisegate_max_lines=8,
        noisegate_head_lines=0,
        noisegate_tail_lines=0,
        noisegate_important_context_lines=2,
    )

    payload = parse_hook_result(transformed)
    assert payload["content"] == content
    assert payload["bytes_written"] == raw_payload["bytes_written"]
    assert payload["path"] == "src/generated.py"
    assert payload["request_id"] == "write-123"
    assert payload["lsp_diagnostics"] != diagnostics
    assert "/workspace/src/service_000.py:42:17" in payload["lsp_diagnostics"]
    assert "error" in payload["lsp_diagnostics"]
    assert "reportArgumentType" in payload["lsp_diagnostics"]
    assert "cannot be assigned" in payload["lsp_diagnostics"]
    assert "[noisegate: omitted" in payload["lsp_diagnostics"]
    assert payload["noisegate"]["fields"]["lsp_diagnostics"]["tool_name"] == "write_file"


def test_write_diagnostics_keep_realistic_tool_anchors() -> None:
    cases = {
        "pyright": (
            "src/service.py:42:17 - error: Argument of type \"str\" cannot be assigned "
            'to parameter "count" of type "int" (reportArgumentType)',
            ("src/service.py:42:17", "error", "reportArgumentType"),
        ),
        "mypy": (
            "src/service.py:42:17: error: Incompatible types in assignment "
            '(expression has type "str", variable has type "int")  [assignment]',
            ("src/service.py:42:17", "error", "[assignment]"),
        ),
        "tsc": (
            "src/service.ts(42,17): error TS2322: Type 'string' is not assignable "
            "to type 'number'.",
            ("src/service.ts(42,17)", "error TS2322", "not assignable"),
        ),
        "eslint": (
            "  42:17  error  Unexpected any. Specify a different type  "
            "@typescript-eslint/no-explicit-any",
            ("42:17", "error", "@typescript-eslint/no-explicit-any"),
        ),
        "lint_output": (
            "src/service.py:42:17: F401 [*] `os` imported but unused",
            ("src/service.py:42:17", "F401", "imported but unused"),
        ),
        "diagnostics": (
            "src/service.py:42:17 [warning] W0611: Unused import os",
            ("src/service.py:42:17", "warning", "W0611", "Unused import"),
        ),
    }

    for field, (anchor, expected_parts) in cases.items():
        diagnostic = "\n".join(
            [
                *(f"analyzing project unit {index:03d}" for index in range(60)),
                anchor,
                *(f"analyzing project unit {index:03d}" for index in range(60, 120)),
            ]
        )
        transformed = transform_tool_result(
            json.dumps({field: diagnostic, "content": "exact source\n"}),
            tool_name="edit_file",
            noisegate_max_chars=260,
            noisegate_max_lines=7,
            noisegate_head_lines=0,
            noisegate_tail_lines=0,
            noisegate_important_context_lines=1,
        )

        payload = parse_hook_result(transformed)
        assert payload["content"] == "exact source\n", field
        assert payload[field] != diagnostic, field
        for part in expected_parts:
            assert part in payload[field], (field, part, payload[field])


def test_write_diagnostics_keep_nearby_codeframe_and_caret() -> None:
    diagnostic = "\n".join(
        [
            *(f"checking module {index:03d}" for index in range(60)),
            "src/service.py:42:17: error: Undefined name `value` [name-defined]",
            "   42 |     result = value + 1",
            "      |              ^^^^^",
            *(f"checking module {index:03d}" for index in range(60, 120)),
        ]
    )

    transformed = transform_tool_result(
        json.dumps({"mypy": diagnostic}),
        tool_name="replace_in_file",
        noisegate_max_chars=260,
        noisegate_max_lines=8,
        noisegate_head_lines=0,
        noisegate_tail_lines=0,
        noisegate_important_context_lines=2,
    )

    payload = parse_hook_result(transformed)
    assert "src/service.py:42:17" in payload["mypy"]
    assert "42 |     result = value + 1" in payload["mypy"]
    assert "|              ^^^^^" in payload["mypy"]


def test_write_diagnostics_prioritize_real_tool_formats_over_generic_errors() -> None:
    cases = {
        "ruff": (
            "lint_output",
            [
                "F401 [*] `os` imported but unused",
                " --> src/app.py:1:8",
                "  |",
                "1 | import os",
                "  |        ^^",
                "  |",
                "help: Remove unused import: `os`",
            ],
            ("F401", "src/app.py:1:8", "1 | import os", "|        ^^"),
        ),
        "pyright": (
            "pyright",
            [
                "src/service.py:42:17 - error: Argument of type \"str\" cannot be "
                'assigned to parameter "count" of type "int" (reportArgumentType)'
            ],
            ("src/service.py:42:17", "error", "reportArgumentType"),
        ),
        "mypy": (
            "mypy",
            [
                "src/service.py:42:17: error: Incompatible types in assignment "
                '(expression has type "str", variable has type "int")  [assignment]'
            ],
            ("src/service.py:42:17", "error", "[assignment]"),
        ),
        "tsc": (
            "tsc",
            [
                "src/service.ts(42,17): error TS2322: Type 'string' is not assignable "
                "to type 'number'."
            ],
            ("src/service.ts(42,17)", "error TS2322", "not assignable"),
        ),
        "eslint": (
            "eslint",
            [
                "/workspace/src/service.ts",
                "  42:17  error  Unexpected any. Specify a different type  "
                "@typescript-eslint/no-explicit-any",
            ],
            ("/workspace/src/service.ts", "42:17", "@typescript-eslint/no-explicit-any"),
        ),
    }

    for case, (field, anchor_lines, expected_parts) in cases.items():
        diagnostic = "\n".join(
            [
                *(f"ERROR: wrapper noise {index:03d}" for index in range(40)),
                *(f"checking nearby module {index:03d}" for index in range(6)),
                *anchor_lines,
                *(f"checking remaining module {index:03d}" for index in range(40)),
            ]
        )

        transformed = transform_tool_result(
            json.dumps({field: diagnostic, "content": "exact source\n"}),
            tool_name="edit_file",
            noisegate_max_chars=420,
            noisegate_max_lines=10,
            noisegate_head_lines=0,
            noisegate_tail_lines=0,
            noisegate_important_context_lines=3,
            noisegate_max_important_lines=2,
        )

        payload = parse_hook_result(transformed)
        assert payload["content"] == "exact source\n", case
        for part in expected_parts:
            assert part in payload[field], (case, part, payload[field])
        assert "ERROR: wrapper noise 000" not in payload[field], case


def test_write_diagnostics_keep_scoped_priority_in_tight_character_fallback() -> None:
    location = "src/service.py:42:17: error: Undefined name `value` [name-defined]"
    frame = "   42 |     result = value + 1"
    caret = "      |              ^^^^^"
    diagnostic = "\n".join(
        [
            "ERROR: wrapper failed" + (" x" * 45),
            *(f"checking module {index:02d}" for index in range(25)),
            location,
            frame,
            caret,
        ]
    )
    complete_excerpt = "\n".join(
        [
            "[noisegate: omitted 26 lines]",
            location,
            frame,
            caret,
        ]
    )
    assert len(complete_excerpt) <= 200

    transformed = transform_tool_result(
        json.dumps({"mypy": diagnostic, "content": "exact source\n"}),
        tool_name="edit_file",
        noisegate_max_chars=200,
        noisegate_max_lines=40,
        noisegate_important_context_lines=2,
    )

    payload = parse_hook_result(transformed)
    assert payload["content"] == "exact source\n"
    assert location in payload["mypy"]
    assert frame in payload["mypy"]
    assert caret in payload["mypy"]
    assert "ERROR: wrapper failed" not in payload["mypy"]


def test_write_diagnostics_keep_scoped_priority_after_upstream_omission_remap() -> None:
    location = "src/a.py:42:17: error: Undefined name `value` [name-defined]"
    frame = "42 | result = value + 1"
    caret = "   |          ^^^^^"
    refs = ("externalized_ref=lcm_a", "externalized_ref=lcm_b")
    diagnostic = "\n".join(
        [
            *(f"ERROR ValueError: generic wrapper noise {index:02d}" for index in range(8)),
            "[noisegate: omitted 20 lines]",
            *refs,
            *(
                f"ERROR ValueError: generic wrapper noise {index:02d}"
                for index in range(8, 16)
            ),
            location,
            frame,
            caret,
            *(f"checking trailing module {index:02d}" for index in range(8)),
        ]
    )
    complete_excerpt = "\n".join(
        [
            "[noisegate: omitted 28 lines]",
            *refs,
            "[noisegate: omitted 8 lines]",
            location,
            frame,
            caret,
            "[noisegate: omitted 8 lines]",
        ]
    )
    assert len(complete_excerpt) <= 250

    transformed = transform_tool_result(
        json.dumps({"mypy": diagnostic, "source": "exact source"}),
        tool_name="write_file",
        noisegate_max_chars=250,
        noisegate_max_lines=10,
        noisegate_head_lines=0,
        noisegate_tail_lines=0,
        noisegate_important_context_lines=2,
        noisegate_max_important_lines=2,
    )

    payload = parse_hook_result(transformed)
    assert payload["source"] == "exact source"
    assert location in payload["mypy"]
    assert frame in payload["mypy"]
    assert caret in payload["mypy"]
    for ref in refs:
        assert ref in payload["mypy"]


def test_write_diagnostic_patterns_are_line_local_on_many_blank_lines() -> None:
    for pattern in engine.DIAGNOSTIC_LOCATION_PATTERNS:
        assert pattern.flags & re.MULTILINE
        assert pattern.pattern.startswith("^")
        assert r"\s*" not in pattern.pattern
        assert r"\s+" not in pattern.pattern

    location = "src/service.py:42:17: error: Undefined name `value` [name-defined]"

    def transform_blank_lines(count: int) -> float:
        diagnostic = (" \n" * count) + location + "\n" + numbered("checking module", 80)
        started = time.perf_counter()
        transformed = transform_tool_result(
            json.dumps({"diagnostics": diagnostic, "source": "exact source"}),
            tool_name="write_file",
            noisegate_max_chars=220,
            noisegate_max_lines=7,
            noisegate_head_lines=0,
            noisegate_tail_lines=0,
            noisegate_important_context_lines=1,
        )
        elapsed = time.perf_counter() - started
        payload = parse_hook_result(transformed)
        assert payload["source"] == "exact source"
        assert location in payload["diagnostics"]
        return elapsed

    small_elapsed = transform_blank_lines(1_000)
    large_elapsed = transform_blank_lines(8_000)
    assert large_elapsed < 1.0
    assert large_elapsed < (small_elapsed * 12) + 0.05


def test_write_diagnostics_override_git_boundary_reducers_when_passthrough_is_off() -> None:
    location = (
        "src/service.py:42:17: error: Incompatible types in assignment [assignment]"
    )
    diagnostic = "\n".join(
        [
            *(f"ERROR: boundary wrapper {index:03d}" for index in range(60)),
            location,
            *(f"ERROR: trailing wrapper {index:03d}" for index in range(60)),
        ]
    )

    cases = (
        ("git log --oneline", True),
        ("git diff -- src/service.py", False),
    )
    for command, preserve_diffs in cases:
        transformed = transform_tool_result(
            json.dumps(
                {
                    "diagnostics": diagnostic,
                    "command": command,
                    "source": "exact source\n",
                }
            ),
            tool_name="write_file",
            noisegate_max_chars=240,
            noisegate_max_lines=7,
            noisegate_head_lines=1,
            noisegate_tail_lines=1,
            noisegate_important_context_lines=1,
            noisegate_max_important_lines=2,
            noisegate_preserve_diffs=preserve_diffs,
        )

        payload = parse_hook_result(transformed)
        assert payload["command"] == command
        assert payload["source"] == "exact source\n"
        assert location in payload["diagnostics"], command
        assert "[assignment]" in payload["diagnostics"], command
        assert "ERROR: boundary wrapper 000" not in payload["diagnostics"], command


def test_write_diagnostics_use_scoped_reduction_in_auto_and_head_tail_modes() -> None:
    diagnostic = "\n".join(
        [
            *(f"checking module {index:03d}" for index in range(60)),
            "src/service.py:42:17: error: Undefined name `value` [name-defined]",
            "   42 |     result = value + 1",
            "      |              ^^^^^",
            *(f"checking module {index:03d}" for index in range(60, 120)),
        ]
    )

    for mode in ("auto", "head_tail"):
        transformed = transform_tool_result(
            json.dumps({"diagnostics": diagnostic}),
            tool_name="write_file",
            noisegate_mode=mode,
            noisegate_max_chars=280,
            noisegate_max_lines=8,
            noisegate_head_lines=1,
            noisegate_tail_lines=1,
            noisegate_important_context_lines=2,
        )

        payload = parse_hook_result(transformed)
        assert "src/service.py:42:17" in payload["diagnostics"], mode
        assert "42 |     result = value + 1" in payload["diagnostics"], mode
        assert "|              ^^^^^" in payload["diagnostics"], mode


def test_write_diagnostics_off_mode_is_noop() -> None:
    raw = json.dumps(
        {"diagnostics": numbered("src/file.py:10:2: error E100 useful diagnostic", 120)}
    )

    assert (
        transform_tool_result(
            raw,
            tool_name="write_file",
            noisegate_mode="off",
            noisegate_max_chars=240,
        )
        is None
    )


def test_patch_like_results_preserve_all_source_bearing_fields() -> None:
    source_values = {
        "content": "new content\nERROR is source here\n",
        "source": "old source\nwarning is source here\n",
        "diff": "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n",
        "patch": "*** Begin Patch\n*** Update File: file.py\n-old\n+new\n*** End Patch",
    }
    diagnostic = "\n".join(
        f"src/file.py:{index}:3: error: E{index:04d} diagnostic message {index}"
        for index in range(1, 121)
    )

    for tool_name in ("patch", "apply_patch", "edit_file", "replace_in_file"):
        transformed = transform_tool_result(
            json.dumps({**source_values, "errors": diagnostic, "applied": True}),
            tool_name=tool_name,
            noisegate_max_chars=300,
            noisegate_max_lines=8,
            noisegate_head_lines=1,
            noisegate_tail_lines=1,
        )

        payload = parse_hook_result(transformed)
        for field, value in source_values.items():
            assert payload[field] == value, (tool_name, field)
        assert payload["applied"] is True
        assert payload["errors"] != diagnostic
        assert "error" in payload["errors"]


def test_write_diagnostic_metadata_collisions_and_unknown_fields_survive() -> None:
    diagnostic = numbered("src/file.py:10:2: error E100 useful diagnostic", 120)
    raw_payload = {
        "diagnostics": diagnostic,
        "unknown_diagnostic_text": numbered("must remain exact", 120),
        "noisegate": {"owned": "by tool"},
        "_noisegate": {"also": "owned"},
        "__noisegate": {"still": "owned"},
    }

    transformed = transform_tool_result(
        json.dumps(raw_payload),
        tool_name="apply_patch",
        noisegate_max_chars=240,
        noisegate_max_lines=7,
    )

    payload = parse_hook_result(transformed)
    assert payload["diagnostics"] != diagnostic
    assert payload["unknown_diagnostic_text"] == raw_payload["unknown_diagnostic_text"]
    assert payload["noisegate"] == {"owned": "by tool"}
    assert payload["_noisegate"] == {"also": "owned"}
    assert payload["__noisegate"] == {"still": "owned"}
    metadata_keys = set(payload) - set(raw_payload)
    assert len(metadata_keys) == 1
    assert payload[metadata_keys.pop()]["compacted"] is True


def test_write_diagnostics_never_plan_or_store_artifacts_in_direct_or_preview_wrapper(
    monkeypatch,
    tmp_path: Path,
) -> None:
    diagnostic = numbered("src/file.py:10:2: error E100 useful diagnostic", 120)
    artifact_dir = tmp_path / "artifacts"

    def unexpected_artifact_call(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("write diagnostics must not plan or store artifacts")

    monkeypatch.setattr(plugin, "_plan_artifact", unexpected_artifact_call)
    monkeypatch.setattr(plugin, "_store_artifact", unexpected_artifact_call)
    monkeypatch.setattr(plugin, "_artifact_preview_plan", unexpected_artifact_call)
    monkeypatch.setattr(engine, "_plan_artifact", unexpected_artifact_call)
    monkeypatch.setattr(engine, "_store_artifact", unexpected_artifact_call)
    raw = json.dumps({"diagnostics": diagnostic, "content": "exact source"})
    transformed = transform_tool_result(
        raw,
        tool_name="write_file",
        noisegate_max_chars=240,
        noisegate_max_lines=7,
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(artifact_dir),
    )

    payload = parse_hook_result(transformed)
    assert "artifact" not in payload["noisegate"]["fields"]["diagnostics"]

    preview_plans: list[plugin._ArtifactPreviewPlan] = []
    preview = plugin._preview_tool_result(
        raw,
        tool_name="tool_call",
        args={"name": "apply_patch", "arguments": {"path": "src/file.py"}},
        artifact_plans_out=preview_plans,
        noisegate_max_chars=240,
        noisegate_max_lines=7,
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(artifact_dir),
    )

    preview_payload = parse_hook_result(preview)
    assert "artifact" not in preview_payload["noisegate"]["fields"]["diagnostics"]
    assert preview_plans == []
    assert not artifact_dir.exists()


def test_write_diagnostic_alignment_exhaustion_aborts_complete_envelope(
    monkeypatch,
) -> None:
    simple = numbered("src/simple.py:10:2: error E100 useful diagnostic", 100)
    expensive = alignment_exhaustion_text()
    created_budgets: list[engine._SourceAlignmentWorkBudget] = []

    def new_budget() -> engine._SourceAlignmentWorkBudget:
        budget = engine._SourceAlignmentWorkBudget(500)
        created_budgets.append(budget)
        return budget

    monkeypatch.setattr(engine, "_new_source_alignment_work_budget", new_budget)

    for diagnostics, lint in ((simple, expensive), (expensive, simple)):
        created_budgets.clear()
        raw = json.dumps(
            {
                "content": "exact source",
                "diagnostics": diagnostics,
                "lint": lint,
            }
        )

        transformed = transform_tool_result(
            raw,
            tool_name="write_file",
            noisegate_max_chars=500,
            noisegate_max_lines=22,
            noisegate_head_lines=0,
            noisegate_tail_lines=0,
            noisegate_important_context_lines=0,
            noisegate_artifacts=True,
        )

        assert transformed is None
        assert len(created_budgets) == 1
        assert created_budgets[0].exhausted is True
        assert engine._SOURCE_ALIGNMENT_WORK_BUDGET.get() is None


def test_nested_json_string_diagnostic_aborts_complete_envelope() -> None:
    nested = json.dumps(
        {
            "errors": numbered("inner.py:7:3: error E700 nested diagnostic", 80),
            "content": "exact nested source ä",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    warnings = numbered("outer.py:10:2: warning W100 useful diagnostic", 120)
    raw = json.dumps(
        {
            "diagnostics": nested,
            "warnings": warnings,
            "content": "exact outer source ä",
        },
        ensure_ascii=False,
        indent=2,
    )

    assert (
        transform_tool_result(
            raw,
            tool_name="edit_file",
            noisegate_max_chars=240,
            noisegate_max_lines=7,
        )
        is None
    )


def test_unsupported_diagnostic_value_aborts_complete_envelope() -> None:
    warnings = numbered("outer.py:10:2: warning W100 useful diagnostic", 120)

    for unsupported in (None, True, 7, ["error"], {"message": "error"}):
        raw = json.dumps(
            {
                "diagnostics": unsupported,
                "warnings": warnings,
                "content": "exact outer source",
            }
        )

        assert (
            transform_tool_result(
                raw,
                tool_name="edit_file",
                noisegate_max_chars=240,
                noisegate_max_lines=7,
            )
            is None
        )


def test_invalid_field_reduction_aborts_complete_envelope(monkeypatch) -> None:
    diagnostics = numbered("first.py:10:2: error E100 useful diagnostic", 120)
    warnings = numbered("second.py:10:2: warning W100 useful diagnostic", 120)
    validate = plugin._valid_transformed_text

    def reject_first_field(
        *,
        original: str,
        transformed: str,
        metadata: dict[str, Any],
        options: engine.NoisegateOptions,
    ) -> bool:
        if original == diagnostics:
            return False
        return validate(
            original=original,
            transformed=transformed,
            metadata=metadata,
            options=options,
        )

    monkeypatch.setattr(plugin, "_valid_transformed_text", reject_first_field)
    raw = json.dumps(
        {
            "diagnostics": diagnostics,
            "warnings": warnings,
            "content": "exact source",
        }
    )

    assert (
        transform_tool_result(
            raw,
            tool_name="write_file",
            noisegate_max_chars=240,
            noisegate_max_lines=7,
        )
        is None
    )


def test_same_object_no_gain_write_diagnostic_aborts_all_siblings() -> None:
    warnings = numbered("src/file.py:10:2: warning W100 useful diagnostic", 120)
    raw = json.dumps(
        {
            "diagnostics": "ok",
            "warnings": warnings,
            "content": "exact source",
        }
    )

    assert (
        transform_tool_result(
            raw,
            tool_name="write_file",
            noisegate_max_chars=240,
            noisegate_max_lines=7,
        )
        is None
    )

    diagnostics = numbered("src/file.py:20:3: error E200 useful diagnostic", 120)
    transformed = transform_tool_result(
        json.dumps({"diagnostics": diagnostics, "warnings": warnings}),
        tool_name="write_file",
        noisegate_max_chars=240,
        noisegate_max_lines=7,
    )
    payload = parse_hook_result(transformed)
    assert payload["diagnostics"] != diagnostics
    assert payload["warnings"] != warnings
    assert set(payload["noisegate"]["fields"]) == {"diagnostics", "warnings"}


def test_write_diagnostics_preserve_nonzero_exit_hints() -> None:
    diagnostics = numbered("src/file.py:20:3: error E200 useful diagnostic", 120)
    cases = (
        ({"exit_code": 7}, None, 7),
        ({"status": "failed"}, None, 1),
        ({}, 9, 9),
    )

    for payload_fields, exit_override, expected_exit_code in cases:
        transformed = transform_tool_result(
            json.dumps({"diagnostics": diagnostics, **payload_fields}),
            tool_name="write_file",
            noisegate_max_chars=240,
            noisegate_max_lines=7,
            noisegate_exit_code=exit_override,
        )

        payload = parse_hook_result(transformed)
        assert f"[noisegate: exit_code={expected_exit_code}]" in payload["diagnostics"]
        assert (
            payload["noisegate"]["fields"]["diagnostics"]["exit_code"]
            == expected_exit_code
        )


def test_write_diagnostic_invalid_and_duplicate_json_fail_open() -> None:
    duplicate = (
        '{"diagnostics":"'
        + numbered("first diagnostic", 80).replace("\n", "\\n")
        + '","diagnostics":"'
        + numbered("second diagnostic", 80).replace("\n", "\\n")
        + '"}'
    )

    assert transform_tool_result("{malformed", tool_name="write_file") is None
    assert transform_tool_result(duplicate, tool_name="apply_patch") is None

    noisy_sibling = numbered("outer.py:10:2: warning W100 useful diagnostic", 120)
    for nested in (
        '{"errors":"unterminated}',
        '{"errors":"first","errors":"second"}',
    ):
        assert (
            transform_tool_result(
                json.dumps({"diagnostics": nested, "warnings": noisy_sibling}),
                tool_name="edit_file",
                noisegate_max_chars=240,
                noisegate_max_lines=7,
            )
            is None
        )


def test_write_diagnostic_nonfinite_json_aborts_complete_envelope() -> None:
    diagnostic = numbered("src/file.py:10:2: error E100 useful diagnostic", 120)

    for constant in ("1e400", "-1e400", "NaN"):
        raw = f'{{"diagnostics":{json.dumps(diagnostic)},"unknown":{constant}}}'

        assert (
            transform_tool_result(
                raw,
                tool_name="write_file",
                noisegate_max_chars=240,
                noisegate_max_lines=7,
            )
            is None
        ), constant


def test_write_diagnostic_lone_surrogates_fail_open_before_utf8_encoding() -> None:
    diagnostic = numbered("src/file.py:10:2: error E100 useful diagnostic", 120)

    for escaped_surrogate in (r"\ud800", r"\udfff"):
        raw = (
            '{"diagnostics":'
            f'{json.dumps(diagnostic)},"source":"{escaped_surrogate}"}}'
        )

        assert (
            transform_tool_result(
                raw,
                tool_name="write_file",
                noisegate_max_chars=240,
                noisegate_max_lines=7,
            )
            is None
        ), escaped_surrogate


def test_write_diagnostic_valid_non_bmp_unicode_stays_exact_and_utf8_encodable() -> None:
    diagnostic = numbered("src/file.py:10:2: error E100 useful diagnostic", 120)
    source = "exact source with non-BMP Unicode: 🧪\n"
    raw = json.dumps(
        {"diagnostics": diagnostic, "source": source},
        ensure_ascii=False,
    )

    transformed = transform_tool_result(
        raw,
        tool_name="write_file",
        noisegate_max_chars=240,
        noisegate_max_lines=7,
    )

    payload = parse_hook_result(transformed)
    assert payload["source"] == source
    assert isinstance(transformed, str)
    transformed.encode("utf-8")


def test_tool_call_wrapper_requires_unambiguous_write_identity() -> None:
    diagnostic = numbered("src/file.py:10:2: error E100 useful diagnostic", 120)
    raw = json.dumps({"diagnostics": diagnostic, "content": "exact source"})

    transformed = transform_tool_result(
        raw,
        tool_name="tool_call",
        args={"name": "write_file", "arguments": {"path": "src/file.py"}},
        noisegate_max_chars=240,
        noisegate_max_lines=7,
    )
    payload = parse_hook_result(transformed)
    assert payload["content"] == "exact source"
    assert payload["diagnostics"] != diagnostic

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={
                "name": "write_file",
                "tool": "apply_patch",
                "arguments": {"path": "src/file.py"},
            },
            noisegate_max_chars=240,
        )
        is None
    )


def test_named_write_tool_rejects_conflicting_embedded_identity() -> None:
    raw = json.dumps(
        {
            "tool_name": "apply_patch",
            "diagnostics": numbered("src/file.py:10:2: error E100 useful diagnostic", 120),
        }
    )

    assert (
        transform_tool_result(raw, tool_name="write_file", noisegate_max_chars=240)
        is None
    )


def test_skill_view_is_not_touched() -> None:
    raw = json.dumps({"content": numbered("skill instruction", 120), "path": "SKILL.md"})

    assert transform_tool_result(raw, tool_name="skill_view", noisegate_max_chars=100) is None


def test_context_retrieval_tools_are_not_touched() -> None:
    raw = json.dumps({"content": numbered("retrieved context", 120)})

    for tool_name in (
        "session_search",
        "hindsight_recall",
        "hindsight_reflect",
        "lcm_expand",
        "lcm_expand_query",
        "mcp__mindlyos__get_note_page",
        "mcp__remarkable__remarkable_read",
        "web_extract",
        "web_search",
        "search_files",
        "skills_list",
        "todo",
        "ha_get_state",
        "browser_snapshot",
        "vision_analyze",
        "image_generate",
        "x_search",
    ):
        assert transform_tool_result(raw, tool_name=tool_name, noisegate_max_chars=100) is None


def test_named_protected_tool_returns_before_result_json_parse(monkeypatch) -> None:
    def fail_parse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("protected result should not be parsed")

    monkeypatch.setattr(plugin.json, "loads", fail_parse)

    assert transform_tool_result("not-json", tool_name="mcp_github_list_issues") is None


def test_mcp_outputs_stay_protected_for_conservative_policy(tmp_path: Path) -> None:
    cases = {
        "mcp_github_list_issues": json.dumps(
            {
                "issues": [
                    {
                        "number": index,
                        "title": f"Issue {index}",
                        "body": numbered(f"issue {index} body", 8),
                    }
                    for index in range(60)
                ]
            }
        ),
        "mcp_github_get_file": json.dumps(
            {"path": "src/app.py", "content": numbered("exact source", 180)}
        ),
        "mcp_github_get_source": json.dumps(
            {"source": "github", "content": numbered("exact source", 180)}
        ),
        "mcp_sentry_stacktrace": json.dumps(
            {
                "stacktrace": numbered("frame", 120),
                "logs": "Authorization: "
                + "Bearer "
                + "not-a-real-sentry-token\n"
                + numbered("sentry log", 120),
            }
        ),
        "mcp_playwright_snapshot": json.dumps({"snapshot": numbered("role=button name=Save", 180)}),
        "mcp_database_query": json.dumps(
            {
                "columns": ["id", "email", "created_at"],
                "rows": [
                    [index, f"person{index}@example.test", "2026-01-01"]
                    for index in range(160)
                ],
            }
        ),
        "mcp_resource_read": json.dumps(
            {"uri": "file:///repo/README.md", "content": numbered("resource line", 160)}
        ),
        "mcp_prompt_schema_output": json.dumps(
            {
                "prompts": [
                    {"name": f"prompt_{index}", "arguments": [{"name": "query"}]}
                    for index in range(80)
                ],
                "tools": [
                    {
                        "name": "lookup",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"q": {"type": "string"}},
                        },
                    }
                ],
            }
        ),
        "mcp_dynamic_tool_discovery": json.dumps(
            {
                "tools": [
                    {"name": f"dynamic_tool_{index}", "description": numbered("schema", 3)}
                    for index in range(90)
                ]
            }
        ),
        "mcp_error_log_spam": json.dumps(
            {
                "error": "rate limited",
                "logs": numbered("retrying MCP request", 240),
                "headers": {"x-api-key": "secret"},
            }
        ),
    }

    for tool_name, raw in cases.items():
        artifact_dir = tmp_path / tool_name
        transformed = transform_tool_result(
            raw,
            tool_name=tool_name,
            noisegate_max_chars=200,
            noisegate_artifacts=True,
            noisegate_artifact_dir=str(artifact_dir),
        )

        assert transformed is None, tool_name
        assert not artifact_dir.exists(), tool_name


def test_tool_search_and_describe_wrappers_stay_exact() -> None:
    raw = json.dumps(
        {
            "tools": [
                {"name": f"mcp_dynamic_{index}", "inputSchema": {"type": "object"}}
                for index in range(120)
            ],
            "content": numbered("dynamic discovery metadata", 120),
        }
    )

    for tool_name in ("tool_search", "tool_describe"):
        assert transform_tool_result(raw, tool_name=tool_name, noisegate_max_chars=120) is None


def test_tool_call_wrapper_uses_real_mcp_tool_semantics() -> None:
    raw = json.dumps({"content": numbered("issue listing", 180)})

    transformed = transform_tool_result(
        raw,
        tool_name="tool_call",
        args={"name": "mcp_github_list_issues", "arguments": {"repo": "Tosko4/noisegate"}},
        noisegate_max_chars=120,
    )

    assert transformed is None


def test_tool_call_wrapper_uses_real_noisy_tool_semantics() -> None:
    raw = json.dumps({"stdout": numbered("pytest output", 180), "exit": 0})

    transformed = transform_tool_result(
        raw,
        tool_name="tool_call",
        args={"tool": {"name": "terminal"}, "arguments": {"command": "pytest -q"}},
        noisegate_max_chars=160,
        noisegate_head_lines=2,
        noisegate_tail_lines=2,
    )

    payload = parse_hook_result(transformed)
    assert "[noisegate: omitted" in payload["stdout"]
    assert payload["noisegate"]["fields"]["stdout"]["tool_name"] == "terminal"


def test_tool_call_wrapper_recurses_through_json_encoded_arguments() -> None:
    raw = json.dumps({"stdout": numbered("pytest output", 180), "exit": 0})

    transformed = transform_tool_result(
        raw,
        tool_name="tool_call",
        args={
            "name": "tool_call",
            "arguments": json.dumps(
                {"name": "terminal", "arguments": {"command": "pytest -q"}}
            ),
        },
        noisegate_max_chars=160,
        noisegate_head_lines=2,
        noisegate_tail_lines=2,
    )

    payload = parse_hook_result(transformed)
    assert "[noisegate: omitted" in payload["stdout"]
    assert payload["noisegate"]["fields"]["stdout"]["command_class"] == "pytest"
    assert payload["noisegate"]["fields"]["stdout"]["tool_name"] == "terminal"


def test_tool_call_wrapper_without_explicit_identity_stays_exact() -> None:
    raw = json.dumps({"stdout": numbered("exact", 180), "tool_name": "terminal"})

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={"arguments": {"name": "terminal", "command": "pytest -q"}},
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_with_conflicting_identities_stays_exact() -> None:
    raw = json.dumps({"stdout": numbered("exact", 180)})

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={
                "name": "terminal",
                "tool": "mcp_github_get_file",
                "arguments": {"command": "pytest -q"},
            },
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_with_conflicting_argument_containers_stays_exact() -> None:
    raw = json.dumps({"stdout": numbered("source", 180)})

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={
                "name": "terminal",
                "arguments": {"command": "pytest -q"},
                "args": {"command": "cat important.py"},
            },
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_with_conflicting_command_aliases_stays_exact() -> None:
    raw = json.dumps({"stdout": numbered("source", 180)})

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={
                "name": "terminal",
                "arguments": {
                    "command": "pytest -q",
                    "cmd": "cat important.py",
                },
            },
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_with_conflicting_command_ownership_stays_exact() -> None:
    raw = json.dumps({"stdout": numbered("source", 180)})

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={
                "name": "terminal",
                "command": "cat important.py",
                "arguments": {"command": "pytest -q"},
            },
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_rejects_duplicate_result_keys() -> None:
    first = json.dumps(numbered("exact source", 180))
    second = json.dumps(numbered("pytest output", 180))
    raw = f'{{"stdout":{first},"stdout":{second},"exit":0}}'

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={"name": "terminal", "arguments": {"command": "pytest -q"}},
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_preserves_json_encoded_result_fields() -> None:
    nested = json.dumps(
        {"tool_name": "mcp_github_get_file", "content": numbered("source", 180)}
    )
    raw = json.dumps({"result": nested, "logs": numbered("browser noise", 180)})

    transformed = transform_tool_result(
        raw,
        tool_name="tool_call",
        args={"name": "browser_console", "arguments": {}},
        noisegate_max_chars=600,
    )

    assert transformed is not None
    payload = json.loads(transformed)
    assert payload["result"] == nested
    assert json.loads(payload["result"])["tool_name"] == "mcp_github_get_file"
    assert "[noisegate: omitted" in payload["logs"]


def test_tool_call_wrapper_compacts_json_encoded_terminal_result() -> None:
    nested = json.dumps(
        {
            "tool_name": "terminal",
            "name": "pytest shard",
            "stdout": numbered("pytest output", 180),
            "exit": 0,
        }
    )
    raw = json.dumps({"result": nested})

    transformed = transform_tool_result(
        raw,
        tool_name="tool_call",
        args={"name": "terminal", "arguments": {"command": "pytest -q"}},
        noisegate_max_chars=600,
    )

    assert transformed is not None
    payload = json.loads(transformed)
    nested_payload = json.loads(payload["result"])
    assert nested_payload["exit"] == 0
    assert nested_payload["name"] == "pytest shard"
    assert "[noisegate: omitted" in nested_payload["stdout"]


def test_tool_call_wrapper_rejects_nested_terminal_command_conflicts() -> None:
    for owner in ("args", "input"):
        nested = json.dumps(
            {
                owner: {"command": "cat important.py"},
                "stdout": numbered("exact source", 180),
                "exit": 0,
            }
        )
        raw = json.dumps({"result": nested, "stdout": numbered("sibling noise", 180)})

        assert (
            transform_tool_result(
                raw,
                tool_name="tool_call",
                args={"name": "terminal", "arguments": {"command": "pytest -q"}},
                noisegate_max_chars=600,
            )
            is None
        )


def test_tool_call_wrapper_nested_terminal_never_stores_artifacts(monkeypatch) -> None:
    nested = json.dumps({"stdout": numbered("pytest output", 180), "exit": 0})
    raw = json.dumps({"result": nested})

    def unexpected_store(*_args, **_kwargs):
        raise AssertionError("nested terminal JSON must not persist an artifact")

    monkeypatch.setattr(plugin, "_store_artifact", unexpected_store)
    transformed = transform_tool_result(
        raw,
        tool_name="tool_call",
        args={"name": "terminal", "arguments": {"command": "pytest -q"}},
        noisegate_max_chars=600,
        noisegate_artifacts=True,
        noisegate_artifact_enabled=True,
    )

    assert transformed is not None


def test_tool_call_wrapper_preserves_scalar_json_result_fields() -> None:
    for nested in ("null   ", "true\n", "-12.5e3\t"):
        raw = json.dumps({"result": nested, "logs": numbered("browser noise", 180)})

        transformed = transform_tool_result(
            raw,
            tool_name="tool_call",
            args={"name": "browser_console", "arguments": {}},
            noisegate_max_chars=600,
        )

        assert transformed is not None
        payload = json.loads(transformed)
        assert payload["result"] == nested
        assert "[noisegate: omitted" in payload["logs"]


def test_tool_call_wrapper_compacts_log_prefixed_plain_result() -> None:
    for first_line in (
        "2026-07-14 build started",
        "- warning",
        "[2026-07-14 09:04:09] build started",
        "[123] build started",
    ):
        raw = json.dumps(
            {
                "result": first_line + "\n" + numbered("detail", 180),
                "logs": numbered("browser noise", 180),
            }
        )

        transformed = transform_tool_result(
            raw,
            tool_name="tool_call",
            args={"name": "browser_console", "arguments": {}},
            noisegate_max_chars=600,
        )

        assert transformed is not None
        payload = json.loads(transformed)
        assert payload["result"].startswith(first_line)
        assert "[noisegate: omitted" in payload["result"]
        assert "[noisegate: omitted" in payload["logs"]


def test_nested_json_detection_bounds_oversized_text_before_parsing() -> None:
    assert plugin._nested_json_text_requires_exact("plain " * 20_000) is False
    try:
        plugin._nested_json_text_requires_exact(" " * 70_000 + '{"value": 1}')
    except ValueError as exc:
        assert "size limit" in str(exc)
    else:
        raise AssertionError("oversized JSON-like text should fail closed")


def test_tool_call_wrapper_rejects_duplicate_keys_in_nested_json_strings() -> None:
    duplicate = (
        '{"tool_name":"mcp_github_get_file",'
        '"tool_name":"browser_console",'
        f'"content":{json.dumps(numbered("source", 180))}}}'
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
    for nested in (
        duplicate,
        json.dumps(duplicate),
        malformed,
        *nonstandard,
        *direct_invalid,
    ):
        raw = json.dumps({"result": nested, "logs": numbered("browser noise", 180)})
        assert (
            transform_tool_result(
                raw,
                tool_name="tool_call",
                args={"name": "browser_console", "arguments": {}},
                noisegate_max_chars=600,
            )
            is None
        )


def test_tool_call_wrapper_rejects_protected_embedded_result_identity() -> None:
    raw = json.dumps(
        {
            "tool_name": "mcp_github_get_file",
            "stdout": numbered("exact source", 180),
        }
    )

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={"name": "terminal", "arguments": {"command": "pytest -q"}},
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_rejects_intermediate_command_conflict() -> None:
    raw = json.dumps({"stdout": numbered("exact source", 180)})

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={
                "name": "tool_call",
                "command": "cat important.py",
                "arguments": {
                    "name": "terminal",
                    "arguments": {"command": "pytest -q"},
                },
            },
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_propagates_intermediate_exact_command() -> None:
    raw = json.dumps({"stdout": numbered("exact source", 180)})

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={
                "name": "tool_call",
                "command": "cat important.py",
                "arguments": {"name": "terminal"},
            },
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_rejects_result_owned_command_conflict() -> None:
    raw = json.dumps(
        {
            "args": {"command": "cat important.py"},
            "stdout": numbered("exact source", 180),
            "exit": 0,
        }
    )

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={"name": "terminal", "arguments": {"command": "pytest -q"}},
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_rejects_conflicting_result_identity_aliases() -> None:
    raw = json.dumps(
        {
            "tool_name": "terminal",
            "toolName": "mcp_github_get_file",
            "stdout": numbered("exact source", 180),
        }
    )

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={"name": "terminal", "arguments": {"command": "pytest -q"}},
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_rejects_object_form_protected_result_identity() -> None:
    raw = json.dumps(
        {
            "tool": {"name": "mcp_github_get_file"},
            "stdout": numbered("exact source", 180),
        }
    )

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={"name": "terminal", "arguments": {"command": "pytest -q"}},
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_rejects_result_input_command_conflict() -> None:
    raw = json.dumps(
        {
            "input": {"command": "cat important.py"},
            "stdout": numbered("exact source", 180),
            "exit": 0,
        }
    )

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={"name": "terminal", "arguments": {"command": "pytest -q"}},
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_rejects_nested_resolved_argument_command_conflict() -> None:
    raw = json.dumps({"stdout": numbered("exact source", 180), "exit": 0})

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={
                "name": "terminal",
                "arguments": {
                    "command": "pytest -q",
                    "input": {"command": "cat important.py"},
                },
            },
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_rejects_nested_protected_result_identity() -> None:
    raw = json.dumps(
        {
            "input": {"tool": {"name": "mcp_github_get_file"}},
            "stdout": numbered("exact source", 180),
            "exit": 0,
        }
    )

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={"name": "terminal", "arguments": {"command": "pytest -q"}},
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_rejects_nested_named_protected_result_identity() -> None:
    raw = json.dumps(
        {
            "input": {"name": "mcp_github_get_file"},
            "stdout": numbered("exact source", 180),
            "exit": 0,
        }
    )

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={"name": "terminal", "arguments": {"command": "pytest -q"}},
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_rejects_object_form_tool_owned_command_conflict() -> None:
    raw = json.dumps(
        {
            "tool": {
                "name": "terminal",
                "input": {"command": "cat important.py"},
            },
            "stdout": numbered("exact source", 180),
            "exit": 0,
        }
    )

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={"name": "terminal", "arguments": {"command": "pytest -q"}},
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_rejects_protected_identity_in_resolved_arguments() -> None:
    raw = json.dumps({"stdout": numbered("exact source", 180), "exit": 0})

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={
                "name": "terminal",
                "arguments": {
                    "tool_name": "mcp_github_get_file",
                    "command": "pytest -q",
                },
            },
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_rejects_root_name_identity_in_resolved_arguments() -> None:
    raw = json.dumps({"stdout": numbered("exact source", 180), "exit": 0})

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={
                "name": "terminal",
                "arguments": {
                    "name": "mcp_github_get_file",
                    "command": "pytest -q",
                },
            },
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_treats_root_result_name_as_label() -> None:
    raw = json.dumps(
        {
            "name": "pytest shard",
            "stdout": numbered("pytest output", 180),
            "exit": 0,
        }
    )

    transformed = transform_tool_result(
        raw,
        tool_name="tool_call",
        args={"name": "terminal", "arguments": {"command": "pytest -q"}},
        noisegate_max_chars=120,
    )

    payload = parse_hook_result(transformed)
    assert payload["name"] == "pytest shard"
    assert "[noisegate: omitted" in payload["stdout"]


def test_tool_call_wrapper_rejects_sibling_call_ownership() -> None:
    raw = json.dumps({"stdout": numbered("exact source", 180), "exit": 0})

    for sibling in (
        {"name": "mcp_github_get_file", "arguments": {}},
        {"name": "terminal", "arguments": {"command": "cat important.py"}},
    ):
        assert (
            transform_tool_result(
                raw,
                tool_name="tool_call",
                args={
                    "name": "terminal",
                    "arguments": {"command": "pytest -q"},
                    "call": sibling,
                },
                noisegate_max_chars=120,
            )
            is None
        )


def test_tool_call_wrapper_rejects_intermediate_object_tool_ownership() -> None:
    raw = json.dumps({"stdout": numbered("exact source", 180), "exit": 0})

    for intermediate_evidence in (
        {"command": "cat important.py"},
        {"call": {"name": "mcp_github_get_file", "arguments": {}}},
    ):
        intermediate = {
            **intermediate_evidence,
            "tool": {
                "name": "terminal",
                "arguments": {"command": "pytest -q"},
            },
        }
        assert (
            transform_tool_result(
                raw,
                tool_name="tool_call",
                args={"name": "tool_call", "arguments": {"tool": intermediate}},
                noisegate_max_chars=120,
            )
            is None
        )


def test_tool_call_wrapper_rejects_identityless_sibling_ownership() -> None:
    raw = json.dumps({"stdout": numbered("exact source", 180), "exit": 0})

    for sibling in (
        {"tool_name": "mcp_github_get_file"},
        {"command": "cat important.py"},
    ):
        assert (
            transform_tool_result(
                raw,
                tool_name="tool_call",
                args={
                    "call": {
                        "name": "terminal",
                        "arguments": {"command": "pytest -q"},
                    },
                    "arguments": sibling,
                },
                noisegate_max_chars=120,
            )
            is None
        )


def test_tool_call_wrapper_rejects_malformed_identity_types() -> None:
    raw = json.dumps({"stdout": numbered("exact source", 180), "exit": 0})

    for malformed in (
        {"tool_name": {"name": "mcp_github_get_file"}},
        {"name": ["terminal"]},
        {"tool": 7},
    ):
        assert (
            transform_tool_result(
                raw,
                tool_name="tool_call",
                args={**malformed, "command": "pytest -q"},
                noisegate_max_chars=120,
            )
            is None
        )


def test_tool_call_wrapper_treats_nonstring_root_result_name_as_label() -> None:
    result_with_name = json.dumps(
        {
            "name": ["pytest shard"],
            "stdout": numbered("pytest output", 180),
            "exit": 0,
        }
    )

    transformed = transform_tool_result(
        result_with_name,
        tool_name="tool_call",
        args={"name": "terminal", "arguments": {"command": "pytest -q"}},
        noisegate_max_chars=120,
    )

    payload = parse_hook_result(transformed)
    assert payload["name"] == ["pytest shard"]
    assert "[noisegate: omitted" in payload["stdout"]


def test_tool_call_wrapper_rejects_malformed_name_identity_in_arguments() -> None:
    assert (
        transform_tool_result(
            json.dumps({"stdout": numbered("exact source", 180), "exit": 0}),
            tool_name="tool_call",
            args={
                "name": "terminal",
                "arguments": {
                    "name": ["mcp_github_get_file"],
                    "command": "pytest -q",
                },
            },
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_with_detached_arguments_stays_exact() -> None:
    raw = json.dumps({"stdout": numbered("source", 180)})

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={"name": "terminal"},
            arguments={"command": "cat important.py"},
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_rejects_duplicate_json_identity_keys() -> None:
    raw = json.dumps({"stdout": numbered("exact", 180)})
    encoded = (
        '{"name":"mcp_github_get_file","name":"terminal",'
        '"arguments":{"command":"pytest -q"}}'
    )

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={"name": "tool_call", "arguments": encoded},
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_rejects_oversized_encoded_arguments() -> None:
    raw = json.dumps({"stdout": numbered("exact", 180)})
    encoded_json = json.dumps(
        {
            "name": "terminal",
            "arguments": {"command": "pytest -q", "padding": "x" * 70_000},
        }
    )

    for encoded in (encoded_json, " " * 70_000):
        assert (
            transform_tool_result(
                raw,
                tool_name="tool_call",
                args={"name": "tool_call", "arguments": encoded},
                noisegate_max_chars=120,
            )
            is None
        )


def test_tool_call_wrapper_container_preserves_exact_command() -> None:
    raw = json.dumps({"stdout": numbered("source", 180)})

    for container in ("call", "request"):
        assert (
            transform_tool_result(
                raw,
                tool_name="tool_call",
                args={
                    container: {
                        "name": "terminal",
                        "arguments": {"command": "cat important.py"},
                    }
                },
                noisegate_max_chars=120,
            )
            is None
        )


def test_tool_call_wrapper_uses_arguments_owned_by_nested_tool_identity() -> None:
    raw = json.dumps({"stdout": numbered("source", 180)})

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args={
                "tool": {
                    "name": "terminal",
                    "arguments": {"command": "cat important.py"},
                }
            },
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_cycle_fails_closed() -> None:
    raw = json.dumps({"stdout": numbered("exact", 180)})
    wrapper: dict[str, object] = {"name": "tool_call"}
    wrapper["arguments"] = wrapper

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args=wrapper,
            noisegate_max_chars=120,
        )
        is None
    )


def test_tool_call_wrapper_shared_mapping_fails_closed() -> None:
    raw = json.dumps({"stdout": numbered("exact", 180)})
    nested: dict[str, object] = {
        "name": "terminal",
        "arguments": {"command": "pytest -q"},
    }
    wrapper = {"name": "tool_call", "args": nested, "arguments": nested}

    assert (
        transform_tool_result(
            raw,
            tool_name="tool_call",
            args=wrapper,
            noisegate_max_chars=120,
        )
        is None
    )


def test_unknown_tool_result_is_not_touched() -> None:
    raw = json.dumps({"content": numbered("unknown but useful", 120)})

    assert transform_tool_result(raw, tool_name="future_tool", noisegate_max_chars=100) is None


def test_source_like_payloads_from_exact_context_tools_are_not_touched() -> None:
    exact = source_like_payload()
    raw = json.dumps({"content": exact, "output": exact, "result": exact})

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
        assert transform_tool_result(raw, tool_name=tool_name, noisegate_max_chars=200) is None


def test_only_known_noisy_tool_results_are_compacted() -> None:
    long_text = numbered("tool output", 120)
    cases = {
        "terminal": json.dumps({"stdout": long_text, "exit": 0}),
        "process": json.dumps(
            {"action": "log", "output": f"{long_text}\nERROR process failed", "exit_code": 0}
        ),
        "read_terminal": json.dumps({"output": long_text, "status": "ok"}),
        "browser_console": json.dumps({"logs": long_text}),
    }

    for tool_name, raw in cases.items():
        transformed = transform_tool_result(raw, tool_name=tool_name, noisegate_max_chars=100)
        assert isinstance(transformed, str)


def test_known_context_and_side_effect_tool_results_are_not_compacted() -> None:
    raw = json.dumps(
        {
            "content": numbered("important context", 120),
            "output": numbered("important output", 120),
            "logs": numbered("important logs", 120),
        }
    )
    # Snapshot of current Hermes built-in/MCP-facing tool names, minus the
    # explicit noisy allowlist covered above.
    non_noisy_tools = (
        "browser_back",
        "browser_cdp",
        "browser_click",
        "browser_dialog",
        "browser_get_images",
        "browser_navigate",
        "browser_press",
        "browser_scroll",
        "browser_snapshot",
        "browser_type",
        "browser_vision",
        "clarify",
        "close_terminal",
        "computer_use",
        "cronjob",
        "delegate_task",
        "discord",
        "discord_admin",
        "execute_code",
        "feishu_doc_read",
        "feishu_drive_add_comment",
        "feishu_drive_list_comment_replies",
        "feishu_drive_list_comments",
        "feishu_drive_reply_comment",
        "ha_call_service",
        "ha_get_state",
        "ha_list_entities",
        "ha_list_services",
        "image_generate",
        "kanban_block",
        "kanban_comment",
        "kanban_complete",
        "kanban_create",
        "kanban_heartbeat",
        "kanban_link",
        "kanban_list",
        "kanban_show",
        "kanban_unblock",
        "memory",
        "patch",
        "project_create",
        "project_list",
        "project_switch",
        "read_file",
        "search_files",
        "session_search",
        "skill_manage",
        "skill_view",
        "skills_list",
        "spotify_albums",
        "spotify_devices",
        "spotify_library",
        "spotify_playback",
        "spotify_playlists",
        "spotify_queue",
        "spotify_search",
        "text_to_speech",
        "todo",
        "video_analyze",
        "video_generate",
        "vision_analyze",
        "web_extract",
        "web_search",
        "write_file",
        "x_search",
        "xai_video_edit",
        "xai_video_extend",
        "yb_query_group_info",
        "yb_query_group_members",
        "yb_search_sticker",
        "yb_send_dm",
        "yb_send_sticker",
    )

    for tool_name in non_noisy_tools:
        assert transform_tool_result(raw, tool_name=tool_name, noisegate_max_chars=100) is None


def test_patch_tool_result_is_not_touched() -> None:
    raw = "\n".join(["*** Begin Patch", *numbered("+line", 100), "*** End Patch"])

    assert transform_tool_result(raw, tool_name="patch", noisegate_max_chars=100) is None


def test_terminal_tool_result_keeps_source_like_file_display_exact() -> None:
    exact = source_like_payload()
    raw = terminal_result(exact, command="nl -ba src/source_fixture.py")

    assert transform_tool_result(raw, tool_name="terminal", noisegate_max_chars=200) is None


def test_terminal_tool_result_uses_args_when_payload_command_is_blank() -> None:
    exact = source_like_payload()
    raw = terminal_result(exact, command="")

    assert (
        transform_tool_result(
            raw,
            tool_name="terminal",
            args={"cmd": "cat src/source_fixture.py"},
            noisegate_max_chars=200,
        )
        is None
    )


def test_terminal_tool_result_uses_arguments_when_args_has_no_command() -> None:
    exact = source_like_payload()
    raw = terminal_result(exact, command="")

    assert (
        transform_tool_result(
            raw,
            tool_name="terminal",
            args={"timeout": 10},
            arguments={"cmd": "cat src/source_fixture.py"},
            noisegate_max_chars=200,
        )
        is None
    )


def test_terminal_tool_result_args_command_alias_wins_over_arguments_command() -> None:
    exact = source_like_payload()
    raw = terminal_result(exact, command="")

    assert (
        transform_tool_result(
            raw,
            tool_name="terminal",
            args={"cmd": "cat src/source_fixture.py"},
            arguments={"command": "pytest -q"},
            noisegate_max_chars=200,
        )
        is None
    )


def test_terminal_tool_result_args_command_alias_wins_over_payload_command() -> None:
    exact = source_like_payload()
    raw = terminal_result(exact, command="pytest -q")

    assert (
        transform_tool_result(
            raw,
            tool_name="terminal",
            args={"cmd": "cat src/source_fixture.py"},
            noisegate_max_chars=200,
        )
        is None
    )


def test_terminal_tool_result_protected_payload_command_wins_over_stale_args_command() -> None:
    exact = source_like_payload()

    for payload_command in (
        "cat src/source_fixture.py",
        "cat '>README'",
        "cat src/source_fixture.py >/dev/stdout",
    ):
        raw = terminal_result(exact, command=payload_command)

        assert (
            transform_tool_result(
                raw,
                tool_name="terminal",
                args={"command": "pytest -q"},
                noisegate_max_chars=200,
            )
            is None
        ), payload_command

    dominant_source = exact + "\ntests/test_api.py::test_literal PASSED"
    raw = terminal_result(
        dominant_source,
        command="cat src/source_fixture.py >/dev/stdout",
    )
    assert (
        transform_tool_result(
            raw,
            tool_name="terminal",
            args={"command": "pytest -q"},
            noisegate_max_chars=200,
        )
        is None
    )


def test_command_derived_file_read_wins_before_output_inferred_diff_class() -> None:
    diff = "\n".join(
        [
            "diff --git a/app.py b/app.py",
            "--- a/app.py",
            "+++ b/app.py",
            "@@ -1,2 +1,2 @@",
            "-old",
            "+new",
            *[f"+exact diff line {index:03d}" for index in range(120)],
        ]
    )
    raw = terminal_result(diff, command="cat patches/change.diff")

    assert (
        transform_tool_result(
            raw,
            tool_name="terminal",
            args={"command": "pytest -q"},
            noisegate_max_chars=200,
            noisegate_max_lines=20,
            noisegate_preserve_diffs=False,
        )
        is None
    )


def test_evidence_backed_search_beats_stale_compound_empty_text_exact() -> None:
    exact = "\n".join(
        f"tests/test_{index}.py::test_target_{index} PASSED" for index in range(180)
    )
    raw = terminal_result(exact, command="cat README && pytest -q")

    assert (
        transform_tool_result(
            raw,
            tool_name="terminal",
            args={"command": 'rg "$(printf target)" src'},
            noisegate_max_chars=200,
            noisegate_max_lines=20,
        )
        is None
    )


def test_evidence_backed_search_beats_stale_git_diff_when_diff_preservation_is_off() -> None:
    exact = "\n".join(
        f"patches/change.diff:{index}:target +exact diff line" for index in range(180)
    )
    raw = terminal_result(exact, command="git diff -- app.py")

    assert (
        transform_tool_result(
            raw,
            tool_name="terminal",
            args={"command": 'rg "$(printf target)" patches'},
            noisegate_max_chars=200,
            noisegate_max_lines=20,
            noisegate_preserve_diffs=False,
        )
        is None
    )


def test_terminal_tool_result_keeps_args_precedence_for_noisy_commands() -> None:
    raw = terminal_result(numbered("stdout", 80), command="npm install")

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        args={"command": "pytest -q"},
        noisegate_max_chars=200,
    )

    payload = parse_hook_result(transformed)
    assert payload["noisegate"]["fields"]["stdout"]["command_class"] == "pytest"


def test_protected_aliases_win_when_exact_content_looks_noisy() -> None:
    npm_like_source = "\n".join(
        f"npm ERR! code E404 exact-source-{index}" for index in range(180)
    )
    cases = (
        ({"command": "npm install missing-package", "cmd": "cat build.log"}, {}, ""),
        ({"command": "npm install missing-package", "argv": ["cat", "build.log"]}, {}, ""),
        ({"command": "npm install missing-package"}, {"command": "cat build.log"}, ""),
        ({"command": "npm install missing-package"}, {}, "cat build.log"),
        ({"command": "npm install missing-package"}, {}, "rg --no-filename target src"),
    )

    for args, arguments, payload_command in cases:
        raw = terminal_result(npm_like_source, command=payload_command, exit_code=1)
        assert (
            transform_tool_result(
                raw,
                tool_name="terminal",
                args=args,
                arguments=arguments,
                noisegate_max_chars=300,
                noisegate_max_lines=20,
            )
            is None
        ), (args, arguments, payload_command)

    pytest_outputs = (
        "\n".join(
            [
                *[f"tests/test_{index}.py::test_case_{index} PASSED" for index in range(180)],
                "================ 180 passed in 2.00s ================",
            ]
        ),
        "\n".join(
            [
                "============================= test session starts =============================",
                "tests/test_api.py::test_broken FAILED",
                "_______________________________ test_broken _______________________________",
                "def test_broken():",
                "    assert False",
                "E   assert False",
                *[f"tests/test_{index}.py::test_case_{index} PASSED" for index in range(160)],
                "=========================== short test summary info ===========================",
                "FAILED tests/test_api.py::test_broken - assert False",
                "1 failed, 160 passed in 2.00s",
            ]
        ),
    )
    for exact in pytest_outputs:
        assert (
            transform_tool_result(
                terminal_result(exact, command="cat test-results.txt"),
                tool_name="terminal",
                args={"command": "pytest -q"},
                noisegate_max_chars=400,
                noisegate_max_lines=20,
            )
            is None
        )

    partial_read = json.dumps(
        {
            "command": "cat build.log missing.txt",
            "stdout": npm_like_source,
            "stderr": "cat: missing.txt: No such file or directory",
            "exit": 1,
            "status": "failed",
        }
    )
    assert (
        transform_tool_result(
            partial_read,
            tool_name="terminal",
            args={"command": "npm install"},
            noisegate_max_chars=300,
            noisegate_max_lines=20,
        )
        is None
    )


def test_terminal_tool_result_preserves_argv_file_display_with_metachar_paths() -> None:
    exact = source_like_payload()
    raw = terminal_result(exact, command="")

    for path in ("src/A&B.py", "src/A>B.py", "src/A;B.py", "src/$(fixture).py"):
        assert (
            transform_tool_result(
                raw,
                tool_name="terminal",
                args={"argv": ["cat", path]},
                noisegate_max_chars=200,
            )
            is None
        )


def test_bad_json_fails_open_for_tool_result_hook() -> None:
    raw = "{not json" + numbered("line", 100)

    assert transform_tool_result(raw, tool_name="terminal", noisegate_max_chars=100) is None


def test_tool_result_hook_fail_open_catches_reducer_exceptions(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("host adapter should never see this")

    monkeypatch.setattr(plugin, "_reduce_text_in_operation", boom)
    raw = terminal_result(numbered("line", 100), command="pytest")

    assert transform_tool_result(raw, tool_name="terminal", noisegate_max_chars=100) is None


def test_noisy_generic_json_string_field_can_be_compacted() -> None:
    raw = json.dumps({"logs": numbered("console log", 100), "ok": True})

    transformed = transform_tool_result(raw, tool_name="browser_console", noisegate_max_chars=120)

    payload = parse_hook_result(transformed)
    assert payload["ok"] is True
    assert "[noisegate: omitted" in payload["logs"]
    assert payload["noisegate"]["fields"]["logs"]["original_lines"] == 100


def test_terminal_json_field_remaps_colliding_upstream_line_marker() -> None:
    raw_lines = [
        "head-" + ("h" * 80),
        "line-1-" + ("x" * 80),
        "line-2-" + ("x" * 80),
        "line-3-" + ("x" * 80),
        "[noisegate: omitted 8 lines]",
        "line-5-" + ("x" * 80),
        "line-6-" + ("x" * 80),
        "line-7-" + ("x" * 80),
        "line-8-" + ("x" * 80),
        "tail-" + ("t" * 80),
    ]
    raw = terminal_result("\n".join(raw_lines), command="make noisy")

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        noisegate_max_chars=10_000,
        noisegate_max_lines=3,
        noisegate_head_lines=1,
        noisegate_tail_lines=1,
    )

    payload = parse_hook_result(transformed)
    stdout = payload["stdout"]
    assert isinstance(stdout, str)
    assert stdout.splitlines() == [
        raw_lines[0],
        "[noisegate: omitted 15 lines]",
        raw_lines[-1],
    ]
    assert engine._represented_line_coverage(stdout) == 17


def test_terminal_json_artifact_rewrite_fails_open_with_upstream_marker(
    tmp_path: Path,
) -> None:
    raw_lines = [
        "head-" + ("h" * 80),
        "line-1-" + ("x" * 80),
        "line-2-" + ("x" * 80),
        "line-3-" + ("x" * 80),
        "[noisegate: omitted 8 lines]",
        "line-5-" + ("x" * 80),
        "line-6-" + ("x" * 80),
        "line-7-" + ("x" * 80),
        "line-8-" + ("x" * 80),
        "tail-" + ("t" * 80),
    ]
    artifact_dir = tmp_path / "artifacts"
    raw = terminal_result("\n".join(raw_lines), command="make noisy", exit_code=1)

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        noisegate_max_chars=220,
        noisegate_max_lines=3,
        noisegate_head_lines=1,
        noisegate_tail_lines=1,
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(artifact_dir),
    )

    assert transformed is None
    assert not artifact_dir.exists()


def test_terminal_json_rejects_non_utf8_candidate_before_artifact_exposure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    stdout = numbered("FAILED tests/test_example.py::test_case - AssertionError", 120)
    raw = (
        '{"command":"pytest","stdout":'
        f'{json.dumps(stdout)},"source":"\\ud800"}}'
    )
    store_calls: list[str] = []

    def record_store(
        text: str,
        options: engine.NoisegateOptions,
    ) -> dict[str, engine.JsonValue]:
        store_calls.append(text)
        return engine._plan_artifact(text, options)

    monkeypatch.setattr(plugin, "_store_artifact", record_store)
    artifact_dir = tmp_path / "artifacts"

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        noisegate_max_chars=500,
        noisegate_max_lines=10,
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(artifact_dir),
    )
    plans: list[plugin._ArtifactPreviewPlan] = []
    preview = plugin._preview_tool_result(
        raw,
        tool_name="terminal",
        artifact_plans_out=plans,
        noisegate_max_chars=500,
        noisegate_max_lines=10,
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(artifact_dir),
    )

    assert transformed is None
    assert preview is None
    assert store_calls == []
    assert plans == []
    assert not artifact_dir.exists()


def test_transform_tool_result_skips_json_rewrite_when_metadata_would_grow_result() -> None:
    raw = json.dumps({"stdout": "A" * 4010})

    transformed = transform_tool_result(raw, tool_name="terminal")

    assert transformed is None


def test_transform_tool_result_skips_top_level_json_string_when_rewrite_would_grow() -> None:
    raw = json.dumps("A" * 34)

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        noisegate_max_chars=33,
        noisegate_max_lines=10,
        noisegate_head_lines=1,
        noisegate_tail_lines=1,
    )

    assert transformed is None


def test_transform_tool_result_does_not_store_top_level_string_artifact_on_no_gain(
    monkeypatch,
) -> None:
    store_calls: list[str] = []

    def fake_store(text: str, _options) -> dict[str, object]:
        store_calls.append(text)
        return {
            "stored": True,
            "id": "ng_" + ("a" * 24),
            "sha256": "b" * 64,
            "size_bytes": len(text.encode()),
        }

    monkeypatch.setattr(plugin, "_store_artifact", fake_store)
    raw = json.dumps("A" * 200)

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        noisegate_max_chars=197,
        noisegate_max_lines=10,
        noisegate_head_lines=1,
        noisegate_tail_lines=1,
        noisegate_artifacts=True,
    )

    assert transformed is None
    assert store_calls == []


def test_preview_top_level_json_string_reports_structured_artifact_plan(
    monkeypatch,
) -> None:
    text = "\n".join(
        [
            "head",
            "[noisegate: omitted 8 lines]",
            *[f"middle-{index}-" + ("x" * 80) for index in range(30)],
            "tail",
        ]
    )
    plans: list[plugin._ArtifactPreviewPlan] = []

    def unexpected_store(_text: str, _options) -> dict[str, object]:
        raise AssertionError("preview must not store")

    monkeypatch.setattr(plugin, "_store_artifact", unexpected_store)

    transformed = plugin._preview_tool_result(
        json.dumps(text),
        tool_name="terminal",
        args={"command": "pytest -q"},
        noisegate_exit_code=1,
        noisegate_max_chars=1000,
        noisegate_max_lines=5,
        noisegate_artifacts=True,
        artifact_plans_out=plans,
    )

    assert isinstance(transformed, str)
    compacted = json.loads(transformed)
    assert len(plans) == 1
    assert plans[0].original_text == text
    assert plans[0].artifact_id in compacted
    assert plans[0].recovery_notice in compacted.splitlines()
    assert plans[0].owner_path == ()
    assert compacted.splitlines().count("[noisegate: exit_code=1]") == 1


def test_store_artifact_preview_plan_removes_new_file_when_read_verification_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "artifacts"
    original = numbered("raw terminal output", 100)
    options = engine.NoisegateOptions(
        artifact_enabled=True,
        artifact_dir=artifact_dir,
    )
    plan = plugin._artifact_preview_plan(
        original,
        {"artifact": engine._plan_artifact(original, options)},
        artifact_dir=options.artifact_dir,
    )
    assert plan is not None

    def fail_read(_store: plugin.ArtifactStore, _artifact_id: str) -> str:
        raise plugin.ArtifactError("verification failed")

    monkeypatch.setattr(plugin.ArtifactStore, "read", fail_read)

    assert plugin._store_artifact_preview_plan(plan, options) is False
    assert not (artifact_dir / f"{plan.artifact_id}.txt").exists()
    assert not artifact_dir.exists() or list(artifact_dir.iterdir()) == []


def test_store_artifact_preview_plan_preserves_preexisting_file_on_read_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "artifacts"
    original = numbered("raw terminal output", 100)
    options = engine.NoisegateOptions(
        artifact_enabled=True,
        artifact_dir=artifact_dir,
    )
    stored = plugin.ArtifactStore(artifact_dir).store(original)
    plan = plugin._artifact_preview_plan(
        original,
        {"artifact": {"stored": True, **stored.to_metadata()}},
        artifact_dir=artifact_dir,
    )
    assert plan is not None
    target = artifact_dir / f"{plan.artifact_id}.txt"

    def fail_read(_store: plugin.ArtifactStore, _artifact_id: str) -> str:
        raise plugin.ArtifactError("verification failed")

    monkeypatch.setattr(plugin.ArtifactStore, "read", fail_read)

    assert plugin._store_artifact_preview_plan(plan, options) is False
    assert target.read_text(encoding="utf-8") == original
    assert list(artifact_dir.glob(".ng_*.tmp")) == []


def test_store_artifact_preview_plan_rolls_back_when_store_raises_after_publication(
    monkeypatch,
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "artifacts"
    original = numbered("raw terminal output", 100)
    options = engine.NoisegateOptions(
        artifact_enabled=True,
        artifact_dir=artifact_dir,
    )
    plan = plugin._artifact_preview_plan(
        original,
        {"artifact": engine._plan_artifact(original, options)},
        artifact_dir=artifact_dir,
    )
    assert plan is not None
    real_store = plugin._store_artifact

    def store_then_raise(text: str, current_options: engine.NoisegateOptions):
        real_store(text, current_options)
        raise RuntimeError("post-write metadata failure")

    monkeypatch.setattr(plugin, "_store_artifact", store_then_raise)

    assert plugin._store_artifact_preview_plan(plan, options) is False
    assert not (artifact_dir / f"{plan.artifact_id}.txt").exists()
    assert not list(artifact_dir.glob(".ng_*.tmp"))


def test_store_artifact_preview_plan_accepts_store_with_nested_receipt_capture(
    monkeypatch,
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "artifacts"
    original = numbered("raw terminal output", 100)
    options = engine.NoisegateOptions(
        artifact_enabled=True,
        artifact_dir=artifact_dir,
    )
    plan = plugin._artifact_preview_plan(
        original,
        {"artifact": engine._plan_artifact(original, options)},
        artifact_dir=artifact_dir,
    )
    assert plan is not None
    real_store = plugin._store_artifact

    def nested_capture_store(text: str, current_options: engine.NoisegateOptions):
        with plugin._capture_artifact_write_receipts():
            return real_store(text, current_options)

    monkeypatch.setattr(plugin, "_store_artifact", nested_capture_store)

    accepted = plugin._store_artifact_preview_plan(plan, options)

    assert accepted is True
    assert plugin.ArtifactStore(artifact_dir).read(plan.artifact_id) == original


def test_store_artifact_preview_plan_ignores_unrelated_created_receipt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "artifacts"
    original = numbered("planned terminal output", 100)
    concurrent = numbered("distinct concurrent output", 100)
    options = engine.NoisegateOptions(
        artifact_enabled=True,
        artifact_dir=artifact_dir,
    )
    planned_artifact = plugin.ArtifactStore(artifact_dir).store(original)
    plan = plugin._artifact_preview_plan(
        original,
        {"artifact": {"stored": True, **planned_artifact.to_metadata()}},
        artifact_dir=artifact_dir,
    )
    assert plan is not None
    real_store = plugin._store_artifact

    def store_plan_then_concurrent(
        text: str,
        current_options: engine.NoisegateOptions,
    ) -> dict[str, object]:
        planned_metadata = real_store(text, current_options)
        real_store(concurrent, current_options)
        return planned_metadata

    monkeypatch.setattr(plugin, "_store_artifact", store_plan_then_concurrent)

    accepted = plugin._store_artifact_preview_plan(plan, options)

    store = plugin.ArtifactStore(artifact_dir)
    concurrent_id = engine._plan_artifact(concurrent, options)["id"]
    assert isinstance(concurrent_id, str)
    assert store.read(plan.artifact_id) == original
    assert store.read(concurrent_id) == concurrent
    assert accepted is True


def test_preview_metadata_does_not_accept_artifact_id_substring_as_recovery_notice() -> None:
    original = numbered("raw terminal output", 100)
    options = engine.NoisegateOptions(
        artifact_enabled=True,
        artifact_dir=Path("a"),
    )
    metadata = {"artifact": engine._plan_artifact(original, options)}
    artifact = metadata["artifact"]
    assert isinstance(artifact, dict)
    artifact_id = artifact["id"]
    assert isinstance(artifact_id, str)

    plugin._mark_artifact_notice_dropped_if_missing(
        metadata,
        f"incidental metadata id={artifact_id}",
        artifact_dir=options.artifact_dir,
    )

    assert metadata["artifact"] == {
        "stored": False,
        "reason": "recovery_notice_dropped",
        "size_bytes": len(original.encode()),
    }


def test_top_level_json_string_rejects_whitespace_only_gain_after_artifact_fallback(
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
    raw = " " + json.dumps(text) + " "
    artifact_dir = tmp_path / "artifacts"

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        noisegate_exit_code=1,
        noisegate_max_chars=10_000,
        noisegate_max_lines=4,
        noisegate_head_lines=1,
        noisegate_tail_lines=1,
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(artifact_dir),
    )

    assert transformed is None
    assert not artifact_dir.exists()


def test_transform_tool_result_does_not_write_artifact_when_json_candidate_is_dropped(
    tmp_path: Path,
) -> None:
    raw = json.dumps({"stdout": "A" * 4010})
    artifact_dir = tmp_path / "artifacts"

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(artifact_dir),
    )

    assert transformed is None
    assert not artifact_dir.exists() or list(artifact_dir.iterdir()) == []


def test_transform_tool_result_artifact_notice_does_not_duplicate_exit_code(tmp_path: Path) -> None:
    raw = terminal_result(numbered("line", 1000), command="pytest", exit_code=1)
    artifact_dir = tmp_path / "artifacts"

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        noisegate_max_chars=1000,
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(artifact_dir),
    )

    payload = parse_hook_result(transformed)
    stdout = payload["stdout"]
    assert isinstance(stdout, str)
    assert stdout.count("[noisegate: exit_code=1]") == 1
    assert "[noisegate artifact: id=ng_" in stdout


def test_transform_tool_result_multi_field_artifacts_stay_inline_only(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    raw = json.dumps(
        {
            "command": "make noisy",
            "stdout": numbered("stdout", 600),
            "stderr": numbered("stderr", 600),
        }
    )

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        noisegate_max_chars=500,
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(artifact_dir),
    )

    payload = parse_hook_result(transformed)
    assert "[noisegate: omitted" in json.dumps(payload)
    assert "[noisegate artifact:" not in json.dumps(payload)
    assert not artifact_dir.exists()


def test_transform_tool_result_ignores_internal_preview_keyword(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    raw = terminal_result(numbered("line", 1000), command="pytest")

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        noisegate_max_chars=1000,
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(artifact_dir),
        noisegate_defer_artifact_store=True,
        defer_artifact_store=True,
    )

    payload = parse_hook_result(transformed)
    stdout = payload["stdout"]
    assert isinstance(stdout, str)
    assert "[noisegate artifact: id=ng_" in stdout
    artifact_files = list(artifact_dir.glob("ng_*.txt"))
    assert len(artifact_files) == 1
    assert artifact_files[0].stem in stdout


def test_transform_tool_result_rebuilds_artifact_notice_after_store_failure(tmp_path: Path) -> None:
    raw = terminal_result(numbered("line", 1000), command="pytest")
    artifact_file = tmp_path / "not-a-dir"
    artifact_file.write_text("x", encoding="utf-8")

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        noisegate_max_chars=1000,
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(artifact_file),
    )

    payload = parse_hook_result(transformed)
    stdout = payload["stdout"]
    assert isinstance(stdout, str)
    assert "id=ng_" not in stdout
    assert "reason=artifact_error" in stdout
    metadata = payload["noisegate"]["fields"]["stdout"]["artifact"]
    assert metadata["stored"] is False


def test_transform_tool_result_artifact_notice_does_not_replace_failure(
    tmp_path: Path,
) -> None:
    stdout = "\n".join(
        [
            *["setup noise " + ("x" * 80) for _ in range(30)],
            "FAILED tests/test_middle.py::test_breaks - AssertionError: boom",
            *["post noise " + ("z" * 80) for _ in range(30)],
        ]
    )
    raw = terminal_result(stdout, command="pytest -q", exit_code=1)

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        noisegate_max_chars=90,
        noisegate_max_lines=10,
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(tmp_path / "artifacts"),
    )

    if transformed is None:
        return
    payload = parse_hook_result(transformed)
    stdout = payload["stdout"]
    assert isinstance(stdout, str)
    assert len(stdout) <= 90
    assert "FAILED" in stdout


def test_transform_tool_result_artifact_notice_preserves_failure_for_failed_exact_tail(
    tmp_path: Path,
) -> None:
    stdout = "\n".join(
        [
            *["setup noise " + ("x" * 80) for _ in range(30)],
            "FAILED tests/test_middle.py::test_breaks - AssertionError: boom",
            *["post noise " + ("z" * 80) for _ in range(30)],
        ]
    )
    raw = terminal_result(stdout, command="pytest -q && cat file.py", exit_code=1)

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        noisegate_max_chars=140,
        noisegate_max_lines=10,
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(tmp_path / "artifacts"),
    )

    if transformed is None:
        return
    payload = parse_hook_result(transformed)
    reduced_stdout = payload["stdout"]
    assert isinstance(reduced_stdout, str)
    assert "FAILED tests/test_middle.py::test_breaks - AssertionError: boom" in reduced_stdout


def test_transform_tool_result_artifact_notice_uses_original_output_for_preservation(
    tmp_path: Path,
) -> None:
    original_stdout = "\n".join(
        [
            "=== FAILURES ===",
            *["setup noise " + ("x" * 80) for _ in range(12)],
            "FAILED tests/test_middle.py::test_breaks - AssertionError: boom",
            *["post noise " + ("z" * 80) for _ in range(12)],
        ]
    )
    raw = json.dumps({"stdout": original_stdout, "exit_code": 1})

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        # Truthful prefix/tail markers + failure + exit notice need 146 chars.
        noisegate_max_chars=150,
        noisegate_max_lines=10,
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(tmp_path / "artifacts"),
    )

    payload = parse_hook_result(transformed)
    stdout = payload["stdout"]
    assert isinstance(stdout, str)
    assert "FAILED tests/test_middle.py::test_breaks - AssertionError: boom" in stdout
    assert "FAILED tests/test_middle\n" not in stdout


def test_transform_tool_result_does_not_store_artifact_when_recovery_notice_drops(
    monkeypatch, tmp_path: Path
) -> None:
    store_calls: list[str] = []

    def fake_store(text: str, _options) -> dict[str, object]:
        store_calls.append(text)
        return {
            "stored": True,
            "id": "ng_" + ("a" * 24),
            "sha256": "b" * 64,
            "size_bytes": len(text.encode()),
        }

    original_stdout = "\n".join(
        [
            *["setup noise " + ("x" * 80) for _ in range(30)],
            "FAILED tests/test_middle.py::test_breaks - AssertionError: boom",
            *["post noise " + ("z" * 80) for _ in range(30)],
        ]
    )
    raw = terminal_result(original_stdout, command="pytest -q", exit_code=1)
    monkeypatch.setattr(plugin, "_store_artifact", fake_store)

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        # Truthful prefix/tail markers + failure + exit notice need 146 chars.
        noisegate_max_chars=150,
        noisegate_max_lines=10,
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(tmp_path / "artifacts"),
    )

    payload = parse_hook_result(transformed)
    stdout = payload["stdout"]
    assert isinstance(stdout, str)
    assert "FAILED" in stdout
    assert "id=ng_" not in stdout
    metadata = payload["noisegate"]["fields"]["stdout"]["artifact"]
    assert metadata == {
        "stored": False,
        "reason": "recovery_notice_too_long",
        "size_bytes": len(original_stdout.encode()),
    }
    assert store_calls == []


def test_transform_tool_result_does_not_treat_http_status_code_as_exit_code() -> None:
    raw = json.dumps({"status_code": 200, "content": numbered("html", 200)})

    transformed = transform_tool_result(raw, tool_name="browser_console", noisegate_max_chars=500)

    payload = parse_hook_result(transformed)
    content = payload["content"]
    assert isinstance(content, str)
    assert "exit_code=200" not in content
    assert "exit_code" not in payload["noisegate"]["fields"]["content"]


def test_transform_tool_result_rejects_protected_tool_before_json_parse(monkeypatch) -> None:
    def boom(_value: str) -> object:
        raise AssertionError("protected tool should not be parsed")

    monkeypatch.setattr(plugin.json, "loads", boom)

    assert transform_tool_result("{not actually parsed", tool_name="read_file") is None


def test_transform_tool_result_uses_args_command_when_payload_argv_empty() -> None:
    raw = json.dumps({"argv": [], "stdout": numbered("exact", 100)})

    assert (
        transform_tool_result(
            raw,
            tool_name="terminal",
            args={"command": "cat important.txt"},
            noisegate_max_chars=120,
        )
        is None
    )


def test_transform_tool_result_falls_back_from_empty_args_argv_to_arguments_command() -> None:
    raw = json.dumps({"stdout": numbered("exact", 100)})

    assert (
        transform_tool_result(
            raw,
            tool_name="terminal",
            args={"argv": []},
            arguments={"command": "cat important.txt"},
            noisegate_max_chars=120,
        )
        is None
    )


def test_transform_tool_result_preserves_embedded_protected_tool_without_tool_name() -> None:
    raw = json.dumps(
        {
            "tool_name": "read_file",
            "output": numbered("exact", 100),
            "status": "ok",
        }
    )

    assert transform_tool_result(raw, noisegate_max_chars=120) is None


def test_transform_tool_result_uses_embedded_args_for_blank_tool_name() -> None:
    raw = json.dumps(
        {
            "tool_name": "terminal",
            "args": {"command": "pytest"},
            "stdout": numbered("line", 100),
        }
    )

    transformed = transform_tool_result(raw, noisegate_max_chars=120)

    payload = parse_hook_result(transformed)
    stdout = payload["stdout"]
    assert isinstance(stdout, str)
    assert "[noisegate: omitted" in stdout
    assert payload["noisegate"]["fields"]["stdout"]["reducer"] == "pytest"


def test_transform_tool_result_prefers_embedded_args_over_outer_args() -> None:
    raw = json.dumps(
        {
            "tool_name": "terminal",
            "args": {"command": "cat important.txt"},
            "stdout": numbered("exact", 100),
        }
    )

    assert (
        transform_tool_result(
            raw,
            args={"command": "pytest"},
            noisegate_max_chars=120,
        )
        is None
    )


def test_transform_tool_result_prefers_host_args_for_explicit_tool_name() -> None:
    raw = json.dumps(
        {
            "args": {"command": "pytest"},
            "stdout": numbered("exact", 100),
        }
    )

    assert (
        transform_tool_result(
            raw,
            tool_name="terminal",
            args={"command": "cat important.txt"},
            noisegate_max_chars=120,
        )
        is None
    )


def test_transform_tool_result_ignores_embedded_args_when_explicit_host_args_empty() -> None:
    raw = json.dumps(
        {
            "args": {"command": "pytest"},
            "stdout": numbered("line", 100),
        }
    )

    for kwargs in (
        {"args": {}},
        {"args": {"command": ""}},
        {"arguments": {"argv": []}},
    ):
        transformed = transform_tool_result(
            raw,
            tool_name="terminal",
            noisegate_max_chars=120,
            **kwargs,
        )

        payload = parse_hook_result(transformed)
        metadata = payload["noisegate"]["fields"]["stdout"]
        assert metadata["command_class"] == "generic"
        assert metadata["reducer"] == "generic_head_tail"


def test_transform_tool_result_prefers_args_over_top_level_command() -> None:
    raw = json.dumps(
        {
            "command": "pytest",
            "args": {"command": "cat important.txt"},
            "stdout": numbered("exact", 100),
        }
    )

    assert transform_tool_result(raw, tool_name="terminal", noisegate_max_chars=120) is None


def test_transform_tool_result_infers_terminal_payload_without_tool_name() -> None:
    raw = json.dumps({"stdout": numbered("line", 100), "returncode": 1})

    transformed = transform_tool_result(raw, noisegate_max_chars=120)

    payload = parse_hook_result(transformed)
    stdout = payload["stdout"]
    assert isinstance(stdout, str)
    assert "[noisegate: omitted" in stdout
    assert "[noisegate: exit_code=1]" in stdout
    assert payload["noisegate"]["fields"]["stdout"]["exit_code"] == 1


def test_transform_tool_result_infers_terminal_payload_from_args_command() -> None:
    raw = json.dumps({"stdout": numbered("line", 100)})

    transformed = transform_tool_result(
        raw,
        args={"command": "pytest"},
        noisegate_max_chars=120,
    )

    payload = parse_hook_result(transformed)
    stdout = payload["stdout"]
    assert isinstance(stdout, str)
    assert "[noisegate: omitted" in stdout
    assert payload["noisegate"]["fields"]["stdout"]["reducer"] == "pytest"


def test_transform_tool_result_keeps_blank_tool_name_non_terminal_payload() -> None:
    raw = json.dumps({"content": numbered("exact", 100)})

    assert transform_tool_result(raw, noisegate_max_chars=120) is None


def test_transform_tool_result_keeps_ambiguous_blank_tool_payloads_exact() -> None:
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
        assert transform_tool_result(json.dumps(payload), noisegate_max_chars=120) is None


def test_process_failed_status_without_numeric_code_does_not_fabricate_exit_notice() -> None:
    raw = json.dumps(
        {
            "action": "wait",
            "status": "failed",
            "output": f'{numbered("line", 100)}\nERROR worker failed',
        }
    )

    transformed = transform_tool_result(raw, tool_name="process", noisegate_max_chars=120)

    payload = parse_hook_result(transformed)
    output = payload["output"]
    assert isinstance(output, str)
    assert "[noisegate: exit_code=" not in output
    assert payload["noisegate"]["fields"]["output"].get("exit_code") is None


def test_transform_tool_result_uses_command_alias_when_command_is_blank() -> None:
    raw = json.dumps({"command": "", "cmd": "cat important.txt", "stdout": numbered("exact", 100)})

    assert transform_tool_result(raw, tool_name="terminal", noisegate_max_chars=120) is None


def test_transform_tool_result_uses_top_level_argv_for_command_intent() -> None:
    raw = json.dumps({"argv": ["cat", "important.txt"], "stdout": numbered("exact", 100)})

    assert transform_tool_result(raw, tool_name="terminal", noisegate_max_chars=120) is None


def test_transform_tool_result_preserves_existing_noisegate_key() -> None:
    raw = json.dumps({"noisegate": {"tool": "data"}, "stdout": numbered("line", 100)})

    transformed = transform_tool_result(raw, tool_name="terminal", noisegate_max_chars=200)

    payload = parse_hook_result(transformed)
    assert payload["noisegate"] == {"tool": "data"}
    assert payload["_noisegate"]["compacted"] is True


def test_transform_tool_result_preserves_existing_noisegate_fallback_key() -> None:
    raw = json.dumps(
        {
            "noisegate": {"tool": "data"},
            "_noisegate": {"prior": "metadata"},
            "stdout": numbered("line", 100),
        }
    )

    transformed = transform_tool_result(raw, tool_name="terminal", noisegate_max_chars=200)

    payload = parse_hook_result(transformed)
    assert payload["noisegate"] == {"tool": "data"}
    assert payload["_noisegate"] == {"prior": "metadata"}
    assert payload["__noisegate"]["compacted"] is True


def test_noisegate_mode_off_returns_none() -> None:
    raw = terminal_result(numbered("stdout", 80))

    assert transform_tool_result(raw, tool_name="terminal", noisegate_mode="off") is None


def test_transform_terminal_output_helper_compacts_plain_text() -> None:
    transformed = transform_terminal_output(
        command="docker build .",
        output=numbered("layer", 100),
        exit_code=0,
        noisegate_max_chars=120,
    )

    assert isinstance(transformed, str)
    assert "[noisegate: omitted" in transformed


def test_transform_terminal_output_accepts_hermes_returncode_kwarg() -> None:
    transformed = transform_terminal_output(
        command="pytest",
        output=numbered("line", 100),
        returncode=7,
        noisegate_max_chars=120,
    )

    assert isinstance(transformed, str)
    assert "[noisegate: exit_code=7]" in transformed


def test_transform_terminal_output_ignores_boolean_exit_hints() -> None:
    calls = (
        lambda: transform_terminal_output(
            command="docker build .",
            output=numbered("layer", 100),
            returncode=True,
            noisegate_max_chars=120,
        ),
        lambda: transform_terminal_output(
            command="docker build .",
            output=numbered("layer", 100),
            exit_code=True,
            noisegate_max_chars=120,
        ),
        lambda: transform_terminal_output(
            "docker build .",
            numbered("layer", 100),
            True,
            noisegate_max_chars=120,
        ),
    )

    for call in calls:
        transformed = call()
        assert isinstance(transformed, str)
        assert "[noisegate: omitted" in transformed
        assert "[noisegate: exit_code=" not in transformed


def test_transform_terminal_output_accepts_positional_host_call() -> None:
    transformed = transform_terminal_output(
        "pytest",
        numbered("line", 100),
        7,
        noisegate_max_chars=120,
    )

    assert isinstance(transformed, str)
    assert "[noisegate: exit_code=7]" in transformed


def test_transform_terminal_output_does_not_store_artifacts_before_redaction(
    tmp_path: Path,
) -> None:
    transformed = transform_terminal_output(
        command="env",
        output=numbered("SECRET_TOKEN=value", 1000),
        exit_code=0,
        noisegate_max_chars=1000,
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(tmp_path / "artifacts"),
    )

    assert isinstance(transformed, str)
    assert "[noisegate artifact:" not in transformed
    assert not (tmp_path / "artifacts").exists()
