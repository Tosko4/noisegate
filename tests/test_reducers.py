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
        "/bin/cat important.txt",
        "bash -lc 'cat important.txt'",
        "/bin/bash -lc 'cat important.txt'",
        "env FOO=1 bash -lc 'cat important.txt'",
        "env -i cat important.txt",
        "env --ignore-environment cat important.txt",
        "env -u FOO cat important.txt",
        "env --unset FOO cat important.txt",
        "env -C pkg cat important.txt",
        "env --chdir pkg cat important.txt",
        "env --argv0 display cat important.txt",
        "sudo cat important.txt",
        "sudo -n -u root cat important.txt",
        "sudo -Eu root cat important.txt",
        "sudo -nEu root cat important.txt",
        "sudo -C 3 cat important.txt",
        "sudo -D /tmp cat important.txt",
        "sudo --chdir /tmp cat important.txt",
        "doas cat important.txt",
        "command cat important.txt",
        "command -p cat important.txt",
        "sudo env FOO=1 cat important.txt",
        "doas env FOO=1 cat important.txt",
        "command env FOO=1 cat important.txt",
        "env env FOO=1 cat important.txt",
        "/usr/bin/env FOO=1 bash -lc 'cat important.txt'",
        "/usr/bin/env -i bash -lc 'cat important.txt'",
        "cd pkg && cat important.txt",
        "bash -lc 'cd pkg && cat important.txt'",
        "bash -euo pipefail -c 'cat important.txt'",
        "bash -O extglob -c 'cat important.txt'",
        "bash --norc -c 'cat important.txt'",
        "bash --posix -c 'cat important.txt'",
        "sed -n '1,200p' file",
        "/usr/bin/sed -n '1,200p' file",
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
        "cat src/example.py >&2",
        "cat src/example.py 1>&2",
        "cat src/example.py 2>&1",
        "cat src/example.py 2>/dev/null",
        "cat < src/example.py",
        "cat <src/example.py",
        "cat 0<src/example.py",
        "sed -n '1,200p' < src/example.py",
        "sed -n '1,200p' <src/example.py",
        "sed -n '1,200p' 0<src/example.py",
        "find . -execdir cat {} +",
        "/usr/bin/find . -exec cat {} \\;",
        "find . -exec cat {} + 2>/dev/null",
        "fd TODO . -x cat {}",
        "find . -exec sed -n '1,80p' {} +",
        "find . -execdir sed -n '1,80p' {} +",
        "fd TODO . -x sed -n '1,80p' {}",
        "fd TODO . --exec sed -n '1,80p' {}",
        "nl -ba src/example.py",
        "bat --paging=never src/example.py",
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
        "cat <(pytest -q)",
        "cat < <(pytest -q)",
        "cat <<EOF",
        "cat <<<payload",
        "cat `pytest -q`",
        "cat \"$(pytest -q)\"",
        "bash -lc 'cat $(pytest -q)'",
        "bash --norc -c 'cat $(pytest -q)'",
        "cat 'file\\'; pytest -q",
        "nl -ba src/foo.py\npytest -q",
        "nl -ba src/foo.py\r\npytest -q",
        "bash -lc 'nl -ba src/foo.py\npytest -q'",
        "bash -lc 'cat src/foo.py\npytest -q'",
        "cat important.txt |& pytest -q",
        "bash -lc 'cat important.txt & pytest -q'",
    ):
        assert engine.classify_command(command, raw) != "file_read", command


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


def test_wrapped_git_diff_name_output_is_protected_by_command_intent() -> None:
    raw = "\n".join([f"src/changed_{index:03d}.py" for index in range(120)])

    result = reduce_text(
        raw,
        command="env CI=1 bash -lc 'git diff --name-only'",
        options=options(max_chars=120, max_lines=8),
    )

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


@pytest.mark.parametrize("command", ("npx vitest run", "./node_modules/.bin/vitest run"))
def test_vitest_command_preserves_error_line(command: str) -> None:
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
        command=command,
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


def test_inventory_artifact_notice_preserves_prefixed_error_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_store(text: str, options: NoisegateOptions) -> dict[str, object]:
        return {
            "stored": True,
            "id": "ng_" + ("a" * 24),
            "sha256": "b" * 64,
            "size_bytes": len(text.encode()),
        }

    monkeypatch.setattr(engine, "_store_artifact", fake_store)
    raw = "\n".join(
        [
            *[f"./src/generated/file {index:03d} " + ("x" * 40) for index in range(20)],
            "find: paths must precede expression: `-name'",
            *[f"./tests/generated/file {index:03d} " + ("z" * 40) for index in range(20)],
        ]
    )

    result = reduce_text(
        raw,
        command="find . -type f",
        exit_code=1,
        options=options(
            max_chars=260,
            max_lines=8,
            head_lines=1,
            tail_lines=1,
            artifact_enabled=True,
        ),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == "inventory"
    assert "find: paths must precede expression" in result.text
    assert "[noisegate artifact: id=ng_" in result.text


def _real_agent_lines(prefix: str, count: int) -> list[str]:
    return [f"{prefix} {index:03d}" for index in range(1, count + 1)]


def _with_noise(before: list[str], signal: list[str], after: list[str] | None = None) -> str:
    return "\n".join([*before, *signal, *(after or [])])


def test_fd_exec_commands_are_not_inventory_slop() -> None:
    commands = (
        "fd TODO . -x sh -c 'printf %s {}'",
        "find . -execdir pytest {} +",
    )

    for command in commands:
        assert engine.classify_command(command, "candidate output") == "generic"


def test_search_commands_with_package_terms_stay_search() -> None:
    commands = (
        "rg uv sync README.md",
        "rg apt install README.md",
        "grep pip install README.md",
    )

    for command in commands:
        assert engine.classify_command(command, "README.md: package command example") == "search"


@pytest.mark.parametrize(
    ("command", "raw", "command_class"),
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
            "apt install -y curl",
            _with_noise(
                _real_agent_lines("Selecting package", 35),
                [
                    "0 upgraded, 1 newly installed, 0 to remove and 8 not upgraded",
                    "Setting up curl (8.5.0-2ubuntu10) ...",
                ],
                _real_agent_lines("Processing triggers for man-db", 35),
            ),
            "os_package",
        ),
        (
            "python -m pip install requests",
            _with_noise(
                _real_agent_lines("Collecting transitive dependency", 35),
                [
                    "Successfully installed certifi-2025.1.31 "
                    "charset-normalizer-3.4.1 requests-2.32.3"
                ],
                _real_agent_lines("Using cached wheel", 35),
            ),
            "dependency_install",
        ),
        (
            "uv sync",
            _with_noise(
                _real_agent_lines("Resolved package", 35),
                ["Resolved 86 packages in 14ms", "Installed 86 packages in 120ms"],
                _real_agent_lines(" + dependency", 35),
            ),
            "dependency_install",
        ),
        (
            "uv pip install pytest",
            _with_noise(
                _real_agent_lines("Resolved wheel", 35),
                ["Resolved 8 packages in 4ms", "Installed 8 packages in 11ms"],
                _real_agent_lines(" + pytest dependency", 35),
            ),
            "dependency_install",
        ),
        (
            "npm install",
            _with_noise(
                _real_agent_lines("npm fetch GET 200", 35),
                ["added 184 packages, and audited 185 packages in 7s", "found 0 vulnerabilities"],
                _real_agent_lines("npm timing idealTree", 35),
            ),
            "node",
        ),
        (
            "pnpm install",
            _with_noise(
                _real_agent_lines("Progress: resolved package", 35),
                ["Packages: +184", "Done in 4.2s"],
                _real_agent_lines("Progress: downloaded package", 35),
            ),
            "node",
        ),
        (
            "yarn install",
            _with_noise(
                _real_agent_lines("YN0000: Resolving package", 35),
                ["➤ YN0000: Done with warnings in 3s 221ms"],
                _real_agent_lines("YN0000: Fetch step", 35),
            ),
            "node",
        ),
        (
            "npm test",
            _with_noise(
                _real_agent_lines("PASS packages/pkg/test", 35),
                ["Test Suites: 12 passed, 12 total", "Tests: 248 passed, 248 total"],
                _real_agent_lines("coverage line", 35),
            ),
            "node",
        ),
        (
            "pnpm test",
            _with_noise(
                _real_agent_lines("✓ package tests passed", 35),
                ["Tests: 248 passed, 248 total"],
                _real_agent_lines("coverage line", 35),
            ),
            "node",
        ),
        (
            "yarn test",
            _with_noise(
                _real_agent_lines("YN0000: Running test shard", 35),
                ["Test Suites: 12 passed, 12 total"],
                _real_agent_lines("YN0000: Test output", 35),
            ),
            "node",
        ),
        (
            "docker build .",
            _with_noise(
                _real_agent_lines("#1 CACHED layer", 35),
                ["#18 exporting to image", "#18 DONE 0.7s"],
                _real_agent_lines("#19 naming layer", 35),
            ),
            "docker_build",
        ),
        (
            "docker compose build",
            _with_noise(
                _real_agent_lines("=> CACHED service layer", 35),
                ["=> exporting layers", "=> naming to local/app:latest"],
                _real_agent_lines("=> DONE service layer", 35),
            ),
            "docker_build",
        ),
        (
            "docker compose --profile web --env-file .env --parallel 4 build",
            _with_noise(
                _real_agent_lines("=> CACHED service layer", 35),
                ["=> exporting layers", "=> naming to local/app:latest"],
                _real_agent_lines("=> DONE service layer", 35),
            ),
            "docker_build",
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
            "python -m unittest discover",
            _with_noise(
                _real_agent_lines("test_case ok", 35),
                ["Ran 84 tests in 3.420s", "OK"],
                _real_agent_lines("test_more ok", 35),
            ),
            "unittest",
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
        (
            "rg TODO .",
            _with_noise(
                _real_agent_lines("src/module.py: TODO marker", 40),
                ["tests/test_module.py: TODO regression"],
                _real_agent_lines("docs/page.md: TODO mention", 40),
            ),
            "search",
        ),
        (
            "bash -lc 'rg TODO .'",
            _with_noise(
                _real_agent_lines("src/module.py: TODO marker", 40),
                ["tests/test_module.py: TODO regression"],
                _real_agent_lines("docs/page.md: TODO mention", 40),
            ),
            "search",
        ),
    ],
)
def test_real_agent_slop_success_outputs_are_strongly_compacted(
    command: str,
    raw: str,
    command_class: str,
) -> None:
    result = reduce_text(
        raw,
        command=command,
        options=options(max_chars=600, max_lines=18, head_lines=3, tail_lines=3),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == command_class
    assert len(result.text) < len(raw) * 0.6
    assert len(result.text.splitlines()) <= 18
    assert "[noisegate: omitted" in result.text


@pytest.mark.parametrize(
    ("command", "raw", "command_class", "signals"),
    [
        (
            "apt-get update",
            _with_noise(
                _real_agent_lines("Get: package index", 40),
                [
                    "W: GPG error: https://example.invalid stable InRelease: NO_PUBKEY DEADBEEF",
                    "E: The repository 'https://example.invalid stable InRelease' is not signed.",
                ],
                _real_agent_lines("tail apt output", 40),
            ),
            "os_package",
            ("GPG error", "not signed"),
        ),
        (
            "apt-get update",
            _with_noise(
                _real_agent_lines("Get: package index", 40),
                ["E: Failed to fetch https://mirror.invalid/Packages Hash Sum mismatch"],
                _real_agent_lines("tail apt output", 40),
            ),
            "os_package",
            ("Hash Sum mismatch",),
        ),
        (
            "apt install -y imaginary-package",
            _with_noise(
                _real_agent_lines("Reading package database", 40),
                ["E: Unable to locate package imaginary-package"],
                _real_agent_lines("tail apt output", 40),
            ),
            "os_package",
            ("Unable to locate package",),
        ),
        (
            "apt-get --yes install imaginary-package",
            _with_noise(
                _real_agent_lines("Reading package database", 40),
                ["E: Unable to locate package imaginary-package"],
                _real_agent_lines("tail apt output", 40),
            ),
            "os_package",
            ("Unable to locate package",),
        ),
        (
            "apt-get -qq update",
            _with_noise(
                _real_agent_lines("Get: package index", 40),
                ["E: Failed to fetch https://mirror.invalid/Packages Hash Sum mismatch"],
                _real_agent_lines("tail apt output", 40),
            ),
            "os_package",
            ("Hash Sum mismatch",),
        ),
        (
            "apt-get -o Acquire::Retries=3 update",
            _with_noise(
                _real_agent_lines("Get: package index", 40),
                ["E: Failed to fetch https://mirror.invalid/Packages Hash Sum mismatch"],
                _real_agent_lines("tail apt output", 40),
            ),
            "os_package",
            ("Hash Sum mismatch",),
        ),
        (
            "apt-get -c apt.conf update",
            _with_noise(
                _real_agent_lines("Get: package index", 40),
                ["E: Failed to fetch https://mirror.invalid/Packages Hash Sum mismatch"],
                _real_agent_lines("tail apt output", 40),
            ),
            "os_package",
            ("Hash Sum mismatch",),
        ),
        (
            "bash -lc 'apt-get -o Acquire::Retries=3 update'",
            _with_noise(
                _real_agent_lines("Get: package index", 40),
                ["E: Failed to fetch https://mirror.invalid/Packages Hash Sum mismatch"],
                _real_agent_lines("tail apt output", 40),
            ),
            "os_package",
            ("Hash Sum mismatch",),
        ),
        (
            "apt install -y curl",
            _with_noise(
                _real_agent_lines("Reading package database", 40),
                [
                    "E: Could not get lock /var/lib/dpkg/lock-frontend. "
                    "It is held by process 1234 (apt)"
                ],
                _real_agent_lines("tail apt output", 40),
            ),
            "os_package",
            ("Could not get lock",),
        ),
        (
            "apt-get -t stable install curl",
            _with_noise(
                _real_agent_lines("Reading package database", 40),
                [
                    "E: Could not get lock /var/lib/dpkg/lock-frontend. "
                    "It is held by process 1234 (apt)"
                ],
                _real_agent_lines("tail apt output", 40),
            ),
            "os_package",
            ("Could not get lock",),
        ),
        (
            "apt-get --target-release stable install curl",
            _with_noise(
                _real_agent_lines("Reading package database", 40),
                [
                    "E: Could not get lock /var/lib/dpkg/lock-frontend. "
                    "It is held by process 1234 (apt)"
                ],
                _real_agent_lines("tail apt output", 40),
            ),
            "os_package",
            ("Could not get lock",),
        ),
        (
            "apt-get remove imaginary-package",
            _with_noise(
                _real_agent_lines("Reading package database", 40),
                ["E: Unable to locate package imaginary-package"],
                _real_agent_lines("tail apt output", 40),
            ),
            "os_package",
            ("Unable to locate package",),
        ),
        (
            "apt-get install broken-package",
            _with_noise(
                _real_agent_lines("Unpacking package", 40),
                ["E: Sub-process /usr/bin/dpkg returned an error code (1)"],
                _real_agent_lines("tail apt output", 40),
            ),
            "os_package",
            ("Sub-process /usr/bin/dpkg", "error code (1)"),
        ),
        (
            "apt-get install imaginary-package && rg TODO .",
            _with_noise(
                _real_agent_lines("Reading package database", 40),
                ["E: Unable to locate package imaginary-package"],
                _real_agent_lines("README.md: TODO", 40),
            ),
            "os_package",
            ("Unable to locate package",),
        ),
        (
            "rg TODO . && apt-get install imaginary-package",
            _with_noise(
                _real_agent_lines("README.md: TODO", 40),
                ["E: Unable to locate package imaginary-package"],
                _real_agent_lines("tail apt output", 40),
            ),
            "os_package",
            ("Unable to locate package",),
        ),
        (
            "apt-get install imaginary-package && pytest -q",
            _with_noise(
                _real_agent_lines("Reading package database " + ("x" * 80), 70),
                ["E: Unable to locate package imaginary-package"],
                _real_agent_lines("tests/test_ok.py .", 40),
            ),
            "os_package",
            ("Unable to locate package",),
        ),
        (
            "npm install && pytest -q",
            _with_noise(
                _real_agent_lines("npm timing idealTree " + ("x" * 80), 70),
                [
                    "npm ERR! code ERESOLVE",
                    "npm ERR! ERESOLVE could not resolve dependency conflict",
                ],
                _real_agent_lines("tests/test_ok.py .", 40),
            ),
            "node",
            ("npm ERR!", "dependency conflict"),
        ),
        (
            "python -m pip install demo",
            _with_noise(
                _real_agent_lines("Collecting dependency", 40),
                [
                    "ERROR: Cannot install demo because these package versions "
                    "have conflicting dependencies.",
                    "ResolutionImpossible: for help visit https://pip.pypa.io/",
                ],
                _real_agent_lines("tail pip output", 40),
            ),
            "dependency_install",
            ("conflicting dependencies", "ResolutionImpossible"),
        ),
        (
            "python -mpip install demo",
            _with_noise(
                _real_agent_lines("Collecting dependency", 40),
                [
                    "ERROR: Cannot install demo because these package versions "
                    "have conflicting dependencies.",
                    "ResolutionImpossible: for help visit https://pip.pypa.io/",
                ],
                _real_agent_lines("tail pip output", 40),
            ),
            "dependency_install",
            ("conflicting dependencies", "ResolutionImpossible"),
        ),
        (
            "python -m pip --disable-pip-version-check install demo",
            _with_noise(
                _real_agent_lines("Collecting dependency", 40),
                [
                    "ERROR: Cannot install demo because these package versions "
                    "have conflicting dependencies.",
                    "ResolutionImpossible: for help visit https://pip.pypa.io/",
                ],
                _real_agent_lines("tail pip output", 40),
            ),
            "dependency_install",
            ("conflicting dependencies", "ResolutionImpossible"),
        ),
        (
            "python -I -m pip --disable-pip-version-check install demo",
            _with_noise(
                _real_agent_lines("Collecting dependency", 40),
                [
                    "ERROR: Cannot install demo because these package versions "
                    "have conflicting dependencies.",
                    "ResolutionImpossible: for help visit https://pip.pypa.io/",
                ],
                _real_agent_lines("tail pip output", 40),
            ),
            "dependency_install",
            ("conflicting dependencies", "ResolutionImpossible"),
        ),
        (
            "pip --proxy http://proxy.invalid install demo",
            _with_noise(
                _real_agent_lines("Collecting dependency", 40),
                [
                    "ERROR: Cannot install demo because these package versions "
                    "have conflicting dependencies.",
                    "ResolutionImpossible: for help visit https://pip.pypa.io/",
                ],
                _real_agent_lines("tail pip output", 40),
            ),
            "dependency_install",
            ("conflicting dependencies", "ResolutionImpossible"),
        ),
        (
            "uv sync",
            _with_noise(
                _real_agent_lines("Resolved dependency", 40),
                [
                    "No solution found when resolving dependencies:",
                    "╰─▶ Because app depends on missing-package, resolution failed.",
                ],
                _real_agent_lines("tail uv output", 40),
            ),
            "dependency_install",
            ("No solution found", "resolution failed"),
        ),
        (
            "uv --project . sync",
            _with_noise(
                _real_agent_lines("Resolved dependency", 40),
                [
                    "No solution found when resolving dependencies:",
                    "╰─▶ Because app depends on missing-package, resolution failed.",
                ],
                _real_agent_lines("tail uv output", 40),
            ),
            "dependency_install",
            ("No solution found", "resolution failed"),
        ),
        (
            "uv pip install pytest",
            _with_noise(
                _real_agent_lines("Prepared distribution", 40),
                ["error: Failed to prepare distributions", "Caused by: dependency conflict"],
                _real_agent_lines("tail uv pip output", 40),
            ),
            "dependency_install",
            ("Failed to prepare", "dependency conflict"),
        ),
        (
            "pip install .",
            _with_noise(
                _real_agent_lines("Collecting dependency", 40),
                [
                    "Traceback (most recent call last):",
                    '  File "setup.py", line 7, in <module>',
                    "RuntimeError: boom",
                ],
                _real_agent_lines("tail pip output", 40),
            ),
            "dependency_install",
            ("Traceback (most recent call last):", "RuntimeError: boom"),
        ),
        (
            "uv sync",
            _with_noise(
                _real_agent_lines("Resolved dependency", 40),
                [
                    "Traceback (most recent call last):",
                    '  File "build.py", line 7, in <module>',
                    "RuntimeError: boom",
                ],
                _real_agent_lines("tail uv output", 40),
            ),
            "dependency_install",
            ("Traceback (most recent call last):", "RuntimeError: boom"),
        ),
        (
            "npm install",
            _with_noise(
                _real_agent_lines("npm timing idealTree", 40),
                [
                    "npm ERR! code ERESOLVE",
                    "npm ERR! ERESOLVE could not resolve dependency conflict",
                ],
                _real_agent_lines("tail npm output", 40),
            ),
            "node",
            ("npm ERR!", "dependency conflict"),
        ),
        (
            "bash -lc 'npm install'",
            _with_noise(
                _real_agent_lines("npm timing idealTree", 40),
                [
                    "npm ERR! code ERESOLVE",
                    "npm ERR! ERESOLVE could not resolve dependency conflict",
                ],
                _real_agent_lines("tail npm output", 40),
            ),
            "node",
            ("npm ERR!", "dependency conflict"),
        ),
        (
            "CI=1 npm install",
            _with_noise(
                _real_agent_lines("npm timing idealTree", 40),
                [
                    "npm ERR! code ERESOLVE",
                    "npm ERR! ERESOLVE could not resolve dependency conflict",
                ],
                _real_agent_lines("tail npm output", 40),
            ),
            "node",
            ("npm ERR!", "dependency conflict"),
        ),
        (
            "bash -euxo pipefail -c 'npm install'",
            _with_noise(
                _real_agent_lines("npm timing idealTree", 40),
                [
                    "npm ERR! code ERESOLVE",
                    "npm ERR! ERESOLVE could not resolve dependency conflict",
                ],
                _real_agent_lines("tail npm output", 40),
            ),
            "node",
            ("npm ERR!", "dependency conflict"),
        ),
        (
            "pnpm install",
            _with_noise(
                _real_agent_lines("Progress: resolved", 40),
                [
                    "pnpm ERR! code ERR_PNPM_PEER_DEP_ISSUES",
                    "pnpm ERR! dependency conflict detected",
                ],
                _real_agent_lines("tail pnpm output", 40),
            ),
            "node",
            ("pnpm ERR!", "dependency conflict"),
        ),
        (
            "yarn install",
            _with_noise(
                _real_agent_lines("YN0000: Resolving", 40),
                [
                    "➤ YN0001: Error: awesome-package@npm:1.0.0: No candidates found",
                    "➤ YN0000: Failed with errors in 2s 130ms",
                ],
                _real_agent_lines("tail yarn output", 40),
            ),
            "node",
            ("YN0001: Error", "Failed with errors"),
        ),
        (
            "npm test",
            _with_noise(
                _real_agent_lines("PASS packages/pkg/test.spec.ts", 40),
                [
                    "FAIL packages/app/component.test.ts",
                    "Error: expected status 200, received 500",
                    "Test Suites: 1 failed, 39 passed, 40 total",
                ],
                _real_agent_lines("tail npm test output", 40),
            ),
            "node",
            ("FAIL packages/app", "expected status 200", "1 failed"),
        ),
        (
            "pnpm test",
            _with_noise(
                _real_agent_lines("✓ package test ok", 40),
                [
                    "ELIFECYCLE Test failed. See above for more details.",
                    "Error: snapshot mismatch in packages/ui/button.test.ts",
                ],
                _real_agent_lines("tail pnpm test output", 40),
            ),
            "node",
            ("ELIFECYCLE", "snapshot mismatch"),
        ),
        (
            "yarn test",
            _with_noise(
                _real_agent_lines("YN0000: Running test shard", 40),
                [
                    "➤ YN0001: Error: Command failed with exit code 1.",
                    "Tests failed in workspace @app/web",
                ],
                _real_agent_lines("tail yarn test output", 40),
            ),
            "node",
            ("YN0001: Error", "exit code 1", "Tests failed"),
        ),
        (
            "docker build .",
            _with_noise(
                _real_agent_lines("#1 CACHED layer", 40),
                [
                    "#12 ERROR: failed to solve: process "
                    '"/bin/sh -c npm test" did not complete successfully: exit code: 1',
                    "Dockerfile:14",
                ],
                _real_agent_lines("tail docker output", 40),
            ),
            "docker_build",
            ("failed to solve", "exit code", "Dockerfile:14"),
        ),
        (
            "docker build .",
            _with_noise(
                _real_agent_lines("#1 CACHED layer", 40),
                [
                    "Traceback (most recent call last):",
                    '  File "app.py", line 7, in <module>',
                    "RuntimeError: boom",
                ],
                _real_agent_lines("tail docker output", 40),
            ),
            "docker_build",
            ("Traceback (most recent call last):", "RuntimeError: boom"),
        ),
        (
            "bash -lc 'docker build .'",
            _with_noise(
                _real_agent_lines("#1 CACHED layer", 40),
                [
                    "#12 ERROR: failed to solve: process "
                    '"/bin/sh -c npm test" did not complete successfully: exit code: 1',
                    "Dockerfile:14",
                ],
                _real_agent_lines("tail docker output", 40),
            ),
            "docker_build",
            ("failed to solve", "exit code", "Dockerfile:14"),
        ),
        (
            "docker buildx build .",
            _with_noise(
                _real_agent_lines("#1 CACHED layer", 40),
                [
                    "#12 ERROR: failed to solve: process "
                    '"/bin/sh -c npm test" did not complete successfully: exit code: 1',
                    "Dockerfile:14",
                ],
                _real_agent_lines("tail docker output", 40),
            ),
            "docker_build",
            ("failed to solve", "exit code", "Dockerfile:14"),
        ),
        (
            "docker image build .",
            _with_noise(
                _real_agent_lines("#1 CACHED layer", 40),
                [
                    "#12 ERROR: failed to solve: process "
                    '"/bin/sh -c npm test" did not complete successfully: exit code: 1',
                    "Dockerfile:14",
                ],
                _real_agent_lines("tail docker output", 40),
            ),
            "docker_build",
            ("failed to solve", "exit code", "Dockerfile:14"),
        ),
        (
            "docker --context default build .",
            _with_noise(
                _real_agent_lines("#1 CACHED layer", 40),
                [
                    "#12 ERROR: failed to solve: process "
                    '"/bin/sh -c npm test" did not complete successfully: exit code: 1',
                    "Dockerfile:14",
                ],
                _real_agent_lines("tail docker output", 40),
            ),
            "docker_build",
            ("failed to solve", "exit code", "Dockerfile:14"),
        ),
        (
            "docker --host unix:///var/run/docker.sock build .",
            _with_noise(
                _real_agent_lines("#1 CACHED layer", 40),
                [
                    "#12 ERROR: failed to solve: process "
                    '"/bin/sh -c npm test" did not complete successfully: exit code: 1',
                    "Dockerfile:14",
                ],
                _real_agent_lines("tail docker output", 40),
            ),
            "docker_build",
            ("failed to solve", "exit code", "Dockerfile:14"),
        ),
        (
            "docker -H unix:///var/run/docker.sock --log-level debug build .",
            _with_noise(
                _real_agent_lines("#1 CACHED layer", 40),
                [
                    "#12 ERROR: failed to solve: process "
                    '"/bin/sh -c npm test" did not complete successfully: exit code: 1',
                    "Dockerfile:14",
                ],
                _real_agent_lines("tail docker output", 40),
            ),
            "docker_build",
            ("failed to solve", "exit code", "Dockerfile:14"),
        ),
        (
            "env CI=1 docker --context default build .",
            _with_noise(
                _real_agent_lines("#1 CACHED layer", 40),
                [
                    "#12 ERROR: failed to solve: process "
                    '"/bin/sh -c npm test" did not complete successfully: exit code: 1',
                    "Dockerfile:14",
                ],
                _real_agent_lines("tail docker output", 40),
            ),
            "docker_build",
            ("failed to solve", "exit code", "Dockerfile:14"),
        ),
        (
            "docker compose build",
            _with_noise(
                _real_agent_lines("=> CACHED service layer", 40),
                [
                    "failed to solve: Dockerfile line 27: failed to compute cache key",
                    "executor failed running [/bin/sh -c make build]: exit code: 2",
                ],
                _real_agent_lines("tail compose output", 40),
            ),
            "docker_build",
            ("failed to solve", "Dockerfile line 27", "exit code"),
        ),
        (
            "docker compose -f docker-compose.yml build",
            _with_noise(
                _real_agent_lines("=> CACHED service layer", 40),
                [
                    "failed to solve: Dockerfile line 27: failed to compute cache key",
                    "executor failed running [/bin/sh -c make build]: exit code: 2",
                ],
                _real_agent_lines("tail compose output", 40),
            ),
            "docker_build",
            ("failed to solve", "Dockerfile line 27", "exit code"),
        ),
        (
            "docker compose --profile web build",
            _with_noise(
                _real_agent_lines("=> CACHED service layer", 40),
                [
                    'failed to solve: process "/bin/sh -c npm test" did not complete successfully',
                    "exit code: 1",
                ],
                _real_agent_lines("tail compose output", 40),
            ),
            "docker_build",
            ("failed to solve", "exit code"),
        ),
        (
            "docker compose --progress plain build",
            _with_noise(
                _real_agent_lines("=> CACHED service layer", 40),
                [
                    'failed to solve: process "/bin/sh -c npm test" did not complete successfully',
                    "exit code: 1",
                ],
                _real_agent_lines("tail compose output", 40),
            ),
            "docker_build",
            ("failed to solve", "exit code"),
        ),
        (
            "docker compose up --build",
            _with_noise(
                _real_agent_lines("=> CACHED service layer", 40),
                [
                    'failed to solve: process "/bin/sh -c npm test" did not complete successfully',
                    "exit code: 1",
                ],
                _real_agent_lines("tail compose output", 40),
            ),
            "docker_build",
            ("failed to solve", "exit code"),
        ),
        (
            "docker compose --profile web up --build",
            _with_noise(
                _real_agent_lines("=> CACHED service layer", 40),
                [
                    "failed to solve: Dockerfile line 27: failed to compute cache key",
                    "executor failed running [/bin/sh -c make build]: exit code: 2",
                ],
                _real_agent_lines("tail compose output", 40),
            ),
            "docker_build",
            ("failed to solve", "Dockerfile line 27", "exit code"),
        ),
        (
            "docker compose --progress plain up --build",
            _with_noise(
                _real_agent_lines("=> CACHED service layer", 40),
                [
                    "failed to solve: Dockerfile line 27: failed to compute cache key",
                    "executor failed running [/bin/sh -c make build]: exit code: 2",
                ],
                _real_agent_lines("tail compose output", 40),
            ),
            "docker_build",
            ("failed to solve", "Dockerfile line 27", "exit code"),
        ),
        (
            "pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                [
                    "FAILED tests/test_app.py::test_status - AssertionError: boom",
                    "E       AssertionError: boom",
                ],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "coverage run -m pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                [
                    "ERROR tests/test_import.py - ImportError: boom",
                    "ImportError: boom",
                ],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("ERROR tests/test_import.py", "ImportError"),
        ),
        (
            "bash -O extglob -c 'pytest -q'",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                [
                    "FAILED tests/test_app.py::test_status - AssertionError: boom",
                    "E       AssertionError: boom",
                ],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "python -m unittest discover",
            _with_noise(
                _real_agent_lines("test_ok ok", 40),
                ["FAILED (failures=1)", "AssertionError: boom"],
                _real_agent_lines("tail unittest output", 40),
            ),
            "unittest",
            ("FAILED (failures=1)", "AssertionError"),
        ),
        (
            "uv sync && uv run pytest -q",
            _with_noise(
                _real_agent_lines("Resolved dependency", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "uv sync&&uv run pytest -q",
            _with_noise(
                _real_agent_lines("Resolved dependency", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "uv sync; uv run pytest -q",
            _with_noise(
                _real_agent_lines("Resolved dependency", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "bash -lc 'uv sync && uv run pytest -q'",
            _with_noise(
                _real_agent_lines("Resolved dependency", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "uv run python -m pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "uv run python -I -m pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "python -W ignore -m pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "python -X dev -m pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "python3.11 -m pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            ".venv/bin/python -m pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "/usr/bin/python3.11 -m pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "py -m pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "uv run python3.11 -m pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "/usr/bin/uv run pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            ".venv/bin/uv run pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "uv run --extra dev pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "uv run --package pkg --env-file .env --with-editable . pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "uv run -C build pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "uv run --refresh-package demo pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "poetry run pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "poetry -C pkg run pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "poetry --directory pkg run pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "poetry run -- pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "pdm -p pkg run pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "hatch -e test run pytest -q",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "bash --norc -c 'pytest -q'",
            _with_noise(
                _real_agent_lines("tests/test_ok.py .", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "uv sync && uv run python -m pytest -q",
            _with_noise(
                _real_agent_lines("Resolved dependency", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
        ),
        (
            "python -m pip install -e . && pytest -q",
            _with_noise(
                _real_agent_lines("Collecting dependency", 40),
                ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
                _real_agent_lines("tail pytest output", 40),
            ),
            "pytest",
            ("FAILED tests/test_app.py", "AssertionError"),
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
        (
            "cd pkg && find . -type f",
            _with_noise(
                _real_agent_lines("./src/file", 40),
                ["find: './secret': Permission denied"],
                _real_agent_lines("./tests/test_file", 40),
            ),
            "inventory",
            ("Permission denied",),
        ),
        (
            "bash -lc 'cd pkg && find . -type f'",
            _with_noise(
                _real_agent_lines("./src/file", 40),
                ["find: './secret': Permission denied"],
                _real_agent_lines("./tests/test_file", 40),
            ),
            "inventory",
            ("Permission denied",),
        ),
        (
            "ls -laR .",
            _with_noise(
                _real_agent_lines("./src/file", 40),
                ["ls: cannot access './secret': Permission denied"],
                _real_agent_lines("./tests/test_file", 40),
            ),
            "inventory",
            ("cannot access", "Permission denied"),
        ),
    ],
)
def test_real_agent_slop_failure_outputs_preserve_actionable_error_signals(
    command: str,
    raw: str,
    command_class: str,
    signals: tuple[str, ...],
) -> None:
    result = reduce_text(
        raw,
        command=command,
        exit_code=1,
        options=options(max_chars=900, max_lines=20, head_lines=3, tail_lines=3),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == command_class
    assert len(result.text.splitlines()) <= 20
    assert "[noisegate: omitted" in result.text
    for signal in signals:
        assert signal in result.text


def test_inventory_char_budget_preserves_prefixed_error_after_head_context() -> None:
    raw = "\n".join(
        [
            *["./src/generated/very_long_inventory_entry_" + ("x" * 80) for _ in range(8)],
            "find: paths must precede expression: `-name'",
            *["./tests/generated/very_long_inventory_entry_" + ("z" * 80) for _ in range(8)],
        ]
    )

    result = reduce_text(
        raw,
        command="find . -type f",
        exit_code=1,
        options=options(max_chars=180, max_lines=10, head_lines=2, tail_lines=2),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == "inventory"
    assert len(result.text) <= 180
    assert "find: paths must precede expression" in result.text


def test_runner_wrapped_pytest_beats_dependency_words_in_arguments() -> None:
    raw = _with_noise(
        _real_agent_lines("tests/test_ok.py .", 40),
        ["FAILED tests/test_app.py::test_status - AssertionError: boom"],
        _real_agent_lines("tail pytest output", 40),
    )

    for command in (
        "poetry run pytest -k 'pip install'",
        "poetry -C pkg run pytest -k 'pip install'",
        "pdm run -- pytest -k 'pip install'",
        "hatch run -- pytest -k 'pip install'",
    ):
        result = reduce_text(
            raw,
            command=command,
            exit_code=1,
            options=options(max_chars=500, max_lines=12, head_lines=2, tail_lines=2),
        )

        assert result.changed is True
        assert result.metadata["command_class"] == "pytest"
        assert "FAILED tests/test_app.py" in result.text


def test_shell_chains_with_file_display_and_pytest_preserve_pytest_failure() -> None:
    raw = _with_noise(
        _real_agent_lines("file line", 40),
        [
            "FAILED tests/test_app.py::test_status - AssertionError: boom",
            "E       AssertionError: boom",
        ],
        _real_agent_lines("tail pytest output", 40),
    )

    for command in (
        "cat src/foo.py\npytest -q",
        "bash -lc 'cat src/foo.py\npytest -q'",
        "cat important.txt |& pytest -q",
        "bash -lc 'cat important.txt & pytest -q'",
    ):
        result = reduce_text(
            raw,
            command=command,
            exit_code=1,
            options=options(max_chars=500, max_lines=12, head_lines=2, tail_lines=2),
        )

        assert result.changed is True
        assert result.metadata["command_class"] == "pytest"
        assert "FAILED tests/test_app.py" in result.text
        assert "AssertionError: boom" in result.text


def test_package_docker_and_inventory_tracebacks_keep_exception_under_tight_budget() -> None:
    cases = (
        ("pip install .", "dependency_install"),
        ("uv sync", "dependency_install"),
        ("docker build .", "docker_build"),
        ("find . -type f", "inventory"),
    )
    for command, command_class in cases:
        raw = _with_noise(
            _real_agent_lines("setup noise " + ("x" * 60), 30),
            [
                "Traceback (most recent call last):",
                '  File "build.py", line 7, in <module>',
                "RuntimeError: boom",
            ],
            _real_agent_lines("tail noise " + ("z" * 60), 30),
        )

        for max_chars in (220, 260, 300):
            result = reduce_text(
                raw,
                command=command,
                exit_code=1,
                options=options(max_chars=max_chars, max_lines=8, head_lines=1, tail_lines=1),
            )

            assert result.changed is True
            assert result.metadata["command_class"] == command_class
            assert "Traceback (most recent call last):" in result.text
            assert "RuntimeError: boom" in result.text


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
