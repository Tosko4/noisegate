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


@pytest.mark.parametrize(
    ("command", "exact_read"),
    (
        ("fd -e py --exec=cat", True),
        ("fdfind -e py --exec-batch=cat", True),
        ("fd --exec='cat'", True),
        ("fd '--exec=cat'", True),
        ("fd --exec=/bin/cat", True),
        ("fdfind --exec-batch=/usr/bin/cat", True),
        ("fd --exec=cat -- .", True),
        ("fd --exec=/tmp/cat", False),
        ("fd --exec=//host/share/cat", False),
        ("fd -e py --exec=pytest", False),
        ("fd -x echo --exec=cat", False),
        ("fd --exec echo --exec-batch=cat", False),
        ("fdfind -X printf --exec=cat", False),
        ("fd -- . --exec=cat", False),
    ),
)
def test_fd_attached_exec_consumers_preserve_only_exact_file_reads(
    command: str,
    exact_read: bool,
) -> None:
    raw = source_like_python()

    result = reduce_text(
        raw,
        command=command,
        tool_name="terminal",
        options=options(max_chars=220, max_lines=20),
    )

    if exact_read:
        assert result.changed is False, command
        assert result.text == raw
        assert result.metadata["command_class"] == "file_read"
    else:
        assert result.changed is True
        assert result.text != raw
        assert result.metadata["command_class"] != "file_read"


def test_fd_attached_exec_does_not_swallow_later_compactable_output() -> None:
    source = "\n".join(
        ["# exact source output", *[f"def exact_{index}(): return {index}" for index in range(80)]]
    )
    raw = source + "\n" + "\n".join(
        [
            *[f"uv resolver chatter {index:03d}" for index in range(50)],
            "  × No solution found when resolving dependencies:",  # noqa: RUF001
            "  ╰─▶ requirements are unsatisfiable.",
            *[f"backtracking {index:03d}" for index in range(50)],
        ]
    )

    for command in (
        "fd --exec=cat && uv run pytest -q",
        "fd --exec=cat && cd repo && uv run pytest -q",
        "fd --exec=cat && export MODE=test && uv run pytest -q",
        "fdfind --exec-batch=cat; uv run pytest -q",
        "uv run fd --exec=cat && uv run pytest -q",
        "sh -c 'fd --exec=cat' && uv run pytest -q",
        "sh -c 'cd . && fd --exec=cat' && uv run pytest -q",
        "uv run sh -c 'cd . && uv run sh -c \"fd --exec=cat\"' && uv run pytest -q",
        "sh -c 'fd --exec=cat; exit 1' || uv run pytest -q",
        "uv run sh -c 'uv run sh -c \"fd --exec=cat\"' && uv run pytest -q",
        "fd --exec=cat && false || true && uv run pytest -q",
        "fd --exec=cat && (exit 0) && uv run pytest -q",
        "fd --exec=cat && (exit 0; false) && uv run pytest -q",
        "fd --exec=cat && uv run pytest -q >/dev/null",
    ):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=options(
                max_chars=260,
                max_lines=8,
                head_lines=1,
                tail_lines=1,
                important_context_lines=0,
            ),
        )

        assert result.changed is True, command
        assert result.metadata["command_class"] == "python_package", command
        assert result.metadata["reducer"] == "python_package", command
        assert "No solution found when resolving dependencies" in result.text, command
        assert "[noisegate: exit_code=1]" in result.text, command

    exact_fixture = "\n".join(
        [
            "UV_FAILURE_FIXTURE = '''",
            "  × No solution found when resolving dependencies:",  # noqa: RUF001
            "  ╰─▶ requirements are unsatisfiable.",
            "'''",
            *[f"def exact_{index}(): return {index}" for index in range(80)],
        ]
    )
    for command, exit_code in (
        ("fd --exec=cat && false && uv run pytest -q", 1),
        ("fd --exec=cat && { false; } && uv run pytest -q", 1),
        ("fd --exec=cat && command false && uv run pytest -q", 1),
        ("fd --exec=cat && exit 1 && uv run pytest -q", 1),
        ("fd --exec=cat && exit 1 || uv run pytest -q", 1),
        ("fd --exec=cat && (exit 1) && uv run pytest -q", 1),
        ("fd --exec=cat && (exit 1; true) && uv run pytest -q", 1),
        ("fd --exec=cat && ! true && uv run pytest -q", 1),
        ("fd --exec=cat && false >/dev/null && uv run pytest -q", 1),
        ("fd --exec=cat && exit 1 >/dev/null && uv run pytest -q", 1),
        ("fd --exec=cat && ! true >/dev/null && uv run pytest -q", 1),
        ("fd --exec=cat && test -f definitely-missing && uv run pytest -q", 1),
        (
            "fd --exec=cat && cd /definitely/missing 2>/dev/null && uv run pytest -q",
            1,
        ),
        (
            "fd --exec=cat && export 1BAD=value 2>/dev/null && uv run pytest -q",
            1,
        ),
        (
            "fd --exec=cat && export MODE=test 2>/dev/null && uv run pytest -q",
            1,
        ),
        ("fd --exec=cat && env cd repo && uv run pytest -q", 127),
        ("fd --exec=cat && ./cd repo && uv run pytest -q", 1),
        ("fd --exec=cat && source ./setup.sh && uv run pytest -q", 1),
        ("fd --exec=cat && . ./setup.sh && uv run pytest -q", 1),
        ("fd --exec=cat & false && uv run pytest -q", 1),
        ("fd --exec=cat && uv run pytest -q 2>/dev/null", 1),
        ("fd --exec=cat && { uv run pytest -q; } 2>/dev/null", 1),
        ("fd --exec=cat && uv run pytest -q >/dev/null", 0),
    ):
        result = reduce_text(
            exact_fixture,
            command=command,
            tool_name="terminal",
            exit_code=exit_code,
            options=options(max_chars=220, max_lines=7, head_lines=1, tail_lines=1),
        )

        assert result.changed is False, command
        assert result.text == exact_fixture, command
        assert result.metadata["command_class"] == "file_read", command
        assert result.metadata["reducer"] == "protected_file_read", command

    for command, setup_error in (
        (
            "fd --exec=cat && source /definitely/missing && uv run pytest -q",
            "bash: line 1: /definitely/missing: No such file or directory",
        ),
        (
            "fd --exec=cat && . /definitely/missing && uv run pytest -q",
            "bash: line 1: /definitely/missing: No such file or directory",
        ),
        (
            "fd --exec=cat && . /definitely/missing && uv run pytest -q",
            "/bin/sh: 1: .: cannot open /definitely/missing: No such file",
        ),
        (
            "fd --exec=cat && popd && uv run pytest -q",
            "bash: line 1: popd: directory stack empty",
        ),
        (
            "fd --exec=cat && cd one two && uv run pytest -q",
            "bash: line 1: cd: too many arguments",
        ),
        (
            "fd --exec=cat && export SHELLOPTS=value && uv run pytest -q",
            "bash: line 1: SHELLOPTS: readonly variable",
        ),
        (
            "fd --exec=cat && export SHELLOPTS=value && uv run pytest -q",
            "-bash: SHELLOPTS: readonly variable",
        ),
        (
            "fd --exec=cat && export PPID=1 && uv run pytest -q",
            "ash: PPID: readonly variable",
        ),
    ):
        setup_failure_output = exact_fixture + "\n" + setup_error
        result = reduce_text(
            setup_failure_output,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=options(max_chars=220, max_lines=7, head_lines=1, tail_lines=1),
        )

        assert result.changed is False, command
        assert result.text == setup_failure_output, command
        assert result.metadata["command_class"] == "file_read", command
        assert result.metadata["reducer"] == "protected_file_read", command


def test_shell_substitution_file_display_commands_are_not_file_read() -> None:
    raw = source_like_python()

    for command in (
        "cat $(pytest -q)",
        "cat `pytest -q`",
        "cat \"$(pytest -q)\"",
        "cat 'file\\'; pytest -q",
        "nl -ba src/foo.py\npytest -q",
        "nl -ba src/foo.py\r\npytest -q",
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


def test_uv_pytest_resolution_failure_uses_python_package_reducer() -> None:
    resolver_detail = (
        "  ╰─▶ Because no version of private-lib matches >=2 and your project depends "
        "on private-lib>=2, the requirements are unsatisfiable."
    )
    resolver_output = "\n".join(
        [
            *[f"checking candidate {index:03d}" for index in range(60)],
            "  × No solution found when resolving dependencies:",  # noqa: RUF001
            resolver_detail,
            *[f"backtracking candidate {index:03d}" for index in range(60)],
        ]
    )

    for command in (
        "uv run --with private-lib pytest -q",
        "bash -lc 'uv run --with private-lib pytest -q'",
        "cd repo && uv run --with private-lib pytest -q",
        "bash -lc 'cd repo && uv run --with private-lib pytest -q'",
    ):
        result = reduce_text(
            resolver_output,
            command=command,
            exit_code=1,
            options=options(
                max_chars=280,
                max_lines=6,
                head_lines=1,
                tail_lines=1,
                important_context_lines=1,
            ),
        )

        assert result.changed is True, command
        assert result.metadata["command_class"] == "python_package", command
        assert result.metadata["reducer"] == "python_package", command
        assert "No solution found when resolving dependencies" in result.text, command

    ordinary_pytest = "\n".join(
        [
            "=================================== FAILURES ===================================",
            "FAILED tests/test_many.py::test_signal",
            "E       AssertionError: boom",
            *[f"pytest noise {index}" for index in range(40)],
        ]
    )
    ordinary_result = reduce_text(
        ordinary_pytest,
        command="uv run --with private-lib pytest -q",
        exit_code=1,
        options=options(max_chars=280, max_lines=6),
    )

    assert ordinary_result.metadata["command_class"] == "pytest"
    assert ordinary_result.metadata["reducer"] == "pytest"
    assert "AssertionError: boom" in ordinary_result.text


def test_uv_pytest_resolution_failure_beats_generic_failed_noise_at_tiny_budget() -> None:
    raw = "\n".join(
        [
            *[f"checking candidate {index:03d}" for index in range(30)],
            "DEBUG fetch failed for optional metadata cache; retrying",
            *[f"retry noise {index:03d}" for index in range(15)],
            "  × No solution found when resolving dependencies:",  # noqa: RUF001
            "  ╰─▶ requirements are unsatisfiable.",
            *[f"backtracking candidate {index:03d}" for index in range(30)],
        ]
    )

    result = reduce_text(
        raw,
        command="uv run pytest -q",
        exit_code=1,
        options=options(
            max_chars=110,
            max_lines=3,
            head_lines=1,
            tail_lines=1,
            important_context_lines=0,
        ),
    )

    assert result.metadata["command_class"] == "python_package"
    assert "No solution found when resolving dependencies" in result.text
    assert "[noisegate: exit_code=1]" in result.text


def test_uv_pytest_literal_resolver_text_does_not_mask_real_test_failure() -> None:
    raw = "\n".join(
        [
            *[f"setup noise {index:03d}" for index in range(50)],
            "application log: No solution found when resolving dependencies",
            *[f"middle noise {index:03d}" for index in range(30)],
            "=================================== FAILURES ===================================",
            "E       TypeError: actual production regression",
            "FAILED src/pkg/test_real.py::test_real_bug - TypeError: actual production regression",
            *[f"teardown noise {index:03d}" for index in range(50)],
        ]
    )

    result = reduce_text(
        raw,
        command="uv run pytest -q",
        exit_code=1,
        options=options(
            max_chars=280,
            max_lines=6,
            head_lines=1,
            tail_lines=1,
            important_context_lines=0,
        ),
    )

    assert result.metadata["command_class"] == "pytest"
    assert "TypeError: actual production regression" in result.text
    assert "FAILED src/pkg/test_real.py::test_real_bug" in result.text

    passing_output = "\n".join(
        [
            *[f"setup noise {index:03d}" for index in range(50)],
            "  × No solution found when resolving dependencies:",  # noqa: RUF001
            *[f"captured noise {index:03d}" for index in range(50)],
            "1 passed in 0.03s",
        ]
    )
    passing_result = reduce_text(
        passing_output,
        command="uv run pytest -q",
        exit_code=0,
        options=options(max_chars=180, max_lines=5, head_lines=1, tail_lines=1),
    )

    assert passing_result.metadata["command_class"] == "pytest"
    assert "1 passed in 0.03s" in passing_result.text

    collection_output = "\n".join(
        [
            *[f"setup noise {index:03d}" for index in range(30)],
            "  × No solution found when resolving dependencies:",  # noqa: RUF001
            *[f"middle noise {index:03d}" for index in range(20)],
            "ERROR pkg/feature/test_collect.py - RuntimeError: actual collection regression",
            *[f"teardown noise {index:03d}" for index in range(30)],
        ]
    )
    collection_result = reduce_text(
        collection_output,
        command="uv run pytest -q",
        exit_code=1,
        options=options(max_chars=110, max_lines=3, head_lines=1, tail_lines=1),
    )

    assert collection_result.metadata["command_class"] == "pytest"
    assert "RuntimeError: actual collection regression" in collection_result.text


def test_pytest_reducer_keeps_exception_and_failed_node_under_tight_budget() -> None:
    for exception_line in (
        "AssertionError: expected signal, got noise",
        "ModuleNotFoundError: missing",
        "ImportError: cannot import name x",
        "TypeError: bad",
    ):
        raw = "\n".join(
            [
                *[f"pytest setup noise {index}" for index in range(100)],
                "Traceback (most recent call last):",
                '  File "tests/test_demo.py", line 3, in test_signal',
                exception_line,
                "FAILED tests/test_demo.py::test_signal",
                *[f"pytest teardown noise {index}" for index in range(100)],
            ]
        )

        result = reduce_text(
            raw,
            command="pytest -q",
            exit_code=1,
            options=NoisegateOptions(
                max_chars=400,
                max_lines=8,
                head_lines=1,
                tail_lines=1,
                important_context_lines=1,
            ),
        )

        assert result.changed is True, exception_line
        assert result.metadata["reducer"] == "pytest", exception_line
        assert exception_line in result.text, exception_line
        assert "FAILED tests/test_demo.py::test_signal" in result.text, exception_line


def test_common_python_traceback_exception_survives_non_pytest_reducers() -> None:
    raw = "\n".join(
        [
            *[f"noise {index}" for index in range(120)],
            "Traceback (most recent call last):",
            '  File "app.py", line 1, in <module>',
            "ModuleNotFoundError: No module named 'missing_pkg'",
            *[f"after {index}" for index in range(80)],
        ]
    )
    commands = {
        "python app.py": "generic_critical",
        "pip install .": "python_package",
        "docker logs api": "docker_logs",
        "docker build .": "docker_build",
    }

    for command, reducer in commands.items():
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=NoisegateOptions(
                max_chars=220,
                max_lines=8,
                head_lines=1,
                tail_lines=1,
                important_context_lines=1,
            ),
        )

        assert result.changed is True, command
        assert result.metadata["reducer"] == reducer, command
        assert "Traceback (most recent call last):" in result.text, command
        assert "ModuleNotFoundError: No module named 'missing_pkg'" in result.text, command


def test_tiny_traceback_budget_keeps_concrete_failure_and_exit_notice() -> None:
    raw = "\n".join(
        [
            *[f"noise before {index}" for index in range(100)],
            "Traceback (most recent call last):",
            '  File "tests/test_demo.py", line 3, in test_signal',
            "ModuleNotFoundError: No module named missing_pkg",
            "FAILED tests/test_demo.py::test_signal",
            *[f"noise after {index}" for index in range(100)],
        ]
    )
    commands = {
        "pytest -q": "pytest",
        "python app.py": "generic_critical",
        "pip install .": "python_package",
        "docker logs api": "docker_logs",
        "docker build .": "docker_build",
    }

    for command, reducer in commands.items():
        for max_lines in (3, 4, 5, 6):
            result = reduce_text(
                raw,
                command=command,
                tool_name="terminal",
                exit_code=1,
                options=NoisegateOptions(
                    max_chars=360,
                    max_lines=max_lines,
                    head_lines=1,
                    tail_lines=1,
                    important_context_lines=1,
                ),
            )

            case = f"{command} max_lines={max_lines}"
            assert result.changed is True, case
            assert result.metadata["reducer"] == reducer, case
            assert "ModuleNotFoundError: No module named missing_pkg" in result.text, case
            assert "FAILED tests/test_demo.py::test_signal" in result.text, case
            assert "[noisegate: exit_code=1]" in result.text, case


def test_plain_exception_root_cause_beats_generic_unhandled_banner() -> None:
    raw = "\n".join(
        [
            *[f"noise {index}" for index in range(80)],
            "Unhandled exception",
            "Exception: root cause",
            *[f"tail {index}" for index in range(80)],
        ]
    )

    result = reduce_text(
        raw,
        command="python app.py",
        tool_name="terminal",
        exit_code=1,
        options=NoisegateOptions(
            max_chars=160,
            max_lines=3,
            head_lines=1,
            tail_lines=1,
            important_context_lines=1,
        ),
    )

    assert result.changed is True
    assert "Exception: root cause" in result.text
    assert "[noisegate: exit_code=1]" in result.text


def test_program_output_piped_to_cat_still_compacts_as_runtime_failure() -> None:
    raw = "\n".join(
        [
            *[f"noise {index}" for index in range(80)],
            "Traceback (most recent call last):",
            '  File "app.py", line 1, in <module>',
            "ModuleNotFoundError: No module named 'missing_pkg'",
            *[f"tail {index}" for index in range(80)],
        ]
    )

    for command in ("python app.py | cat", "python app.py | head -100"):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=NoisegateOptions(
                max_chars=220,
                max_lines=8,
                head_lines=1,
                tail_lines=1,
                important_context_lines=1,
            ),
        )

        assert result.changed is True, command
        assert result.metadata["command_class"] == "generic", command
        assert "ModuleNotFoundError: No module named 'missing_pkg'" in result.text, command


def test_search_output_is_source_context_and_stays_exact() -> None:
    raw = "\n".join(f"src/file_{index}.py:match {index}" for index in range(1, 40))

    result = reduce_text(raw, command="rg match src", options=options())

    assert result.changed is False
    assert result.metadata["reducer"] == "protected_source_search"
    assert result.text == raw


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
