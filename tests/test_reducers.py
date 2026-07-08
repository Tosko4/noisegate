from __future__ import annotations

import re

import pytest

import noisegate.engine as engine
from noisegate.engine import NoisegateOptions, _first_pattern_match, reduce_text


def numbered(prefix: str, count: int) -> str:
    return "\n".join(f"{prefix} {index:03d}" for index in range(1, count + 1))


def source_like_python() -> str:
    lines = [
        "# Source file that deliberately looks like noisy output.",
        "def render_status() -> dict[str, str]:",
        "    return {",
        "        'FAILED': 'literal test fixture, not a pytest result',",
        "        'ERROR': 'literal config value, not a runtime failure',",
        "        'Traceback': 'literal docs example',",
        "        'npm ERR!': 'literal npm transcript fixture',",
        "        'Dockerfile': 'literal filename fixture',",
        "    }",
    ]
    lines.extend(
        f"# exact source filler {index:03d}: FAILED ERROR Traceback npm ERR!"
        for index in range(80)
    )
    return "\n".join(lines)


def options(**overrides: object) -> NoisegateOptions:
    values: dict[str, object] = {"max_chars": 160, "head_lines": 3, "tail_lines": 2}
    values.update(overrides)
    return NoisegateOptions(**values)  # type: ignore[arg-type]


def test_short_output_is_not_changed() -> None:
    result = reduce_text("one\nshort line\n", command="printf", options=options())

    assert result.changed is False
    assert result.text == "one\nshort line\n"


def test_generic_long_output_uses_deterministic_head_tail() -> None:
    raw = numbered("line", 30)

    result = reduce_text(raw, command="make noisy", options=options())

    assert result.changed is True
    assert result.text.startswith("line 001\nline 002\nline 003")
    assert "[noisegate: omitted 25 lines" in result.text
    assert result.text.endswith("line 029\nline 030")
    assert result.metadata["reducer"] == "generic_head_tail"
    assert result.metadata["original_lines"] == 30
    assert result.metadata["omitted_lines"] > 0
    assert result.metadata["omitted_chars"] > 0


def test_named_non_noisy_tools_are_protected_by_default() -> None:
    raw = numbered("useful context", 100)

    for tool_name in (
        "execute_code",
        "search_files",
        "web_extract",
        "session_search",
        "mcp_github_create_issue",
        "mcp__mindlyos__get_note_page",
        "future_tool",
    ):
        result = reduce_text(raw, tool_name=tool_name, options=options(max_chars=120))
        assert result.changed is False
        assert result.text == raw
        assert result.metadata["reducer"] == "protected_tool"


def test_named_noisy_tools_can_still_reduce() -> None:
    raw = numbered("noisy output", 100)

    for tool_name in ("terminal", "process", "read_terminal", "browser_console"):
        result = reduce_text(raw, tool_name=tool_name, options=options(max_chars=120))
        assert result.changed is True


def test_tiny_char_budget_fails_open_instead_of_emitting_garbage() -> None:
    raw = "A" * 130

    for max_chars in (0, 1, 2, 10, 30):
        result = reduce_text(raw, options=options(max_chars=max_chars))

        assert result.changed is False
        assert result.text == raw
        assert result.metadata["reason"] == "invalid_budget"


def test_char_budget_reduces_only_when_notice_and_content_fit() -> None:
    raw = "A" * 130

    result = reduce_text(raw, options=options(max_chars=40))

    assert result.changed is True
    assert len(result.text) <= 40
    assert result.text.startswith("A")
    assert result.text.endswith("A")
    assert "[noisegate: omitted" in result.text


def test_char_budget_accepts_tight_budget_that_can_fit_notice_and_content() -> None:
    raw = "A" * 100

    result = reduce_text(raw, options=options(max_chars=33))

    assert result.changed is True
    assert result.text == "A\n[noisegate: omitted 98 chars]\nA"
    assert len(result.text) == 33


def test_line_reducer_can_shrink_marker_before_tight_char_budget() -> None:
    raw = "\n".join(f"l{index:02d}" for index in range(100))

    result = reduce_text(
        raw,
        options=options(max_chars=34, max_lines=10, head_lines=1, tail_lines=1),
    )

    assert result.changed is True
    assert result.text == "l\n[noisegate: omitted 34 chars]\n99"
    assert len(result.text) == 34


def test_tiny_line_budget_fails_open_instead_of_dropping_marker() -> None:
    cases = (
        numbered("line", 10),
        "A" * 130,
        ("A" * 80) + "\n" + ("B" * 80),
    )

    for raw in cases:
        for max_lines in (1, 2):
            result = reduce_text(
                raw,
                options=options(
                    max_chars=40,
                    max_lines=max_lines,
                    head_lines=1,
                    tail_lines=1,
                ),
            )

            assert result.changed is False
            assert result.text == raw
            assert result.metadata["reason"] == "invalid_budget"


def test_recovery_notices_do_not_exceed_budget_or_grow_output() -> None:
    raw = "A" * 130

    result = reduce_text(raw, exit_code=1, options=options(max_chars=120))

    assert result.changed is True
    assert len(result.text) <= 120
    assert len(result.text) < len(raw)


def test_important_line_reducer_enforces_line_cap_when_all_lines_match() -> None:
    raw = "\n".join(f"FAILED tests/test_{index}.py::test_x" for index in range(70))

    result = reduce_text(raw, command="pytest", options=options(max_chars=10000, max_lines=50))

    assert result.changed is True
    assert len(result.text.splitlines()) <= 50


def test_important_line_reducer_keeps_middle_failure_when_matches_are_trimmed() -> None:
    lines = [f"tests/test_{index}.py::test_ok PASSED" for index in range(100)]
    lines[50] = "FAILED tests/test_middle.py::test_breaks - AssertionError: boom"
    lines[51] = "E       AssertionError: boom"
    raw = "\n".join(lines)

    result = reduce_text(
        raw,
        command="pytest",
        options=options(max_chars=10000, max_lines=25, max_important_lines=10),
    )

    assert result.changed is True
    assert "test_middle.py::test_breaks" in result.text
    assert "AssertionError: boom" in result.text


def test_important_line_char_cap_keeps_middle_failure() -> None:
    lines = [f"long passing setup line {index} " + ("x" * 80) for index in range(80)]
    lines[40] = "FAILED tests/test_middle.py::test_breaks - AssertionError: boom " + ("y" * 80)
    lines[41] = "E       AssertionError: boom " + ("z" * 80)
    raw = "\n".join(lines)

    result = reduce_text(
        raw,
        command="pytest",
        options=options(max_chars=500, max_lines=25, max_important_lines=20),
    )

    assert result.changed is True
    assert len(result.text) <= 500
    assert "test_middle.py::test_breaks" in result.text
    assert "AssertionError: boom" in result.text


def test_line_budget_prefers_pytest_assertion_detail_over_progress_line() -> None:
    lines = [
        f"tests/test_generated.py::test_pass_before_{index:03d} PASSED [ 20%]"
        for index in range(120)
    ]
    lines.append("tests/test_generated.py::test_signal FAILED [ 50%]")
    lines.extend(
        f"tests/test_generated.py::test_pass_after_{index:03d} PASSED [ 70%]"
        for index in range(120)
    )
    lines.extend(
        [
            "=================================== FAILURES ===================================",
            "_______________________________ test_signal _______________________________",
            "E       AssertionError: DOGFOOD_SIGNAL_SURVIVED",
            "=========================== short test summary info ===========================",
            "FAILED tests/test_generated.py::test_signal - AssertionError: DOGFOOD_SIGNAL_SURVIVED",
        ]
    )
    raw = "\n".join(lines)

    result = reduce_text(
        raw,
        command="uv run pytest -vv tests/test_generated.py",
        options=options(max_chars=700, max_lines=20, max_important_lines=20),
    )

    assert result.changed is True
    assert len(result.text) <= 700
    assert "DOGFOOD_SIGNAL_SURVIVED" in result.text


def test_char_budget_prefers_pytest_assertion_detail_over_progress_line() -> None:
    lines = [
        f"tests/test_generated.py::test_pass_before_{index:03d} PASSED [ 20%]"
        for index in range(20)
    ]
    lines.append("tests/test_generated.py::test_signal FAILED [ 50%]")
    lines.extend(
        f"tests/test_generated.py::test_pass_after_{index:03d} PASSED [ 70%]"
        for index in range(20)
    )
    lines.extend(
        [
            "=================================== FAILURES ===================================",
            "_______________________________ test_signal _______________________________",
            "E       AssertionError: DOGFOOD_SIGNAL_SURVIVED",
            "=========================== short test summary info ===========================",
            "FAILED tests/test_generated.py::test_signal - AssertionError: DOGFOOD_SIGNAL_SURVIVED",
        ]
    )
    raw = "\n".join(lines)

    result = reduce_text(
        raw,
        command="uv run pytest -vv tests/test_generated.py",
        options=options(max_chars=700, max_lines=160, max_important_lines=80),
    )

    assert result.changed is True
    assert len(result.text) <= 700
    assert "DOGFOOD_SIGNAL_SURVIVED" in result.text


def test_char_budget_ignores_passing_test_name_with_exception_substring() -> None:
    lines = [
        "tests/test_widget.py::test_exception_name PASSED " + ("p" * 40)
        for _ in range(8)
    ]
    lines.extend(
        [
            "tests/test_widget.py::test_boom FAILED [100%]",
            "E       RuntimeError: boom",
        ]
    )
    raw = "\n".join(lines)

    result = reduce_text(
        raw,
        command="pytest -vv",
        exit_code=1,
        options=options(max_chars=180, max_lines=80, head_lines=0, tail_lines=0),
    )

    assert result.changed is True
    assert len(result.text) <= 180
    assert "RuntimeError: boom" in result.text
    assert "test_exception_name PASSED" not in result.text


def test_line_budget_prefers_pytest_exception_summary_over_count_summary() -> None:
    raw = "\n".join(
        [
            *[f"tests/test_widget.py::test_ok_{index} PASSED" for index in range(30)],
            "FAILED tests/test_widget.py::test_widget - TypeError: unsupported operand type",
            *[f"noise {index}" for index in range(30)],
            "========================= 1 failed in 0.12s =========================",
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        exit_code=1,
        options=options(
            max_chars=10_000,
            max_lines=3,
            head_lines=0,
            tail_lines=0,
            max_important_lines=10,
            important_context_lines=0,
        ),
    )

    assert result.changed is True
    assert "TypeError: unsupported operand type" in result.text
    assert "1 failed in 0.12s" not in result.text


def test_line_budget_prefers_pytest_progress_failure_over_count_summary() -> None:
    raw = "\n".join(
        [
            *[f"tests/test_widget.py::test_ok_{index} PASSED" for index in range(10)],
            "tests/test_widget.py::test_widget FAILED [100%]",
            *[f"noise {index}" for index in range(10)],
            "========================= 1 failed in 0.12s =========================",
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        exit_code=1,
        options=options(
            max_chars=10_000,
            max_lines=3,
            head_lines=0,
            tail_lines=0,
            max_important_lines=10,
            important_context_lines=0,
        ),
    )

    assert result.changed is True
    assert "tests/test_widget.py::test_widget FAILED" in result.text
    assert "1 failed in 0.12s" not in result.text


def test_char_budget_prefers_real_pytest_failure_over_incidental_exception_log() -> None:
    raw = "\n".join(
        [
            "Exception ignored in: <function _cleanup at 0xabc>",
            *[f"captured log noise {index} " + ("x" * 50) for index in range(10)],
            "tests/test_widget.py::test_widget FAILED [100%]",
            "E       AssertionError: real failure detail",
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        exit_code=1,
        options=options(max_chars=180, max_lines=80, head_lines=0, tail_lines=0),
    )

    assert result.changed is True
    assert "AssertionError: real failure detail" in result.text
    assert "Exception ignored" not in result.text


def test_recovery_notice_survives_when_incidental_exception_name_is_dropped() -> None:
    raw = "\n".join(
        [
            *[
                "tests/test_widget.py::test_exception_name PASSED " + ("p" * 20)
                for _ in range(4)
            ],
            "FAILED tests/test_widget.py::test_widget - TypeError: unsupported operand type",
            *[f"noise {index} " + ("x" * 20) for index in range(6)],
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        exit_code=1,
        options=options(max_chars=170, max_lines=80, head_lines=0, tail_lines=0),
    )

    assert result.changed is True
    assert len(result.text) <= 170
    assert "TypeError: unsupported operand type" in result.text
    assert "test_exception_name PASSED" not in result.text
    assert "[noisegate: exit_code=1]" in result.text


def test_char_budget_falls_back_to_lower_ranked_pytest_line_that_fits() -> None:
    raw = "\n".join(
        [
            "E       AssertionError: " + ("x" * 200),
            *[f"noise {index}" for index in range(10)],
            "tests/test_widget.py::test_widget FAILED",
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        exit_code=1,
        options=options(max_chars=120, max_lines=80, head_lines=0, tail_lines=0),
    )

    assert result.changed is True
    assert len(result.text) <= 120
    assert "tests/test_widget.py::test_widget FAILED" in result.text


def test_char_budget_keeps_trying_anchors_when_capped_context_loses_anchor() -> None:
    raw = "\n".join(
        [
            "E       AssertionError: " + ("x" * 200),
            "captured ERROR local log recovered successfully",
            *[f"noise {index}" for index in range(8)],
            "tests/test_widget.py::test_widget FAILED [100%]",
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        exit_code=1,
        options=options(
            max_chars=135,
            max_lines=80,
            head_lines=0,
            tail_lines=0,
            important_context_lines=2,
        ),
    )

    assert result.changed is True
    assert len(result.text) <= 135
    assert "tests/test_widget.py::test_widget FAILED" in result.text
    assert "captured ERROR local log" not in result.text


def test_line_budget_falls_back_to_lower_ranked_pytest_line_that_fits() -> None:
    raw = "\n".join(
        [
            *[f"setup {index}" for index in range(20)],
            "E       AssertionError: " + ("x" * 200),
            *[f"noise {index}" for index in range(20)],
            "tests/test_widget.py::test_widget FAILED",
            *[f"teardown {index}" for index in range(20)],
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        exit_code=1,
        options=options(
            max_chars=120,
            max_lines=3,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
        ),
    )

    assert result.changed is True
    assert len(result.text) <= 120
    assert len(result.text.splitlines()) <= 3
    assert "tests/test_widget.py::test_widget FAILED" in result.text


def test_char_budget_prefers_pytest_failure_over_benign_error_log() -> None:
    raw = "\n".join(
        [
            "ERROR monitoring task recovered successfully",
            *[f"captured log noise {index} " + ("x" * 20) for index in range(10)],
            "tests/test_widget.py::test_widget FAILED [100%]",
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        exit_code=1,
        options=options(max_chars=120, max_lines=80, head_lines=0, tail_lines=0),
    )

    assert result.changed is True
    assert len(result.text) <= 120
    assert "tests/test_widget.py::test_widget FAILED" in result.text
    assert "ERROR monitoring task recovered successfully" not in result.text


def test_line_budget_prefers_traceback_over_failed_progress_line() -> None:
    raw = "\n".join(
        [
            *[f"setup {index}" for index in range(20)],
            "tests/test_widget.py::test_widget FAILED [100%]",
            *[f"noise {index}" for index in range(20)],
            "Traceback (most recent call last):",
            *[f"teardown {index}" for index in range(20)],
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        exit_code=1,
        options=options(
            max_chars=10_000,
            max_lines=3,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
        ),
    )

    assert result.changed is True
    assert "Traceback (most recent call last):" in result.text
    assert "tests/test_widget.py::test_widget FAILED" not in result.text


def test_line_budget_recognizes_exception_group_as_diagnostic_detail() -> None:
    raw = "\n".join(
        [
            *[f"setup {index}" for index in range(20)],
            "ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)",
            *[f"noise {index}" for index in range(20)],
            "FAILED tests/test_widget.py::test_widget - ExceptionGroup",
            *[f"teardown {index}" for index in range(20)],
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        exit_code=1,
        options=options(
            max_chars=10_000,
            max_lines=3,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
        ),
    )

    assert result.changed is True
    assert "ExceptionGroup: unhandled errors" in result.text
    assert "FAILED tests/test_widget.py::test_widget" not in result.text


def test_line_budget_recognizes_exception_in_header_as_diagnostic_detail() -> None:
    raw = "\n".join(
        [
            *[f"setup {index}" for index in range(20)],
            "Exception in thread worker-1:",
            *[f"noise {index}" for index in range(20)],
            "========================= 1 failed in 0.12s =========================",
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        options=options(
            max_chars=10_000,
            max_lines=3,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
        ),
    )

    assert result.changed is True
    assert "Exception in thread worker-1:" in result.text
    assert "1 failed in 0.12s" not in result.text


def test_line_budget_preserves_unhandled_exception_shutdown_detail() -> None:
    raw = "\n".join(
        [
            *[
                f"tests/test_widget.py::test_exception_name_{index} PASSED"
                for index in range(30)
            ],
            "Unhandled exception during asyncio.run() shutdown",
            "RuntimeError: shutdown boom",
            *[f"noise {index}" for index in range(30)],
            "========================= 1 failed in 0.12s =========================",
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        exit_code=1,
        options=options(
            max_chars=10_000,
            max_lines=3,
            head_lines=0,
            tail_lines=0,
            max_important_lines=10,
            important_context_lines=0,
        ),
    )

    assert result.changed is True
    assert "Unhandled exception during asyncio.run() shutdown" in result.text
    assert "1 failed in 0.12s" not in result.text
    assert "test_exception_name_" not in result.text


def test_line_budget_prefers_pytest_pass_summary_over_progress_line() -> None:
    raw = "\n".join(
        [
            *[
                f"tests/test_widget.py::test_ok_{index} PASSED [ {index}%]"
                for index in range(20)
            ],
            "========================= 40 passed in 1.23s =========================",
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        options=options(
            max_chars=10_000,
            max_lines=3,
            head_lines=0,
            tail_lines=0,
            max_important_lines=10,
            important_context_lines=0,
        ),
    )

    assert result.changed is True
    assert "40 passed in 1.23s" in result.text
    assert "tests/test_widget.py::test_ok_0 PASSED" not in result.text


def test_first_pattern_match_short_circuits_after_first_matching_pattern() -> None:
    match = _first_pattern_match("FIRST then SECOND", (re.compile("FIRST"), re.compile("SECOND")))

    assert match is not None
    assert match.group(0) == "FIRST"


def test_important_line_reducer_handles_high_volume_repeated_failures() -> None:
    raw = "\n".join(
        f"FAILED tests/test_load_{index}.py::test_case - AssertionError: {'x' * 80}"
        for index in range(20_000)
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        options=options(
            max_chars=1_200,
            max_lines=80,
            head_lines=8,
            tail_lines=8,
            max_important_lines=20_000,
        ),
    )

    assert result.changed is True
    assert len(result.text) <= 1_200
    assert result.metadata["reducer"] == "pytest"
    assert "FAILED tests/test_load_" in result.text
    assert "[noisegate: omitted" in result.text


def test_ranked_pattern_matches_carry_line_rank_metadata() -> None:
    matches = engine._ranked_pattern_line_matches(
        "E       AssertionError: dense failure\n",
        engine.CRITICAL_PATTERNS,
    )

    assert matches
    assert matches[0].line_index == 0
    assert engine._rank_for_span_match(
        matches[0],
        engine._LineLayout(lines=[], offsets=[]),
    ) == 0


def test_terminal_file_read_commands_are_protected_by_default() -> None:
    raw = numbered("file line", 100)

    commands = (
        "cat important.txt",
        "sed -n '1,200p' file",
        "head -200 file",
        "tail -200 file",
    )
    for command in commands:
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            options=options(max_chars=120),
        )
        assert result.changed is False
        assert result.text == raw
        assert result.metadata["reducer"] == "protected_file_read"


def test_source_like_terminal_file_display_commands_are_protected() -> None:
    raw = source_like_python()

    commands = (
        "cat -- src/example.py",
        "head -n 200 src/example.py",
        "tail -n +1 src/example.py",
        "sed -n -e '1,200p' src/example.py",
        "cat 'src/A&B.py'",
        "cat 'src/A>B.py'",
        "nl -ba src/example.py",
        "nl -ba < src/example.py",
        "bat --paging=never src/example.py",
        "cat < src/example.py",
        "sed -n '1,200p' < src/example.py",
        "jq . < config.json",
        "cat src/example.py 2>&1",
        "head -200 src/example.py 2>&1",
        "tail -200 src/example.py 2>&1",
        "nl -ba src/example.py 2>&1",
        "sed -n '1,200p' src/example.py 2>&1",
        "jq . config.json 2>&1",
    )
    for command in commands:
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            options=options(max_chars=220, max_lines=20),
        )
        assert result.changed is False, command
        assert result.text == raw
        assert result.metadata["reducer"] == "protected_file_read"


def test_shell_substitution_file_display_commands_are_not_file_read() -> None:
    raw = source_like_python()

    for command in (
        "cat $(pytest -q)",
        "cat `pytest -q`",
        "cat \"$(pytest -q)\"",
        "cat < $(pytest -q)",
        "cat source.py > generated.py",
        "cat $(pytest -q) && echo done",
        "cat source.py > generated.py && pytest -q",
        "pytest -q && cat source.py > generated.py",
        "bash -c 'cat source.py' > generated.py && pytest -q",
        "bash -lc 'cat source.py' > generated.py && pytest -q",
        "env -S bash -c 'cat source.py' > generated.py && pytest -q",
        "bash -c 'cat source.py' $(pytest -q)",
        "bash -c 'cat source.py' `pytest -q`",
    ):
        assert engine.classify_command(command, raw) not in {"file_read", "source_mixed"}, command


def test_multiline_source_reader_snippets_are_preserved_exactly() -> None:
    raw = source_like_python()

    for command in (
        "cat file.py\npytest -q",
        "nl -ba src/foo.py\npytest -q",
        "pytest -q\ncat file.py",
        "pytest -q\r\ncat file.py",
        "echo hi\ncat file.py",
        "bash -lc 'cat source.py' && pytest -q",
        "nl -ba file.py | sed -n '1,80p'",
        "cat file.py | head -80",
        "jq . package.json | head -20",
    ):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            options=options(max_chars=220, max_lines=20),
        )

        assert result.changed is False, command
        assert result.text == raw
        assert result.metadata["command_class"] == "source_mixed"
        assert result.metadata["reducer"] == "protected_file_read"


def test_multiline_test_intent_wins_over_inventory_and_git_prefixes() -> None:
    raw = "\n".join(
        [
            *[f"status noise {index}" for index in range(40)],
            "FAILED tests/test_demo.py::test_breaks",
            "AssertionError: important signal",
            *[f"tail noise {index}" for index in range(40)],
        ]
    )

    for command in (
        "git status\npytest -q",
        "git log --oneline\npytest -q",
        "rg --files\npytest -q",
        "ls -la\npytest -q",
        "pytest -q\nrg --files",
    ):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=options(max_chars=220, max_lines=14),
        )

        assert result.changed is True, command
        assert result.metadata["command_class"] == "pytest"
        assert result.metadata["reducer"] == "pytest"
        assert "FAILED tests/test_demo.py::test_breaks" in result.text
        assert "AssertionError: important signal" in result.text


def test_package_intent_wins_over_test_and_git_prefixes() -> None:
    raw = "\n".join(
        [
            *[f"install noise {index}" for index in range(40)],
            "E: Unable to locate package missing-pkg",
            "AssertionError: important signal",
            *[f"tail noise {index}" for index in range(40)],
        ]
    )

    for command, command_class in (
        ("apt install missing-pkg && pytest -q", "os_package"),
        ("git status && apt install missing-pkg", "os_package"),
        ("uv sync && pytest -q", "dependency_install"),
        ("npm install && pytest -q", "dependency_install"),
        ("git log --oneline && npm install", "dependency_install"),
    ):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=options(max_chars=260, max_lines=14),
        )

        assert result.changed is True, command
        assert result.metadata["command_class"] == command_class
        assert "E: Unable to locate package missing-pkg" in result.text


def test_sed_search_scripts_are_not_file_read_passthroughs() -> None:
    raw = source_like_python()

    assert engine.classify_command("sed -n '/ERROR/p' build.log", raw) != "file_read"


def test_git_show_line_range_option_is_not_file_read_passthrough() -> None:
    raw = source_like_python()

    assert engine.classify_command("git show -L 1,10:src/foo.py HEAD", raw) != "file_read"


def test_v4a_patch_snippet_inside_noisy_log_is_not_patch_passthrough() -> None:
    raw = "\n".join(
        [
            "FAILED tests/test_patch_docs.py::test_example",
            "Here is an example patch snippet in the failure output:",
            "*** Begin Patch",
            "*** Update File: src/example.py",
            "+def fixture():",
            *[f"noisy failure filler {index:03d} FAILED ERROR Traceback" for index in range(90)],
            "*** End Patch",
            "========================= 1 failed in 1.23s =========================",
        ]
    )

    result = reduce_text(raw, command="pytest -q", options=options(max_chars=280, max_lines=20))

    assert result.changed is True
    assert result.metadata["command_class"] == "pytest"
    assert result.metadata["reducer"] != "protected_patch"


def test_git_show_path_source_read_is_protected() -> None:
    raw = source_like_python()

    for command in (
        "git show HEAD:src/example.py",
        "git show 1234:src/example.py",
        "git show :src/example.py",
    ):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            options=options(max_chars=220, max_lines=20, preserve_diffs=False),
        )

        assert result.changed is False, command
        assert result.text == raw
        assert result.metadata["reducer"] == "protected_file_read"


def test_git_diff_is_protected_by_default() -> None:
    raw = "\n".join(
        [
            "diff --git a/app.py b/app.py",
            "index 1111111..2222222 100644",
            "--- a/app.py",
            "+++ b/app.py",
            "@@ -1,3 +1,3 @@",
            "-old exact content",
            "+new exact content",
            *[f"+generated diff line {index}" for index in range(80)],
        ]
    )

    result = reduce_text(raw, command="git diff -- app.py", options=options())

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reducer"] == "protected_diff"


def test_v4a_patch_payload_with_failure_like_text_is_protected_by_default() -> None:
    raw = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: src/failure_fixture.py",
            "+def fixture():",
            "+    return 'FAILED ERROR Traceback npm ERR! Dockerfile'",
            *[f"+source patch filler {index:03d} FAILED ERROR Traceback" for index in range(100)],
            "*** End Patch",
        ]
    )

    result = reduce_text(raw, command="", options=options(max_chars=400, max_lines=30))

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reducer"] == "protected_patch"


def test_v4a_patch_payload_stays_exact_when_diff_passthrough_is_disabled() -> None:
    raw = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: src/failure_fixture.py",
            "+def fixture():",
            "+    return 'FAILED ERROR Traceback npm ERR! Dockerfile'",
            *[f"+source patch filler {index:03d} FAILED ERROR Traceback" for index in range(100)],
            "*** End Patch",
        ]
    )

    result = reduce_text(
        raw,
        command="",
        options=options(max_chars=400, max_lines=30, preserve_diffs=False),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reducer"] == "protected_patch"


def test_v4a_patch_containing_diff_stays_exact_when_diff_passthrough_is_disabled() -> None:
    raw = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: src/example.py",
            "+diff --git a/src/example.py b/src/example.py",
            "+--- a/src/example.py",
            "++++ b/src/example.py",
            "+@@ -1,2 +1,2 @@",
            *[f"+source patch filler {index:03d} FAILED ERROR Traceback" for index in range(100)],
            "*** End Patch",
        ]
    )

    result = reduce_text(
        raw,
        command="",
        options=options(max_chars=400, max_lines=30, preserve_diffs=False),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reducer"] == "protected_patch"


def test_v4a_patch_from_diff_like_command_stays_exact_when_diff_passthrough_is_disabled() -> None:
    raw = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: src/example.py",
            "+def fixture():",
            *[f"+source patch filler {index:03d} FAILED ERROR Traceback" for index in range(100)],
            "*** End Patch",
        ]
    )

    result = reduce_text(
        raw,
        command="git show --patch HEAD",
        options=options(max_chars=400, max_lines=30, preserve_diffs=False),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reducer"] == "protected_patch"


def test_file_read_patch_payload_stays_exact_when_diff_passthrough_is_disabled() -> None:
    raw = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: src/failure_fixture.py",
            "+def fixture():",
            "+    return 'FAILED ERROR Traceback npm ERR! Dockerfile'",
            *[f"+source patch filler {index:03d} FAILED ERROR Traceback" for index in range(100)],
            "*** End Patch",
        ]
    )

    result = reduce_text(
        raw,
        command="cat patches/source.patch",
        tool_name="terminal",
        options=options(max_chars=400, max_lines=30, preserve_diffs=False),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reducer"] == "protected_file_read"


def test_git_log_patch_output_is_protected_by_default() -> None:
    raw = "\n".join(
        [
            "commit abc123",
            "Author: Nabu <nabu@example.invalid>",
            "Date: today",
            "",
            "    fix the thing",
            "",
            "diff --git a/app.py b/app.py",
            "index 1111111..2222222 100644",
            "--- a/app.py",
            "+++ b/app.py",
            "@@ -1,3 +1,3 @@",
            "-old exact content",
            "+new exact content",
            *[f"+generated diff line {index}" for index in range(80)],
        ]
    )

    result = reduce_text(raw, command="git log -p -- app.py", options=options())

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reducer"] == "protected_diff"


def test_git_log_patch_output_is_protected_even_when_diff_marker_is_late() -> None:
    raw = "\n".join(
        [
            "commit abc123",
            "A" * 5_000,
            "diff --git a/app.py b/app.py",
            "index 1111111..2222222 100644",
            "--- a/app.py",
            "+++ b/app.py",
            "@@ -1,3 +1,3 @@",
            "-old exact content",
            "+new exact content",
            *[f"+generated diff line {index}" for index in range(80)],
        ]
    )

    result = reduce_text(raw, command="git -C repo log --patch -- app.py", options=options())

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reducer"] == "protected_diff"


def test_diff_output_is_protected_even_without_command_and_late_marker() -> None:
    raw = "\n".join(
        [
            "A" * 5_000,
            "diff --git a/app.py b/app.py",
            "index 1111111..2222222 100644",
            "--- a/app.py",
            "+++ b/app.py",
            "@@ -1,3 +1,3 @@",
            "-old exact content",
            "+new exact content",
            *[f"+generated diff line {index}" for index in range(80)],
        ]
    )

    result = reduce_text(raw, command="", options=options())

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reducer"] == "protected_diff"


def test_unified_diff_output_is_protected_by_default() -> None:
    raw = "\n".join(
        [
            "--- old.txt",
            "+++ new.txt",
            "@@ -1,3 +1,3 @@",
            "-old exact content",
            "+new exact content",
            *[f"+generated diff line {index}" for index in range(80)],
        ]
    )

    result = reduce_text(raw, command="", options=options())

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reducer"] == "protected_diff"


def test_late_unified_diff_output_is_protected_by_default() -> None:
    raw = (
        "A" * 25_000
        + "\n--- old.txt\n+++ new.txt\n@@ -1,3 +1,3 @@\n-old exact content\n+new exact content\n"
        + "\n".join(f"+generated diff line {index}" for index in range(80))
    )

    result = reduce_text(raw, command="", options=options(max_chars=1000))

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reducer"] == "protected_diff"


def test_pytest_reducer_preserves_failure_context() -> None:
    raw = "\n".join(
        [
            "============================= test session starts =============================",
            *[f"tests/test_many.py::{index} PASSED" for index in range(40)],
            "=================================== FAILURES ===================================",
            "____________________________ test_writes_config ____________________________",
            "E       AssertionError: expected private mode",
            "tests/test_artifacts.py:37: AssertionError",
            *[f"noise line {index}" for index in range(40)],
            "FAILED tests/test_artifacts.py::test_writes_config - AssertionError",
            "========================= 1 failed, 82 passed in 4.21s =========================",
        ]
    )

    result = reduce_text(
        raw,
        command="uv run pytest",
        exit_code=1,
        options=options(max_chars=1_200),
    )

    assert result.changed is True
    assert result.metadata["reducer"] == "pytest"
    assert "AssertionError: expected private mode" in result.text
    assert "FAILED tests/test_artifacts.py::test_writes_config" in result.text
    assert "1 failed, 82 passed" in result.text
    assert "[noisegate: omitted" in result.text


def test_search_reducer_keeps_first_and_last_matches() -> None:
    raw = "\n".join(f"src/file_{index}.py:match {index}" for index in range(1, 40))

    result = reduce_text(raw, command="rg match src", options=options())

    assert result.changed is True
    assert result.metadata["reducer"] == "search"
    assert "src/file_1.py:match 1" in result.text
    assert "src/file_39.py:match 39" in result.text


def test_bypass_marker_leaves_text_unchanged() -> None:
    raw = "NOISEGATE_BYPASS\n" + numbered("line", 50)

    result = reduce_text(raw, command="pytest", options=options())

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reducer"] == "bypass"


def test_disable_env_leaves_text_unchanged(monkeypatch) -> None:
    monkeypatch.setenv("NOISEGATE_DISABLE", "1")
    raw = numbered("line", 50)

    result = reduce_text(raw, command="pytest", options=NoisegateOptions.from_env())

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reducer"] == "disabled"


def test_bypass_env_leaves_text_unchanged(monkeypatch) -> None:
    monkeypatch.setenv("NOISEGATE_BYPASS", "1")
    raw = numbered("line", 50)

    result = reduce_text(raw, command="pytest", options=NoisegateOptions.from_env())

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reducer"] == "disabled"


def test_env_flag_parser_rejects_falsey_disable(monkeypatch) -> None:
    monkeypatch.setenv("NOISEGATE_DISABLE", "0")
    raw = numbered("line", 50)

    result = reduce_text(raw, command="pytest", options=NoisegateOptions.from_env(max_chars=160))

    assert result.changed is True


def test_artifact_size_cap_env_rejects_negative_and_invalid_values(monkeypatch) -> None:
    monkeypatch.setenv("NOISEGATE_ARTIFACT_SIZE_CAP", "-1")
    assert NoisegateOptions.from_env().artifact_size_cap == 1_000_000

    monkeypatch.setenv("NOISEGATE_ARTIFACT_SIZE_CAP", "not-an-int")
    assert NoisegateOptions.from_env().artifact_size_cap == 1_000_000

    monkeypatch.setenv("NOISEGATE_ARTIFACT_SIZE_CAP", "123")
    assert NoisegateOptions.from_env().artifact_size_cap == 123


def test_env_diagnostics_warns_on_invalid_raw_and_bypass_values() -> None:
    diagnostics = engine.env_diagnostics(
        {
            "NOISEGATE_RAW": "maybe",
            "NOISEGATE_BYPASS": "not-sure",
        }
    )

    assert any("NOISEGATE_RAW='maybe' is not recognized" in item for item in diagnostics)
    assert any("NOISEGATE_BYPASS='not-sure' is not recognized" in item for item in diagnostics)


def test_reducers_enforce_max_chars_after_line_selection() -> None:
    raw = "\n".join("A" * 2_000 for _ in range(50))
    opts = NoisegateOptions(max_chars=4_000, max_lines=160, head_lines=24, tail_lines=16)

    result = reduce_text(raw, command="make noisy", options=opts)

    assert result.changed is True
    assert len(result.text) <= 4_000
    assert "[noisegate: omitted" in result.text


def test_char_reducer_counts_omission_marker_in_budget() -> None:
    raw = "A" * 10_000
    opts = NoisegateOptions(max_chars=120)

    result = reduce_text(raw, command="make noisy", options=opts)

    assert result.changed is True
    assert len(result.text) <= 120
    assert "[noisegate: omitted" in result.text


def test_reducers_enforce_max_lines_when_output_is_short_but_tall() -> None:
    raw = numbered("line", 30)
    opts = NoisegateOptions(max_chars=4_000, max_lines=10, head_lines=24, tail_lines=16)

    result = reduce_text(raw, command="make noisy", options=opts)

    assert result.changed is True
    assert result.metadata["reducer"] == "generic_head_tail"
    assert result.text.count("\n") + 1 <= 10
    assert "[noisegate: omitted" in result.text


def test_line_reducer_fails_open_when_marker_digits_make_result_over_char_budget() -> None:
    raw = "\n".join(f"l{index:02d}" for index in range(20))

    result = reduce_text(
        raw,
        options=options(max_chars=32, max_lines=10, head_lines=2, tail_lines=2),
    )

    assert result.changed is False
    assert result.text == raw


def test_important_pattern_tight_budget_fails_open_when_match_line_cannot_fit() -> None:
    raw = "\n".join(
        [
            "setup noise " + ("x" * 80),
            "more setup noise " + ("y" * 80),
            "FAILED tests/test_middle.py::test_breaks - AssertionError: boom",
            "post noise " + ("z" * 80),
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        options=options(max_chars=40, max_lines=10, head_lines=1, tail_lines=1),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reason"] == "reducer_no_output"
    assert result.metadata["attempted_reducer"] == "pytest"


def test_important_pattern_focus_does_not_split_full_match() -> None:
    raw = "\n".join(
        [
            "setup noise " + ("x" * 200),
            "FAILED tests/test_middle.py::test_breaks - AssertionError: boom",
            "post noise " + ("z" * 200),
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        options=options(max_chars=130, max_lines=10, head_lines=1, tail_lines=1),
    )

    assert result.changed is True
    assert len(result.text) <= 130
    assert "FAILED" in result.text
    assert "FAILE\n" not in result.text


def test_centered_failure_excerpt_keeps_omission_markers_when_they_fit() -> None:
    raw = "\n".join(
        [
            "setup noise " + ("x" * 80),
            "FAILED short_case",
            "post noise " + ("z" * 80),
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        options=options(max_chars=100, max_lines=10, head_lines=1, tail_lines=1),
    )

    assert result.changed is True
    assert len(result.text) <= 100
    assert "FAILED short_case" in result.text
    assert "[noisegate: omitted" in result.text


def test_recovery_notices_fail_open_when_important_line_cannot_fit() -> None:
    raw = "\n".join(
        [
            "setup noise " + ("x" * 80),
            "FAILED tests/test_middle.py::test_breaks - AssertionError: boom",
            "post noise " + ("z" * 80),
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        exit_code=1,
        options=options(max_chars=40, max_lines=10, head_lines=1, tail_lines=1),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reason"] == "reducer_no_output"


def test_artifact_enabled_failure_line_that_cannot_fit_fails_open() -> None:
    raw = "\n".join(
        [
            "setup " + ("x" * 80),
            "FAILED tests/test_middle.py::test_breaks - AssertionError: boom",
            "post " + ("z" * 80),
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        exit_code=1,
        options=options(
            max_chars=62,
            max_lines=10,
            head_lines=1,
            tail_lines=1,
            artifact_enabled=True,
        ),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reason"] == "reducer_no_output"


def test_important_line_reducer_tight_line_cap_preserves_middle_failure() -> None:
    raw = "\n".join(
        [
            "setup 1",
            "setup 2",
            "setup 3",
            "FAILED tests/test_middle.py::test_breaks - AssertionError: boom",
            "E       AssertionError: boom",
            "teardown 1",
            "teardown 2",
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        options=options(
            max_chars=10_000,
            max_lines=4,
            head_lines=2,
            tail_lines=2,
            important_context_lines=2,
        ),
    )

    assert result.changed is True
    assert len(result.text.splitlines()) <= 4
    assert "FAILED" in result.text or "AssertionError" in result.text


def test_final_budget_enforcement_preserves_middle_failure_under_line_cap() -> None:
    lines = [f"setup {index}" for index in range(20)]
    lines += [
        "FAILED tests/test_middle.py::test_breaks - AssertionError: boom",
        "E       AssertionError: boom",
    ]
    lines += [f"teardown {index}" for index in range(20)]
    raw = "\n".join(lines)

    result = reduce_text(
        raw,
        command="pytest -q",
        options=options(
            max_chars=10_000,
            max_lines=5,
            head_lines=3,
            tail_lines=3,
            important_context_lines=2,
        ),
    )

    assert result.changed is True
    assert len(result.text.splitlines()) <= 5
    assert "FAILED" in result.text or "AssertionError" in result.text


def test_recovery_notices_do_not_emit_partial_important_markers() -> None:
    lines = [f"setup {index} " + ("x" * 10) for index in range(20)]
    lines += [
        "FAILED tests/test_middle.py::test_breaks - AssertionError: boom",
        "E       AssertionError: boom",
    ]
    lines += [f"teardown {index} " + ("z" * 10) for index in range(20)]
    raw = "\n".join(lines)

    result = reduce_text(
        raw,
        command="pytest -q",
        exit_code=1,
        options=options(
            max_chars=180,
            max_lines=20,
            head_lines=3,
            tail_lines=3,
            important_context_lines=2,
        ),
    )

    assert result.changed is True
    assert len(result.text) <= 180
    assert len(result.text.splitlines()) <= 20
    assert "FAILED" in result.text or "AssertionError" in result.text
    assert "\nomitted after important output]" not in result.text


def test_tight_recovery_notice_is_dropped_instead_of_failure_line() -> None:
    raw = "\n".join(
        [
            *[f"setup {index} " + ("x" * 20) for index in range(20)],
            "FAILED tests/test_middle.py::test_breaks - AssertionError: boom",
            *[f"teardown {index} " + ("z" * 20) for index in range(20)],
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        exit_code=1,
        options=options(
            max_chars=80,
            max_lines=3,
            head_lines=1,
            tail_lines=1,
            important_context_lines=1,
        ),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reason"] == "reducer_no_output"


def test_tight_important_excerpt_does_not_slice_omission_marker() -> None:
    raw = "\n".join(
        [
            *[f"setup {index} " + ("x" * 20) for index in range(20)],
            "FAILED tests/test_middle.py::test_breaks - AssertionError: boom",
            *[f"teardown {index} " + ("z" * 20) for index in range(20)],
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        exit_code=1,
        options=options(
            max_chars=35,
            max_lines=3,
            head_lines=1,
            tail_lines=1,
            important_context_lines=1,
        ),
    )

    assert result.changed is False or len(result.text) <= 35
    assert "ted 20 lines]" not in result.text
    assert "itted 20 lines]" not in result.text
    assert "mitted 20 lines]" not in result.text
    assert "tted 20 lines]" not in result.text
    if result.changed:
        assert "FAILED" in result.text


def test_tight_important_excerpt_does_not_slice_marker_suffix_lines() -> None:
    raw = "\n".join(
        [
            *[f"setup {index} " + ("x" * 20) for index in range(20)],
            "FAILED tests/test_middle.py::test_breaks - AssertionError: boom",
            *[f"teardown {index} " + ("z" * 20) for index in range(20)],
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        exit_code=1,
        options=options(
            max_chars=80,
            max_lines=4,
            head_lines=1,
            tail_lines=1,
            important_context_lines=1,
        ),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reason"] == "reducer_no_output"


def test_tight_important_excerpt_does_not_slice_numeric_marker_suffixes() -> None:
    raw = "\n".join(
        [
            *[f"setup {index} " + ("x" * 20) for index in range(30)],
            "FAILED tests/test_middle.py::test_breaks - AssertionError: boom",
            *[f"teardown {index} " + ("z" * 20) for index in range(30)],
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        exit_code=1,
        options=options(
            max_chars=84,
            max_lines=5,
            head_lines=1,
            tail_lines=1,
            important_context_lines=1,
        ),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reason"] == "reducer_no_output"


def test_char_budget_fails_open_instead_of_count_only_pytest_summary() -> None:
    raw = "\n".join(
        [
            *[f"setup {index} " + ("x" * 20) for index in range(10)],
            "E       AssertionError: " + ("x" * 90),
            *[f"noise {index} " + ("y" * 20) for index in range(10)],
            "========================= 1 failed in 0.12s =========================",
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        options=options(max_chars=100, max_lines=80, head_lines=0, tail_lines=0),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reason"] == "reducer_no_output"
    assert result.metadata["attempted_reducer"] == "pytest"


def test_node_reducer_tight_budget_fails_open_when_node_error_line_cannot_fit() -> None:
    raw = "\n".join(
        [
            "setup " + ("x" * 100),
            "npm ERR! lifecycle script crashed " + ("z" * 100),
            "tail " + ("q" * 100),
        ]
    )

    result = reduce_text(
        raw,
        command="npm test",
        exit_code=1,
        options=options(max_chars=80, max_lines=10, head_lines=1, tail_lines=1),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reason"] == "reducer_no_output"


def test_node_char_budget_preserves_python_exception_over_warning() -> None:
    raw = "\n".join(
        [
            "warning package.json: deprecated dependency",
            *[f"build noise {index} " + ("x" * 20) for index in range(12)],
            "Exception: plugin crashed",
        ]
    )

    result = reduce_text(
        raw,
        command="npm test",
        exit_code=1,
        options=options(max_chars=120, max_lines=80, head_lines=0, tail_lines=0),
    )

    assert result.changed is True
    assert len(result.text) <= 120
    assert "Exception: plugin crashed" in result.text
    assert "deprecated dependency" not in result.text


def test_node_recovery_notice_budget_preserves_python_exception_line() -> None:
    raw = "\n".join(
        [
            "warning package.json: deprecated dependency",
            *[f"build noise {index} " + ("x" * 20) for index in range(8)],
            "Exception: plugin crashed",
            *[f"tail noise {index} " + ("y" * 20) for index in range(8)],
        ]
    )

    for max_chars in (120, 130, 140, 170, 200):
        result = reduce_text(
            raw,
            command="npm test",
            exit_code=1,
            options=options(max_chars=max_chars, max_lines=80, head_lines=0, tail_lines=0),
        )

        if result.changed:
            assert "Exception: plugin crashed" in result.text
            assert "[noisegate: exit_code=1]" in result.text
        else:
            assert result.text == raw
            assert result.metadata["reason"] == "no_gain"


def test_node_char_budget_fails_open_instead_of_warning_when_error_cannot_fit() -> None:
    raw = "\n".join(
        [
            "WARNING deprecated package",
            *[f"build noise {index} " + ("x" * 20) for index in range(10)],
            "Error: central failure detail " + ("z" * 40),
            *[f"tail noise {index} " + ("y" * 20) for index in range(10)],
        ]
    )

    result = reduce_text(
        raw,
        command="npm test",
        exit_code=1,
        options=options(max_chars=80, max_lines=80, head_lines=0, tail_lines=0),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reason"] == "reducer_no_output"
    assert result.metadata["attempted_reducer"] == "node"


def test_node_line_budget_prefers_error_detail_over_count_summary() -> None:
    raw = "\n".join(
        [
            *[f"setup {index}" for index in range(30)],
            "Error: Cannot find module './missing'",
            *[f"build noise {index}" for index in range(30)],
            "3 failed",
        ]
    )

    result = reduce_text(
        raw,
        command="npm test",
        exit_code=1,
        options=options(
            max_chars=10_000,
            max_lines=3,
            head_lines=0,
            tail_lines=0,
            max_important_lines=10,
            important_context_lines=0,
        ),
    )

    assert result.changed is True
    assert "Cannot find module './missing'" in result.text
    assert "3 failed" not in result.text


def test_node_char_budget_preserves_pattern_priority_when_ranks_tie() -> None:
    raw = "\n".join(
        [
            "warning package.json: deprecated dependency",
            *[f"build noise {index}" for index in range(20)],
            "npm ERR! Cannot find module './missing'",
        ]
    )

    result = reduce_text(
        raw,
        command="npm test",
        exit_code=1,
        options=options(max_chars=120, max_lines=80, head_lines=0, tail_lines=0),
    )

    assert result.changed is True
    assert len(result.text) <= 120
    assert "npm ERR! Cannot find module './missing'" in result.text
    assert "deprecated dependency" not in result.text


def test_node_char_budget_prefers_npm_error_over_count_summary() -> None:
    raw = "\n".join(
        [
            *[f"setup {index} " + ("x" * 20) for index in range(10)],
            "npm ERR! Cannot find module './missing'",
            *[f"build noise {index} " + ("y" * 20) for index in range(10)],
            "3 failed",
        ]
    )

    result = reduce_text(
        raw,
        command="npm test",
        exit_code=1,
        options=options(max_chars=120, max_lines=80, head_lines=0, tail_lines=0),
    )

    assert result.changed is True
    assert len(result.text) <= 120
    assert "npm ERR! Cannot find module './missing'" in result.text
    assert "3 failed" not in result.text


def test_preserving_pattern_char_path_reuses_line_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = "\n".join(
        [
            *("warning " + ("x" * 160) for _ in range(50)),
            "npm ERR! Cannot find module './missing'",
        ]
    )
    calls = 0
    original = engine._line_layout

    def counted_line_layout(text: str) -> engine._LineLayout:
        nonlocal calls
        calls += 1
        return original(text)

    monkeypatch.setattr(engine, "_line_layout", counted_line_layout)

    result = reduce_text(
        raw,
        command="npm test",
        exit_code=1,
        options=options(max_chars=120, max_lines=80, head_lines=0, tail_lines=0),
    )

    assert result.changed is True
    assert "npm ERR! Cannot find module './missing'" in result.text
    assert calls == 1


def test_direct_node_command_preserves_error_line() -> None:
    raw = "\n".join(
        [
            *[f"setup {index}" for index in range(20)],
            "Error: direct node failure",
            "    at test.js:12:3",
            *[f"tail {index}" for index in range(20)],
        ]
    )

    result = reduce_text(
        raw,
        command="node test.js",
        exit_code=1,
        options=options(max_chars=120, max_lines=6, head_lines=1, tail_lines=1),
    )

    assert result.metadata["command_class"] == "node"
    if result.changed:
        assert "Error: direct node failure" in result.text
        assert "[noisegate: omitted" in result.text


def test_vitest_command_preserves_error_line() -> None:
    raw = "\n".join(
        [
            *[f"setup {index}" for index in range(20)],
            "Error: vitest failure",
            "    at src/example.test.ts:4:1",
            *[f"tail {index}" for index in range(20)],
        ]
    )

    result = reduce_text(
        raw,
        command="npx vitest run",
        exit_code=1,
        options=options(max_chars=120, max_lines=6, head_lines=1, tail_lines=1),
    )

    assert result.metadata["command_class"] == "node"
    if result.changed:
        assert "Error: vitest failure" in result.text
        assert "[noisegate: omitted" in result.text


def test_nonrecoverable_artifact_notice_only_does_not_replace_generic_output() -> None:
    raw = "\n".join(f"line {index:03d} " + ("x" * 40) for index in range(30))

    result = reduce_text(
        raw,
        command="some command",
        options=options(
            max_chars=80,
            max_lines=10,
            head_lines=1,
            tail_lines=1,
            artifact_enabled=True,
        ),
    )

    assert result.changed is True
    assert len(result.text) <= 80
    assert "line" in result.text
    assert result.text != "[noisegate artifact: not stored; reason=recovery_notice_too_long]"
    assert result.metadata["artifact"] == {
        "stored": False,
        "reason": "recovery_notice_too_long",
        "size_bytes": len(raw.encode()),
    }


def test_nonrecoverable_artifact_notice_does_not_fragment_preserved_failure() -> None:
    raw = "\n".join(
        [
            "setup " + ("x" * 80),
            "FAILED tests/test_x.py::test_y - AssertionError: boom",
            "tail " + ("z" * 80),
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        exit_code=1,
        options=options(
            max_chars=110,
            max_lines=10,
            head_lines=1,
            tail_lines=1,
            artifact_enabled=True,
        ),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reason"] == "reducer_no_output"


def test_artifact_notice_only_does_not_replace_preserved_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_calls: list[str] = []

    def fake_store(text: str, options: NoisegateOptions) -> dict[str, object]:
        store_calls.append(text)
        return {
            "stored": True,
            "id": "ng_" + ("a" * 24),
            "sha256": "b" * 64,
            "size_bytes": len(text.encode()),
        }

    monkeypatch.setattr(engine, "_store_artifact", fake_store)
    raw = "\n".join(
        [
            "setup noise " + ("x" * 80),
            "FAILED tests/test_middle.py::test_breaks - AssertionError: boom",
            "post noise " + ("z" * 80),
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        options=options(
            max_chars=110,
            max_lines=10,
            head_lines=1,
            tail_lines=1,
            artifact_enabled=True,
        ),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reason"] == "reducer_no_output"
    assert store_calls == []


def test_changed_outputs_always_fit_configured_budgets() -> None:
    cases = [
        numbered("line", 30),
        "\n".join("A" * 80 for _ in range(8)),
        "\n".join(
            [
                "setup " + ("x" * 80),
                "FAILED tests/test_middle.py::test_breaks - AssertionError: boom",
                "post " + ("z" * 80),
            ]
        ),
    ]

    for raw in cases:
        for max_chars in (32, 40, 80, 120):
            for max_lines in (3, 5, 10):
                result = reduce_text(
                    raw,
                    command="pytest -q",
                    exit_code=1,
                    options=options(
                        max_chars=max_chars,
                        max_lines=max_lines,
                        head_lines=1,
                        tail_lines=1,
                    ),
                )
                if result.changed:
                    assert len(result.text) <= max_chars
                    assert len(result.text.splitlines()) <= max_lines


def test_source_read_detection_does_not_match_arguments() -> None:
    raw = numbered("ordinary noisy output", 100)

    commands = (
        ("rg cat src", "search"),
        ("grep head file.txt", "search"),
        ("pytest -k cat", "pytest"),
        ("pip install bat", "dependency_install"),
        ("npm test -- --grep yq", "node"),
        ("/bin/bash -o pipefail -c 'npm test'", "node"),
        ("/usr/bin/node script.js", "node"),
        ("bash -lc '/usr/bin/node script.js'", "node"),
        ("env NODE_ENV=test /usr/local/bin/node script.js", "node"),
    )
    for command, command_class in commands:
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            options=options(max_chars=120),
        )
        assert result.changed is True
        assert result.metadata["command_class"] == command_class
        assert result.metadata["reducer"] != "protected_file_read"

def test_pytest_command_intent_beats_package_argument_words() -> None:
    raw = "\n".join(
        [
            *[f"tests/test_{index}.py ." for index in range(80)],
            "FAILED tests/test_pkg.py::test_yarn",
            "AssertionError: expected 2 got 1",
            *[f"tail {index}" for index in range(80)],
        ]
    )

    commands = (
        "pytest -k yarn",
        "pytest -k 'npm test'",
        "pytest -k apt install",
        "python -m pytest -k yarn",
        "uv run pytest -k yarn",
        "uv run --group dev pytest -k yarn",
        "uv run --with pytest pytest -k yarn",
        "uv run --group dev python -m pytest -k yarn",
        "uv run -m pytest -k yarn",
        "uv --quiet run pytest -k yarn",
        "uv --offline --no-progress run pytest -k yarn",
        "uv --python 3.13 run pytest -k yarn",
        "uv --directory . run python -m pytest -k yarn",
        "python -I -m pytest -k yarn",
        "npx --yes pytest -k yarn",
        "npx -p pytest pytest -k yarn",
        "npm exec pytest -- -k yarn",
        "npm-exec pytest -k yarn",
        "pnpm dlx pytest -k yarn",
        "pnpm-dlx pytest -k yarn",
    )
    for command in commands:
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=options(max_chars=220, max_lines=8, head_lines=1, tail_lines=1),
        )
        assert result.changed is True
        assert result.metadata["command_class"] == "pytest"
        assert result.metadata["reducer"] == "pytest"
        assert "AssertionError: expected 2 got 1" in result.text

def test_hyphenated_pytest_runner_success_output_keeps_pytest_intent() -> None:
    raw = "\n".join([*[f"tests/test_{index}.py ." for index in range(80)], "80 passed in 3.12s"])

    for command in ("npm-exec pytest -q", "pnpm-dlx pytest -q"):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=options(max_chars=160, max_lines=6, head_lines=1, tail_lines=1),
        )

        assert result.changed is True
        assert result.metadata["command_class"] == "pytest"
        assert result.metadata["reducer"] == "pytest"

def test_mixed_source_and_noisy_commands_are_protected_by_default() -> None:
    raw = numbered("mixed source line", 100)

    commands = (
        "cat file.py && pytest",
        "sed -n '1,200p' file.py && npm test",
        "sed -En '1,20p' file.py && pytest",
        "bash -lc \"sed -n '1,20p' file.py && pytest\"",
        "bash -lc \"/bin/cat file.py && pytest\"",
        "pytest -q; /bin/cat file.py",
        "pytest -q && /usr/bin/cat file.py",
        "npm test; /bin/sed -n '1,200p' file.py",
        "find . -type f -exec /bin/cat {} \\;",
        "find . -type f -execdir /bin/cat {} \\;",
        "find . -type f -execdir sed -n '1,20p' {} \\;",
        "/bin/bash -lc 'cat file.py && pytest'",
        "bash -o pipefail -c 'cat file.py && pytest'",
        "bash -euo pipefail -c 'cat file.py && pytest'",
        "bash --noprofile --rcfile /tmp/bashrc -c 'cat file.py && pytest'",
        "bash -c 'cat file.py && pytest'",
        "git diff && pytest",
        "rg 'pattern' . && npm test",
        "/usr/bin/rg 'pattern' . && npm test",
        "/bin/grep -R 'pattern' . && npm test",
        "/opt/bin/ag pattern . && npm test",
        "/opt/bin/ack pattern . && npm test",
        "find . -type f -exec cat {} \\;",
        "/usr/bin/find . -type f -exec /bin/cat {} \\;",
        "find . -type f -exec sh -c 'cat \"$1\"' sh {} \\;",
        "find . -type f -exec bash -c 'sed -n \"1,80p\" \"$1\"' bash {} \\;",
        "fd -x cat",
        "/usr/bin/fd -x /bin/cat",
        "fd --exec sed -n '1,20p'",
        "/usr/bin/fd --exec=/bin/cat",
        "fd --exec-batch cat",
        "/usr/bin/fd --exec-batch=/bin/cat",
        "rg 'pattern' . | head -50",
        "git status && git diff",
        "python - <<'PY'",
        "find . -exec cat {} \\;",
        "rg 'pattern' src | xargs sed -n '1,80p'",
    )
    for command in commands:
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            options=options(max_chars=120),
        )
        assert result.changed is False
        assert result.text == raw
        assert result.metadata["reducer"] in {"protected_file_read", "protected_diff"}
        assert result.metadata["command_class"] in {"source_mixed", "git_diff"}

def test_inventory_commands_can_reduce() -> None:
    raw = numbered("inventory entry", 100)

    commands = (
        "ls -la",
        "find . -maxdepth 3 -type f",
        "/usr/bin/find . -maxdepth 3 -type f",
        "/bin/ls -la",
        "/usr/bin/tree .",
        "/usr/bin/fd",
        "bash -lc 'find . -type f'",
        "git ls-files",
        "/usr/bin/git ls-files",
        "/usr/bin/git -c core.quotepath=false ls-files",
        "rg --files",
        "/usr/bin/rg --files",
        "env FOO=bar find . -type f",
        "/usr/bin/env FOO=bar fd",
        "command ls -la",
        "fd",
        "tree",
    )
    for command in commands:
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            options=options(max_chars=120),
        )
        assert result.changed is True
        assert result.metadata["command_class"] == "inventory"
        assert result.metadata["reducer"] == "inventory"

def test_env_wrapped_inventory_keeps_permission_errors() -> None:
    raw = "\n".join(
        [
            *[f"src/file_{index}.py" for index in range(50)],
            "find: './private': Permission denied",
            *[f"tests/file_{index}.py" for index in range(50)],
        ]
    )

    for command in ("env FOO=bar find . -type f", "/usr/bin/find . -type f"):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=options(max_chars=180, max_lines=10, head_lines=1, tail_lines=1),
        )

        assert result.changed is True
        assert result.metadata["command_class"] == "inventory"
        assert result.metadata["reducer"] == "inventory"
        assert "find: './private': Permission denied" in result.text

def test_inventory_tail_does_not_hide_earlier_test_failures() -> None:
    raw = "\n".join(
        [
            *[f"setup {index}" for index in range(40)],
            "FAILED tests/test_demo.py::test_breaks",
            "AssertionError: inventory tail must not hide this",
            *[f"src/file_{index}.py" for index in range(40)],
        ]
    )

    for command in (
        "pytest -q; find . -type f",
        "pytest -q && rg --files",
        "apt install -y curl && pytest -q",
        "env PYTHONPATH=. pytest -q && find . -type f",
        "npx pytest && find . -type f",
    ):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=options(max_chars=220, max_lines=14, head_lines=2, tail_lines=2),
        )
        assert result.changed is True
        assert result.metadata["command_class"] == "pytest"
        assert result.metadata["reducer"] == "pytest"
        assert "FAILED tests/test_demo.py::test_breaks" in result.text
        assert "AssertionError: inventory tail must not hide this" in result.text

def test_node_wrapped_pytest_output_uses_test_reducer() -> None:
    raw = "\n".join(
        [
            *[f"node wrapper noise {index}" for index in range(50)],
            "=================================== FAILURES ===================================",
            "____________________________ test_wrapped_failure ____________________________",
            "E       AssertionError: wrapped pytest detail",
            "tests/test_wrapped.py:12: AssertionError",
            *[f"tail noise {index}" for index in range(50)],
            "FAILED tests/test_wrapped.py::test_wrapped_failure - AssertionError",
        ]
    )

    result = reduce_text(
        raw,
        command="npm test",
        tool_name="terminal",
        exit_code=1,
        options=options(max_chars=260, max_lines=14, head_lines=1, tail_lines=1),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == "pytest"
    assert result.metadata["reducer"] == "pytest"
    assert "E       AssertionError: wrapped pytest detail" in result.text
    assert "tests/test_wrapped.py:12: AssertionError" in result.text

def test_code_search_commands_can_reduce_cautiously() -> None:
    raw = "\n".join(f"src/file_{index}.py:match {index}" for index in range(1, 60))

    for command in (
        "rg 'pattern' src tests",
        "/usr/bin/rg 'pattern' src tests",
        "grep -R 'pattern' .",
        "/bin/grep -R 'pattern' .",
        "ag pattern",
        "/opt/bin/ag pattern",
        "ack pattern",
        "/opt/bin/ack pattern",
    ):
        result = reduce_text(raw, command=command, tool_name="terminal", options=options())
        assert result.changed is True
        assert result.metadata["command_class"] == "search"
        assert result.metadata["reducer"] == "search"

def test_search_command_intent_beats_log_like_hits() -> None:
    raw = "\n".join(
        f"src/file_{index}.js:{index}: console.log('npm ERR! synthetic {index}')"
        for index in range(1, 80)
    )

    result = reduce_text(
        raw,
        command="/usr/bin/rg 'npm ERR' src",
        tool_name="terminal",
        options=options(max_chars=500, max_lines=12, head_lines=2, tail_lines=2),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == "search"
    assert result.metadata["reducer"] == "search"
    assert "src/file_1.js" in result.text
    assert "src/file_79.js" in result.text

def test_package_manager_option_forms_keep_specialized_reducers() -> None:
    raw = "\n".join(
        [
            *[f"noise {index}" for index in range(40)],
            "ERROR: ResolutionImpossible",
            "No match for argument: missing",
            *[f"tail {index}" for index in range(40)],
        ]
    )

    commands = (
        ("apt-get -y install curl", "os_package"),
        ("/usr/bin/apt-get -y install curl", "os_package"),
        ("apt --yes install curl", "os_package"),
        ("dnf install missing", "os_package"),
        ("/usr/bin/dnf install missing", "os_package"),
        ("apk add missing", "os_package"),
        ("pkg install missing", "os_package"),
        ("/usr/sbin/pkg install missing", "os_package"),
        ("brew install missing", "os_package"),
        ("pip --disable-pip-version-check install foo", "dependency_install"),
        ("/usr/bin/pip --disable-pip-version-check install foo", "dependency_install"),
        ("python -m pip --no-cache-dir install foo", "dependency_install"),
        ("/usr/bin/python3 -m pip --no-cache-dir install foo", "dependency_install"),
        ("uv --directory app sync", "dependency_install"),
        ("pacman -Sy broken-package", "os_package"),
        ("pip --proxy https://proxy.example install foo", "dependency_install"),
        ("bash -lc 'apt-get update'", "os_package"),
        ("bash -lc 'pip install bad'", "dependency_install"),
        ("uv --directory=app sync", "dependency_install"),
        ("npm --prefix web install", "dependency_install"),
        ("npm -f install", "dependency_install"),
        ("npm --loglevel warn install", "dependency_install"),
        ("npm --omit dev install", "dependency_install"),
        ("pnpm --store-dir .pnpm-store install", "dependency_install"),
        ("npm i", "dependency_install"),
        ("pnpm --filter app install", "dependency_install"),
        ("pnpm -C web install", "dependency_install"),
        ("pnpm -F app install", "dependency_install"),
        ("pnpm i", "dependency_install"),
        ("yarn --cwd web install", "dependency_install"),
        ("yarn", "dependency_install"),
    )
    for command, command_class in commands:
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            options=options(max_chars=220, max_lines=10),
        )
        assert result.changed is True
        assert result.metadata["command_class"] == command_class
        assert result.metadata["reducer"] == command_class

def test_os_package_reducer_preserves_unavailable_package_errors() -> None:
    raw = "\n".join(
        [
            *[f"install noise {index}" for index in range(40)],
            "No packages available to install matching 'missing-package'",
            *[f"tail {index}" for index in range(40)],
        ]
    )

    result = reduce_text(
        raw,
        command="pkg install missing-package",
        tool_name="terminal",
        exit_code=1,
        options=options(max_chars=180, max_lines=10, head_lines=1, tail_lines=1),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == "os_package"
    assert result.metadata["reducer"] == "os_package"
    assert "No packages available to install matching" in result.text

def test_package_command_intent_beats_test_like_output() -> None:
    raw = "\n".join(
        [
            *[f"install noise {index}" for index in range(50)],
            "FAILED tests/metadata fixture copied from package docs",
            *[f"middle noise {index}" for index in range(50)],
            "E: Unable to locate package definitely-missing-package",
            *[f"tail noise {index}" for index in range(50)],
        ]
    )

    result = reduce_text(
        raw,
        command="apt-get install definitely-missing-package",
        tool_name="terminal",
        exit_code=100,
        options=options(max_chars=260, max_lines=12, head_lines=1, tail_lines=1),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == "os_package"
    assert result.metadata["reducer"] == "os_package"
    assert "Unable to locate package" in result.text

def test_noisy_maintenance_commands_can_reduce() -> None:
    raw = numbered("install noise", 100)

    commands = (
        ("apt-get update", "os_package"),
        ("apt install curl", "os_package"),
        ("pip install noisegate-hermes", "dependency_install"),
        ("uv sync", "dependency_install"),
        ("uv pip install -e .", "dependency_install"),
        ("npm install", "dependency_install"),
        ("pnpm install", "dependency_install"),
        ("yarn install", "dependency_install"),
        ("docker build .", "docker_build"),
        ("docker compose build", "docker_build"),
    )
    for command, command_class in commands:
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            options=options(max_chars=120),
        )
        assert result.changed is True
        assert result.metadata["command_class"] == command_class

def test_path_qualified_diff_command_is_protected_without_diff_markers() -> None:
    raw = "\n".join(
        f"Files old/file_{index}.py and new/file_{index}.py differ" for index in range(80)
    )

    for command in ("/usr/bin/diff -q old new", "/usr/bin/git diff --name-only"):
        result = reduce_text(raw, command=command, tool_name="terminal", options=options())

        assert result.changed is False
        assert result.text == raw
        assert result.metadata["command_class"] == "git_diff"
        assert result.metadata["reducer"] == "protected_diff"

def test_node_reducer_preserves_common_runtime_error_names() -> None:
    raw = "\n".join(
        [
            *[f"setup {index}" for index in range(30)],
            "TypeError: Cannot read properties of undefined (reading 'id')",
            "    at run (/repo/src/index.js:12:3)",
            "ReferenceError: missingValue is not defined",
            *[f"tail {index}" for index in range(30)],
        ]
    )

    result = reduce_text(
        raw,
        command="/usr/bin/node src/index.js",
        exit_code=1,
        options=options(max_chars=320, max_lines=14, head_lines=2, tail_lines=2),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == "node"
    assert result.metadata["reducer"] == "node"
    assert "TypeError: Cannot read properties" in result.text
    assert "ReferenceError: missingValue" in result.text

def test_node_command_intent_beats_package_words_in_arguments() -> None:
    raw = "\n".join(
        [
            *[f"setup {index}" for index in range(40)],
            "TypeError: Cannot read properties of undefined (reading 'id')",
            "    at run (/repo/src/index.js:12:3)",
            *[f"tail {index}" for index in range(40)],
        ]
    )

    for command in (
        "node script.js --query pip install",
        "npm test -- --grep apt install",
    ):
        result = reduce_text(
            raw,
            command=command,
            exit_code=1,
            options=options(max_chars=260, max_lines=12, head_lines=1, tail_lines=1),
        )

        assert result.changed is True
        assert result.metadata["command_class"] == "node"
        assert result.metadata["reducer"] == "node"
        assert "TypeError: Cannot read properties" in result.text

def _real_agent_lines(prefix: str, count: int) -> list[str]:
    return [f"{prefix} {index:03d}" for index in range(1, count + 1)]


def _with_noise(before: list[str], signal: list[str], after: list[str] | None = None) -> str:
    return "\n".join([*before, *signal, *(after or [])])


@pytest.mark.parametrize(
    ("command", "raw", "expected_reducer"),
    [
        (
            "apt-get update",
            _with_noise(
                _real_agent_lines("Hit: package index mirror", 35),
                ["Reading package lists... Done"],
                _real_agent_lines("Fetched translation metadata", 35),
            ),
            "os_package",
        ),
        (
            "pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_module.py .", 35),
                ["84 passed in 3.42s"],
                _real_agent_lines("tests/test_more.py .", 35),
            ),
            "pytest",
        ),
        (
            "find . -type f",
            _with_noise(
                _real_agent_lines("./src/file", 40),
                ["./pyproject.toml"],
                _real_agent_lines("./tests/test_file", 40),
            ),
            "inventory",
        ),
    ],
)
def test_real_agent_slop_success_outputs_are_strongly_compacted(
    command: str,
    raw: str,
    expected_reducer: str,
) -> None:
    result = reduce_text(
        raw,
        command=command,
        exit_code=0,
        options=options(max_chars=500, max_lines=12, head_lines=2, tail_lines=2),
    )

    assert result.changed is True
    assert result.metadata["reducer"] == expected_reducer
    assert result.metadata["reducer"] != "generic_head_tail"
    assert len(result.text) < len(raw) // 2
    assert len(result.text.splitlines()) <= 12
    assert "[noisegate: omitted" in result.text


@pytest.mark.parametrize(
    ("command", "raw", "expected_reducer", "signals"),
    [
        (
            "apt-get install definitely-missing-package",
            _with_noise(
                _real_agent_lines("install noise", 40),
                ["E: Unable to locate package definitely-missing-package"],
                _real_agent_lines("tail noise", 40),
            ),
            "os_package",
            ("Unable to locate package",),
        ),
        (
            "node src/index.js",
            _with_noise(
                _real_agent_lines("setup", 40),
                ["TypeError: Cannot read properties of undefined (reading 'id')"],
                _real_agent_lines("tail", 40),
            ),
            "node",
            ("TypeError: Cannot read properties",),
        ),
        (
            "find . -type f",
            _with_noise(
                _real_agent_lines("./src/file", 40),
                ["find: './secret': Permission denied"],
                _real_agent_lines("./tests/test_file", 40),
            ),
            "inventory",
            ("Permission denied",),
        ),
    ],
)
def test_real_agent_slop_failures_keep_actionable_error_signals(
    command: str,
    raw: str,
    expected_reducer: str,
    signals: tuple[str, ...],
) -> None:
    result = reduce_text(
        raw,
        command=command,
        exit_code=1,
        options=options(max_chars=650, max_lines=16, head_lines=2, tail_lines=2),
    )

    assert result.changed is True
    assert result.metadata["reducer"] == expected_reducer
    assert result.metadata["reducer"] != "generic_head_tail"
    assert len(result.text) < len(raw) // 2
    assert len(result.text.splitlines()) <= 16
    assert "[noisegate: omitted" in result.text
    for signal in signals:
        assert signal in result.text

def test_noisy_compound_inventory_keeps_noisy_reducer() -> None:
    raw = "\n".join(
        [f"setup line {index}" for index in range(50)]
        + ["FAILED tests/test_app.py::test_app", "AssertionError: boom"]
        + [f"inventory file {index}" for index in range(50)]
    )
    result = reduce_text(
        raw,
        command="pytest -q && ls -R",
        tool_name="terminal",
        exit_code=1,
        options=options(max_chars=180, max_lines=16),
    )
    assert result.changed is True
    assert result.metadata["command_class"] == "pytest"
    assert "FAILED tests/test_app.py::test_app" in result.text
    assert "AssertionError: boom" in result.text
