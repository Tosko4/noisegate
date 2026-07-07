from __future__ import annotations

import re

import pytest

import noisegate.engine as engine
from noisegate.engine import NoisegateOptions, _first_pattern_match, reduce_text


def numbered(prefix: str, count: int) -> str:
    return "\n".join(f"{prefix} {index:03d}" for index in range(1, count + 1))


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


def test_important_pattern_tight_budget_keeps_match_centered_excerpt() -> None:
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

    assert result.changed is True
    assert len(result.text) <= 40
    assert "FAILED" in result.text


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
        options=options(max_chars=100, max_lines=10, head_lines=1, tail_lines=1),
    )

    assert result.changed is True
    assert len(result.text) <= 100
    assert "FAILED" in result.text
    assert "FAILE\n" not in result.text


def test_recovery_notices_do_not_destroy_tight_important_excerpt() -> None:
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

    assert result.changed is True
    assert len(result.text) <= 40
    assert "FAILED" in result.text
    assert "[noisegate" not in result.text or "[noisegate:" in result.text


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

    assert result.changed is True
    assert len(result.text) <= 80
    assert len(result.text.splitlines()) <= 3
    assert "FAILED tests/test_middle.py::test_breaks - AssertionError: boom" in result.text
    assert "exit_code=1" not in result.text


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

    assert result.changed is True
    assert len(result.text) <= 80
    assert len(result.text.splitlines()) <= 4
    assert "FAILED tests/test_middle.py::test_breaks - AssertionError: boom" in result.text
    assert "lines]" not in result.text
    assert "exit_code=1" not in result.text


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

    assert result.changed is True
    assert len(result.text) <= 84
    assert len(result.text.splitlines()) <= 5
    assert "FAILED tests/test_middle.py::test_breaks - AssertionError: boom" in result.text
    assert "lines]" not in result.text
    assert "chars]" not in result.text
    assert "exit_code=1" not in result.text


def test_node_reducer_tight_budget_preserves_node_error() -> None:
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

    assert result.changed is True
    assert len(result.text) <= 80
    assert "npm ERR!" in result.text


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

    assert result.changed is True
    assert len(result.text) <= 110
    assert "FAILED" in result.text
    assert "noisegate artifact:" not in result.text
    assert result.metadata["artifact"] == {
        "stored": False,
        "reason": "recovery_notice_dropped",
        "size_bytes": len(raw.encode()),
    }
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
