from __future__ import annotations

import re

import pytest

import noisegate.engine as engine
from noisegate.engine import NoisegateOptions, _first_pattern_match, reduce_text
from noisegate.plugin import transform_terminal_output


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


def capture_alignment_budgets(
    monkeypatch: pytest.MonkeyPatch,
    *limits: int,
) -> list[engine._SourceAlignmentWorkBudget]:
    budgets: list[engine._SourceAlignmentWorkBudget] = []
    configured_limits = iter(limits)

    def new_budget() -> engine._SourceAlignmentWorkBudget:
        budget = engine._SourceAlignmentWorkBudget(
            next(configured_limits, engine._SOURCE_ALIGNMENT_WORK_LIMIT)
        )
        budgets.append(budget)
        return budget

    monkeypatch.setattr(engine, "_new_source_alignment_work_budget", new_budget)
    return budgets


def assert_fail_open_or_truthful_failure_excerpt(
    raw: str,
    transformed: str | None,
) -> None:
    if transformed is None or transformed == raw:
        return

    output_lines = transformed.splitlines()
    assert output_lines[-1] == "[noisegate: exit_code=1]"
    excerpt_lines = output_lines[:-1]
    source_lines = raw.splitlines()
    source_index = 0
    saw_omission = False
    for line in excerpt_lines:
        omission = re.fullmatch(r"\[noisegate: omitted (\d+) lines\]", line)
        if omission is not None:
            omitted_count = int(omission.group(1))
            assert omitted_count > 0
            source_index += omitted_count
            assert source_index <= len(source_lines)
            saw_omission = True
            continue
        assert source_index < len(source_lines)
        assert line == source_lines[source_index]
        source_index += 1

    assert source_index == len(source_lines)
    if excerpt_lines != source_lines:
        assert saw_omission


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


def test_write_diagnostic_location_patterns_are_line_anchored() -> None:
    assert all(
        pattern.pattern.startswith("^") for pattern in engine.DIAGNOSTIC_LOCATION_PATTERNS
    )


def test_write_diagnostic_location_patterns_reject_long_nonmatching_line() -> None:
    long_noise = "x" * 8_192

    assert all(
        pattern.search(long_noise) is None
        for pattern in engine.DIAGNOSTIC_LOCATION_PATTERNS
    )


def test_generic_head_tail_remaps_colliding_upstream_line_omission_by_coverage() -> None:
    raw = "\n".join(
        [
            "head",
            "line-1",
            "line-2",
            "line-3",
            "[noisegate: omitted 8 lines]",
            "line-5",
            "line-6",
            "line-7",
            "line-8",
            "tail",
        ]
    )

    result = reduce_text(
        raw,
        command="make noisy",
        options=options(
            max_chars=10_000,
            max_lines=3,
            head_lines=1,
            tail_lines=1,
        ),
    )

    assert result.changed is True
    assert result.text == "head\n[noisegate: omitted 15 lines]\ntail"
    assert engine._represented_line_coverage(result.text) == 17


@pytest.mark.parametrize(
    "trailing_newline",
    ["", "\n"],
    ids=("no_trailing_newline", "trailing_newline"),
)
def test_generic_tail_only_remap_preserves_duplicate_line_source_position(
    trailing_newline: str,
) -> None:
    raw = "\n".join(
        [
            "same",
            "[noisegate: omitted 100 lines]",
            "middle",
            "same",
        ]
    ) + trailing_newline

    result = reduce_text(
        raw,
        command="make noisy",
        options=options(
            max_chars=10_000,
            max_lines=3,
            head_lines=0,
            tail_lines=1,
        ),
    )

    assert result.changed is True
    assert result.text == "[noisegate: omitted 102 lines]\nsame"
    assert engine._represented_line_coverage(result.text) == 103


def test_generic_head_only_remap_preserves_duplicate_line_source_position() -> None:
    raw = "\n".join(
        [
            "same",
            "[noisegate: omitted 100 lines]",
            "middle",
            "same",
        ]
    )

    result = reduce_text(
        raw,
        command="make noisy",
        options=options(
            max_chars=10_000,
            max_lines=3,
            head_lines=1,
            tail_lines=0,
        ),
    )

    assert result.changed is True
    assert result.text == "same\n[noisegate: omitted 102 lines]"
    assert engine._represented_line_coverage(result.text) == 103


def test_remap_selects_interior_duplicate_between_generated_boundary_markers() -> None:
    source_lines = [
        "same",
        "before",
        "[noisegate: omitted 5 lines]",
        "same",
        "after",
        "same",
    ]
    excerpt = engine._marked_excerpt_for_line_indices(source_lines, [3])

    assert excerpt == "[noisegate: omitted 3 lines]\nsame\n[noisegate: omitted 2 lines]"
    assert engine._remark_excerpt_with_line_coverage(
        "\n".join(source_lines),
        excerpt,
    ) == "[noisegate: omitted 7 lines]\nsame\n[noisegate: omitted 2 lines]"


@pytest.mark.parametrize(
    ("source_lines", "kept_indices", "expected"),
    [
        (
            [
                "duplicate anchor",
                "noise-a",
                "[noisegate: omitted 10 lines]",
                "duplicate anchor",
                "noise-b",
                "duplicate anchor",
            ],
            [0, 5],
            "duplicate anchor\n[noisegate: omitted 13 lines]\nduplicate anchor",
        ),
        (
            [
                "duplicate anchor",
                "noise-a",
                "duplicate anchor",
                "[noisegate: omitted 5 lines]",
                "noise-b",
                "duplicate anchor",
                "noise-c",
                "duplicate anchor",
                "noise-d",
            ],
            [0, 5, 7],
            "\n".join(
                [
                    "duplicate anchor",
                    "[noisegate: omitted 8 lines]",
                    "duplicate anchor",
                    "[noisegate: omitted 1 lines]",
                    "duplicate anchor",
                    "[noisegate: omitted 1 lines]",
                ]
            ),
        ),
    ],
    ids=("two_anchors", "three_anchors"),
)
def test_stitched_duplicate_anchors_remap_monotonically_by_occurrence(
    source_lines: list[str],
    kept_indices: list[int],
    expected: str,
) -> None:
    excerpt = engine._marked_excerpt_for_line_indices(source_lines, kept_indices)

    assert excerpt is not None
    assert engine._remark_excerpt_with_line_coverage(
        "\n".join(source_lines),
        excerpt,
    ) == expected
    assert engine._represented_line_coverage(expected) == engine._represented_line_coverage(
        "\n".join(source_lines)
    )


def test_ambiguous_generated_or_upstream_marker_mapping_fails_open() -> None:
    raw = "\n".join(
        [
            "[noisegate: omitted 3 lines]",
            "same",
            "middle",
            "same",
        ]
    )

    result = reduce_text(
        raw,
        command="make noisy",
        options=options(
            max_chars=10_000,
            max_lines=3,
            head_lines=0,
            tail_lines=1,
        ),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["reason"] == "reducer_no_output"


def test_repeated_omission_only_alignment_has_linear_work_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "[noisegate: omitted 1 lines]"
    source_size = 1_000
    source_lines = [marker] * source_size
    excerpt = "\n".join(
        [
            *([marker] * 24),
            "[noisegate: omitted 960 lines]",
            *([marker] * 16),
        ]
    )
    work_limit = source_size * 2

    assert engine._source_line_indices_for_excerpt(
        source_lines,
        excerpt,
        _work_limit=work_limit,
    ) == []

    monkeypatch.setattr(engine, "_SOURCE_ALIGNMENT_WORK_LIMIT", work_limit)
    result = reduce_text(
        "\n".join(source_lines),
        command="make noisy",
        options=NoisegateOptions(
            max_chars=4_000,
            max_lines=160,
            head_lines=24,
            tail_lines=16,
        ),
    )

    assert result.changed is True
    assert result.text == "[noisegate: omitted 1000 lines]"


def test_duplicate_alignment_work_exhaustion_returns_ambiguous() -> None:
    marker = "[noisegate: omitted 1 lines]"
    source_lines = [part for _ in range(20) for part in ("same", marker)] + ["tail"]
    excerpt = "\n".join(source_lines)

    assert engine._source_line_indices_for_excerpt(
        source_lines,
        excerpt,
        _work_limit=8,
    ) is None
    assert engine._source_line_indices_for_excerpt(
        source_lines,
        excerpt,
        _work_limit=10_000,
    ) == [*range(0, 40, 2), 40]


@pytest.mark.parametrize(
    "entrypoint_name",
    ["reduce_text", "_preview_reduce_text"],
)
def test_public_reduction_shares_one_alignment_budget_across_ranked_candidates(
    monkeypatch: pytest.MonkeyPatch,
    entrypoint_name: str,
) -> None:
    budgets = capture_alignment_budgets(monkeypatch)
    marker = "[noisegate: omitted 1 lines]"
    ref = "externalized_ref=dup"
    raw = "\n".join(
        [
            marker,
            *([ref] * 20),
            *[f"ValueError: diagnostic-{index}" for index in range(4_000)],
        ]
    )

    result = getattr(engine, entrypoint_name)(
        raw,
        command="pytest -q",
        exit_code=1,
        options=NoisegateOptions(
            max_chars=500_000,
            max_lines=22,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
        ),
    )

    assert result.changed is False
    assert result.text == raw
    assert len(raw.splitlines()) == 4_021
    assert len(budgets) == 1
    assert 1 < budgets[0].alignment_calls < 200
    assert budgets[0].exhausted is True
    assert budgets[0].spent <= budgets[0].limit
    assert engine._SOURCE_ALIGNMENT_WORK_BUDGET.get() is None


def test_direct_ranked_loop_cannot_mint_alignment_budget_per_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budgets = capture_alignment_budgets(monkeypatch, 1_000)
    marker = "[noisegate: omitted 1 lines]"
    ref = "externalized_ref=dup"
    raw = "\n".join(
        [
            marker,
            *([ref] * 20),
            *[f"ValueError: diagnostic-{index}" for index in range(100)],
        ]
    )

    best = engine._best_ranked_diagnostic_excerpt(
        before=raw,
        options=NoisegateOptions(
            max_chars=500_000,
            max_lines=22,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
        ),
        preserve_patterns=engine._preserve_patterns_for_output("pytest", raw),
    )

    assert best is None
    assert len(budgets) == 1
    assert budgets[0].exhausted is True
    assert engine._SOURCE_ALIGNMENT_WORK_BUDGET.get() is None


def test_non_exhausted_public_alignment_budget_still_compacts_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budgets = capture_alignment_budgets(monkeypatch)
    raw = "\n".join(
        [
            "externalized_ref=foo",
            "[noisegate: omitted 8 lines]",
            "ValueError: actionable boom",
            *[f"middle-{index}-" + ("x" * 80) for index in range(5)],
            "ERROR: generic transient noise",
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        exit_code=1,
        options=NoisegateOptions(
            max_chars=140,
            max_lines=50,
            head_lines=1,
            tail_lines=1,
            important_context_lines=0,
            max_important_lines=1,
        ),
    )

    assert result.changed is True
    assert result.text == "\n".join(
        [
            "externalized_ref=foo",
            "[noisegate: omitted 8 lines]",
            "ValueError: actionable boom",
            "[noisegate: omitted 6 lines]",
            "[noisegate: exit_code=1]",
        ]
    )
    assert len(budgets) == 1
    assert budgets[0].alignment_calls > 1
    assert 0 < budgets[0].spent < budgets[0].limit
    assert budgets[0].exhausted is False
    assert engine._SOURCE_ALIGNMENT_WORK_BUDGET.get() is None


def test_exhausted_public_alignment_budget_does_not_leak_to_next_reduction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budgets = capture_alignment_budgets(
        monkeypatch,
        1,
        engine._SOURCE_ALIGNMENT_WORK_LIMIT,
    )
    raw = "\n".join(
        [
            "head",
            "[noisegate: omitted 8 lines]",
            "middle",
            "tail",
        ]
    )
    reduction_options = NoisegateOptions(
        max_chars=10_000,
        max_lines=3,
        head_lines=1,
        tail_lines=1,
    )

    exhausted = reduce_text(raw, command="make noisy", options=reduction_options)
    independent = reduce_text(raw, command="make noisy", options=reduction_options)

    assert exhausted.changed is False
    assert exhausted.text == raw
    assert independent.changed is True
    assert independent.text == "head\n[noisegate: omitted 9 lines]\ntail"
    assert len(budgets) == 2
    assert budgets[0].exhausted is True
    assert budgets[1].exhausted is False
    assert budgets[0] is not budgets[1]
    assert engine._SOURCE_ALIGNMENT_WORK_BUDGET.get() is None


def test_top_level_reduction_isolates_inherited_alignment_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inherited = engine._SourceAlignmentWorkBudget(0)
    created = capture_alignment_budgets(monkeypatch)
    token = engine._SOURCE_ALIGNMENT_WORK_BUDGET.set(inherited)
    raw = "\n".join(
        [
            "head",
            "[noisegate: omitted 8 lines]",
            "middle",
            "tail",
        ]
    )
    try:
        result = reduce_text(
            raw,
            command="make noisy",
            options=NoisegateOptions(
                max_chars=10_000,
                max_lines=3,
                head_lines=1,
                tail_lines=1,
            ),
        )
        assert engine._SOURCE_ALIGNMENT_WORK_BUDGET.get() is inherited
    finally:
        engine._SOURCE_ALIGNMENT_WORK_BUDGET.reset(token)

    assert result.changed is True
    assert len(created) == 1
    assert inherited.spent == 0
    assert inherited.exhausted is False
    assert engine._SOURCE_ALIGNMENT_WORK_BUDGET.get() is None


def test_cached_alignment_does_not_spend_shared_budget_twice() -> None:
    budget = engine._SourceAlignmentWorkBudget(10_000)
    token = engine._SOURCE_ALIGNMENT_WORK_BUDGET.set(budget)
    source_lines = [
        "head",
        "[noisegate: omitted 8 lines]",
        "middle",
        "tail",
    ]
    excerpt = "head\n[noisegate: omitted 2 lines]\ntail"
    try:
        first = engine._source_line_indices_for_excerpt(source_lines, excerpt)
        spent_after_first = budget.spent
        second = engine._source_line_indices_for_excerpt(source_lines, excerpt)
    finally:
        engine._SOURCE_ALIGNMENT_WORK_BUDGET.reset(token)

    assert first == second == [0, 3]
    assert spent_after_first > 0
    assert budget.spent == spent_after_first
    assert budget.alignment_calls == 2
    assert engine._SOURCE_ALIGNMENT_WORK_BUDGET.get() is None


def test_generic_head_tail_aggregates_duplicate_upstream_line_omissions() -> None:
    raw = "\n".join(
        [
            "head",
            "[noisegate: omitted 4 lines]",
            "line-2",
            "line-3",
            "line-4",
            "line-5",
            "[noisegate: omitted 4 lines]",
            "tail",
        ]
    )

    result = reduce_text(
        raw,
        command="make noisy",
        options=options(
            max_chars=10_000,
            max_lines=3,
            head_lines=1,
            tail_lines=1,
        ),
    )

    assert result.changed is True
    assert result.text == "head\n[noisegate: omitted 12 lines]\ntail"
    assert engine._represented_line_coverage(result.text) == 14


def test_line_coverage_remap_keeps_fittable_ranked_failure() -> None:
    raw = "\n".join(
        [
            "FAILED tests/t.py::test_x - AssertionError: nope",
            *["[noisegate: omitted 8 lines]"] * 3,
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        options=NoisegateOptions(
            max_chars=80,
            max_lines=3,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
        ),
    )

    assert result.changed is True
    assert result.text == "\n".join(
        [
            "FAILED tests/t.py::test_x - AssertionError: nope",
            "[noisegate: omitted 24 lines]",
        ]
    )


@pytest.mark.parametrize(
    ("command", "command_class"),
    [
        ("pytest -q", "pytest"),
        ("npm test", "node"),
        ("docker build .", "docker_build"),
        ("make noisy", "generic"),
    ],
)
def test_line_coverage_remap_keeps_best_ranked_fittable_diagnostic(
    command: str,
    command_class: str,
) -> None:
    raw = "\n".join(
        [
            "[noisegate: omitted 8 lines]",
            "ValueError: actionable boom",
            *[f"middle-{index}-" + ("x" * 80) for index in range(5)],
            "ERROR: generic transient noise",
        ]
    )

    result = reduce_text(
        raw,
        command=command,
        exit_code=1,
        options=NoisegateOptions(
            max_chars=120,
            max_lines=4,
            head_lines=1,
            tail_lines=1,
            important_context_lines=0,
            max_important_lines=1,
        ),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == command_class
    assert result.text == "\n".join(
        [
            "[noisegate: omitted 8 lines]",
            "ValueError: actionable boom",
            "[noisegate: omitted 6 lines]",
            "[noisegate: exit_code=1]",
        ]
    )


@pytest.mark.parametrize(
    ("max_chars", "max_lines"),
    [(140, 50), (10_000, 5)],
    ids=("char_budget", "line_budget"),
)
def test_line_coverage_remap_keeps_lcm_ref_with_best_ranked_diagnostic(
    max_chars: int,
    max_lines: int,
) -> None:
    raw = "\n".join(
        [
            "externalized_ref=foo",
            "[noisegate: omitted 8 lines]",
            "ValueError: actionable boom",
            *[f"middle-{index}-" + ("x" * 80) for index in range(5)],
            "ERROR: generic transient noise",
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        exit_code=1,
        options=NoisegateOptions(
            max_chars=max_chars,
            max_lines=max_lines,
            head_lines=1,
            tail_lines=1,
            important_context_lines=0,
            max_important_lines=1,
        ),
    )

    assert result.changed is True
    assert result.text == "\n".join(
        [
            "externalized_ref=foo",
            "[noisegate: omitted 8 lines]",
            "ValueError: actionable boom",
            "[noisegate: omitted 6 lines]",
            "[noisegate: exit_code=1]",
        ]
    )


@pytest.mark.parametrize(
    ("max_chars", "max_lines"),
    [(140, 50), (10_000, 5)],
    ids=("char_budget_second_pass", "line_budget_second_pass"),
)
def test_duplicate_lcm_refs_and_diagnostics_preserve_multiplicity_and_position(
    max_chars: int,
    max_lines: int,
) -> None:
    raw = "\n".join(
        [
            "externalized_ref=dup",
            "ValueError: repeated",
            "[noisegate: omitted 5 lines]",
            *[f"middle-{index}-" + ("x" * 80) for index in range(5)],
            "ValueError: repeated",
            "externalized_ref=dup",
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        exit_code=1,
        options=NoisegateOptions(
            max_chars=max_chars,
            max_lines=max_lines,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
            max_important_lines=3,
        ),
    )

    assert result.changed is True
    assert result.text == "\n".join(
        [
            "externalized_ref=dup",
            "ValueError: repeated",
            "[noisegate: omitted 11 lines]",
            "externalized_ref=dup",
            "[noisegate: exit_code=1]",
        ]
    )
    assert result.text.splitlines().count("externalized_ref=dup") == 2
    body = "\n".join(result.text.splitlines()[:-1])
    assert engine._represented_line_coverage(body) == engine._represented_line_coverage(raw)


def test_duplicate_diagnostic_text_preserves_best_source_occurrence() -> None:
    source_lines = [
        "externalized_ref=dup",
        "ValueError: repeated",
        "[noisegate: omitted 5 lines]",
        "noise",
        "ValueError: repeated",
        "externalized_ref=dup",
    ]
    raw = "\n".join(source_lines)
    later_marked = engine._marked_excerpt_for_line_indices(source_lines, [0, 4, 5])
    assert later_marked is not None
    later_excerpt = engine._remark_excerpt_with_line_coverage(raw, later_marked)
    assert later_excerpt is not None
    preserve_patterns = engine._preserve_patterns_for_output("pytest", raw)
    remap_options = NoisegateOptions(
        max_chars=200,
        max_lines=6,
        head_lines=0,
        tail_lines=0,
        important_context_lines=0,
    )

    ensured = engine._ensure_ranked_diagnostic_after_line_coverage_remap(
        before=raw,
        shortened=later_excerpt,
        options=remap_options,
        preserve_patterns=preserve_patterns,
    )

    assert ensured == "\n".join(
        [
            "externalized_ref=dup",
            "ValueError: repeated",
            "[noisegate: omitted 7 lines]",
            "externalized_ref=dup",
        ]
    )
    assert engine._source_line_indices_for_excerpt(source_lines, ensured) == [0, 1, 5]
    exit_notice = "[noisegate: exit_code=1]"
    assert engine._line_coverage_remap_dropped_ranked_diagnostic(
        before=raw,
        after=f"{later_excerpt}\n{exit_notice}",
        options=NoisegateOptions(
            max_chars=240,
            max_lines=7,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
        ),
        preserve_patterns=preserve_patterns,
        required_notices=[exit_notice],
    ) is True


def test_line_coverage_remap_keeps_reducer_specific_anchor_with_lcm_ref() -> None:
    raw = "\n".join(
        [
            "externalized_ref=foo",
            " M important.py",
            "[noisegate: omitted 1 lines]",
            "xxxx",
            "[noisegate: omitted 2 lines]",
            "yyyy",
            "[noisegate: omitted 20 lines]",
            "zzzz",
        ]
    )

    result = reduce_text(
        raw,
        command="git status --short",
        exit_code=1,
        options=NoisegateOptions(
            max_chars=91,
            max_lines=4,
            max_important_lines=2,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
        ),
    )

    assert result.changed is True
    assert result.text == "\n".join(
        [
            "externalized_ref=foo",
            " M important.py",
            "[noisegate: omitted 26 lines]",
            "[noisegate: exit_code=1]",
        ]
    )
    assert len(result.text) == 91
    body = "\n".join(result.text.splitlines()[:-1])
    assert engine._represented_line_coverage(body) == engine._represented_line_coverage(raw)


def test_generated_char_marker_cannot_impersonate_upstream_marker() -> None:
    raw = "HEAD-a\n[noisegate: omitted 32 chars]\nb-TAIL"

    result = reduce_text(
        raw,
        command="pytest -q",
        options=options(
            max_chars=42,
            max_lines=50,
            head_lines=50,
            tail_lines=50,
        ),
    )

    assert result.changed is False
    assert result.text == raw


def test_duplicate_upstream_char_markers_fail_open_when_they_cannot_fit() -> None:
    notice = "[noisegate: omitted 32 chars]"
    raw = "\n".join(
        [
            "head-" + ("h" * 40),
            notice,
            "middle-1-" + ("x" * 40),
            "middle-2-" + ("x" * 40),
            notice,
            "tail-" + ("t" * 40),
        ]
    )

    result = reduce_text(
        raw,
        command="make noisy",
        options=options(
            max_chars=10_000,
            max_lines=3,
            head_lines=1,
            tail_lines=1,
        ),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.text.splitlines().count(notice) == 2


def test_final_budget_fails_open_when_upstream_char_marker_cannot_fit() -> None:
    raw = "HEAD-a\n[noisegate: omitted 32 chars]\nb-TAIL"

    rewrite = engine._enforce_final_budget(
        raw,
        options(
            max_chars=42,
            max_lines=50,
            head_lines=50,
            tail_lines=50,
        ),
        preserve_patterns=engine.CRITICAL_PATTERNS,
    )

    assert rewrite is None


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


@pytest.mark.parametrize(
    ("command", "expected_class"),
    (
        ("pytest -q --diagnostics lint", "pytest"),
        ("npm run lint -- --diagnostics", "node"),
        ("uv pip install diagnostics-lint", "python_package"),
    ),
)
def test_diagnostic_words_do_not_steal_existing_classifier_precedence(
    command: str,
    expected_class: str,
) -> None:
    output = "\n".join(
        [
            "diagnostics lint started",
            *(f"progress {index:03d}" for index in range(100)),
            "ERROR: diagnostics lint failed",
        ]
    )

    result = reduce_text(output, command=command, options=options(max_chars=180))

    assert result.metadata["command_class"] == expected_class
    assert result.metadata["reducer"] == expected_class


def test_diagnostic_locations_do_not_change_unscoped_generic_reduction() -> None:
    output = "\n".join(
        [
            "generic head",
            *(f"progress {index:03d}" for index in range(80)),
            "src/service.py:42:17: F401 imported but unused",
            *(f"progress {index:03d}" for index in range(80, 160)),
            "generic tail",
        ]
    )

    result = reduce_text(
        output,
        command="make noisy",
        exit_code=1,
        options=options(
            max_chars=180,
            max_lines=6,
            head_lines=1,
            tail_lines=1,
        ),
    )

    assert result.metadata["command_class"] == "generic"
    assert result.metadata["reducer"] == "generic_head_tail"
    assert "src/service.py:42:17" not in result.text


@pytest.mark.parametrize(
    "command",
    (
        "lcm_grep retrieval",
        "lcm_load_session abc123",
        "lcm_describe 42",
        "./lcm_expand 123",
        "lcm_expand_query retrieval",
        "/usr/local/bin/hindsight_recall foo",
        "hindsight_reflect retrieval",
        "/opt/venv/bin/session_search query",
        "hermes lcm grep retrieval",
        "hermes lcm load-session abc123",
        "hermes lcm load session abc123",
        "hermes lcm describe 42",
        "hermes lcm expand 123",
        "session_search git diff",
        "hermes lcm expand git diff",
        "hermes 2>/dev/null lcm expand 123",
        "hermes lcm expand 123 <<<ignored",
        "hermes lcm expand 123 <<< ignored",
        "hermes lcm expand 123 <<<\"ignored value\"",
        "hermes lcm expand 123 <<< \"ignored value\"",
        "exec /opt/bin/lcm_expand 123",
        "exec -a hermes /opt/hermes/bin/hermes --profile work lcm expand 123",
        "exec -aretrieval /opt/bin/lcm_expand 123",
        "exec -l /opt/bin/lcm_expand 123",
        "exec -cl /opt/bin/lcm_expand 123",
        "exec -cla retrieval /opt/bin/lcm_expand 123",
        "echo `echo \\`/opt/bin/session_search q\\``",
        "printf '%s' \"$(hermes lcm expand 123)\"",
        "printf '%s' \"$(value=`hermes lcm expand 123`; printf '%s' \"$value\")\"",
        "hermes lcm expand-query retrieval",
        "hermes lcm expand query retrieval",
        "hermes hindsight recall retrieval",
        "hermes hindsight reflect retrieval",
        "hermes memory search retrieval",
        "hermes memory recall retrieval",
        "hermes memory reflect retrieval",
        "hermes memory get memory-id",
        "hermes memory read memory-id",
        "hermes memory show memory-id",
        "hermes memory list",
        "hermes session search retrieval",
    ),
)
def test_terminal_memory_retrieval_commands_are_exact(command: str) -> None:
    raw = numbered("retrieval evidence", 100)

    result = reduce_text(
        raw,
        command=command,
        tool_name="terminal",
        options=options(max_chars=120),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["command_class"] == "memory_retrieval"
    assert result.metadata["reducer"] == "protected_memory_retrieval"
    assert result.metadata["reason"] == "memory_retrieval_passthrough"


@pytest.mark.parametrize(
    "command",
    (
        "hermes --profile work lcm expand 123",
        "hermes --profile=work lcm expand 123",
        "hermes -p work lcm expand 123",
        "hermes -pwork lcm expand 123",
        "hermes --yolo --profile work lcm expand 123",
        "hermes --pass-session-id lcm expand 123",
        "hermes --ignore-user-config lcm expand 123",
        "hermes --ignore-rules lcm expand 123",
        "hermes --tui lcm expand 123",
        "hermes --cli lcm expand 123",
        "hermes --dev --tui lcm expand 123",
        "hermes --worktree lcm expand 123",
        "hermes -w lcm expand 123",
        "hermes --safe-mode lcm expand 123",
        "hermes --accept-hooks lcm expand 123",
        "hermes --no-restore-cwd lcm expand 123",
        "hermes --resume session-123 lcm expand 123",
        "hermes --resume=session-123 lcm expand 123",
        "hermes -rsession-123 lcm expand 123",
        "hermes --continue=session-name lcm expand 123",
        "hermes -csession-name lcm expand 123",
        "hermes --continue --yolo lcm expand 123",
        "hermes --model gpt-5.6-sol lcm expand 123",
        "hermes --provider openai-codex lcm expand 123",
        "hermes --toolsets web,terminal lcm expand 123",
        "hermes --skills noisegate lcm expand 123",
        "hermes -snoisegate lcm expand 123",
        "hermes --usage-file /tmp/hermes-usage.json lcm expand 123",
        "/opt/hermes/bin/hermes --profile work lcm expand 123",
        "exec /opt/hermes/bin/hermes --profile work 2>/dev/null lcm expand 123 <<<ignored",
        "printf '%s' \"$(hermes --profile work lcm expand 123)\"",
    ),
)
def test_profiled_hermes_memory_retrieval_commands_are_exact(command: str) -> None:
    raw = numbered("profiled retrieval evidence", 100)

    result = reduce_text(
        raw,
        command=command,
        tool_name="terminal",
        options=options(max_chars=120),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["command_class"] == "memory_retrieval"
    assert result.metadata["reducer"] == "protected_memory_retrieval"


@pytest.mark.parametrize(
    ("command", "expected_class", "expected_reducer"),
    (
        ("rg lcm expand src", "source_search", "protected_source_search"),
        ("grep hindsight recall logs", "source_search", "protected_source_search"),
        ("printf session_search", "generic", "generic_head_tail"),
        ("echo 'hermes lcm expand'", "generic", "generic_head_tail"),
        ("echo '$(hermes lcm expand 123)'", "generic", "generic_head_tail"),
        ("echo '`hermes lcm expand 123`'", "generic", "generic_head_tail"),
        ("git diff session_search", "git_diff", "protected_diff"),
        ("pytest -q -k 'hindsight recall'", "pytest", "pytest"),
    ),
)
def test_retrieval_phrases_in_arguments_keep_existing_behavior(
    command: str,
    expected_class: str,
    expected_reducer: str,
) -> None:
    raw = numbered("ordinary output", 100)

    result = reduce_text(
        raw,
        command=command,
        tool_name="terminal",
        options=options(max_chars=120),
    )

    assert result.metadata["command_class"] == expected_class
    assert result.metadata["reducer"] == expected_reducer
    assert result.changed is (expected_class not in {"git_diff", "source_search"})


def test_nested_retrieval_substitution_inspection_fails_open_at_bound() -> None:
    raw = numbered("possibly retrieved evidence", 100)
    command = "hermes lcm expand 123"
    for _ in range(engine._MEMORY_RETRIEVAL_SUBSTITUTION_LIMIT + 1):
        command = f'printf \'%s\' "$({command})"'

    result = reduce_text(
        raw,
        command=command,
        tool_name="terminal",
        options=options(max_chars=120),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["command_class"] == "memory_retrieval"


def test_nested_retrieval_substitution_inspection_deduplicates_bodies() -> None:
    raw = numbered("ordinary output", 100)
    repeated = " ".join(
        "$(printf ordinary)"
        for _ in range(engine._MEMORY_RETRIEVAL_SUBSTITUTION_LIMIT + 1)
    )

    result = reduce_text(
        raw,
        command=f'printf \'%s\' "{repeated}"',
        tool_name="terminal",
        options=options(max_chars=120),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == "generic"


@pytest.mark.parametrize(
    "command",
    (
        "session_search git diff",
        "hermes lcm expand git diff",
    ),
)
def test_retrieval_executable_intent_beats_diff_like_query_and_output(command: str) -> None:
    raw = "\n".join(
        [
            "diff --git a/recalled.py b/recalled.py",
            "--- a/recalled.py",
            "+++ b/recalled.py",
            "@@ -1,2 +1,2 @@",
            "-old recalled evidence",
            "+new recalled evidence",
            *[f"+exact recalled diff line {index:03d}" for index in range(120)],
        ]
    )

    result = reduce_text(
        raw,
        command=command,
        tool_name="terminal",
        options=options(max_chars=160, preserve_diffs=False),
    )

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["command_class"] == "memory_retrieval"
    assert result.metadata["reducer"] == "protected_memory_retrieval"


@pytest.mark.parametrize(
    ("command", "line"),
    (
        ("hermes lcm import archive.jsonl", "indexed message"),
        ("hermes lcm doctor --reindex", "vector index progress"),
        ("python embed.py --batch-size 512", "embedding batch complete"),
        ("python api_worker.py", "API retry rate-limit backoff"),
    ),
)
def test_memory_maintenance_commands_remain_compactable(command: str, line: str) -> None:
    raw = numbered(line, 100)

    result = reduce_text(
        raw,
        command=command,
        tool_name="terminal",
        options=options(max_chars=160),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == "generic"
    assert result.metadata["reducer"] == "generic_head_tail"


@pytest.mark.parametrize(
    "command",
    (
        "hermes --profile work lcm import archive.jsonl",
        "hermes -p work lcm doctor --reindex",
        "hermes --profile lcm expand 123",
        "hermes chat -q 'lcm expand 123'",
        "hermes --profile --yolo lcm expand 123",
        "hermes --profile= lcm expand 123",
        "hermes -p --yolo lcm expand 123",
        "hermes -p",
        "hermes --profile=bad:name lcm expand 123",
        "hermes -p-work lcm expand 123",
        "hermes --profile 'bad name' lcm expand 123",
        "hermes --profile=abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyzabcdefghijklm "
        "lcm expand 123",
        "hermes --profile \u017f lcm expand 123",
        "hermes --profile \u0131 lcm expand 123",
        "hermes --profile \u0130 lcm expand 123",
        "hermes --resume lcm expand 123",
        "hermes --resume --yolo lcm expand 123",
        "hermes --continue lcm expand 123",
        "hermes --model --yolo lcm expand 123",
        "hermes --unknown-global lcm expand 123",
        "hermes --version lcm expand 123",
    ),
)
def test_profile_syntax_without_retrieval_intent_remains_compactable(command: str) -> None:
    raw = numbered("ordinary Hermes output", 100)

    result = reduce_text(
        raw,
        command=command,
        tool_name="terminal",
        options=options(max_chars=160),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == "generic"
    assert result.metadata["reducer"] == "generic_head_tail"


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
    assert result.text == "l00\n[noisegate: omitted 99 lines]"
    assert len(result.text) == 33


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
            # Prefix marker + interior signal + suffix marker + exit notice.
            max_lines=4,
            head_lines=0,
            tail_lines=0,
            max_important_lines=10,
            important_context_lines=0,
        ),
    )

    assert result.changed is True
    assert "TypeError: unsupported operand type" in result.text
    assert "1 failed in 0.12s" not in result.text


def test_line_budget_uses_marked_summary_when_progress_needs_two_markers() -> None:
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
    assert "tests/test_widget.py::test_widget FAILED" not in result.text
    assert "1 failed in 0.12s" in result.text
    assert "[noisegate: omitted 21 lines]" in result.text
    assert "[noisegate: exit_code=1]" in result.text


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
            # The four truthful lines occupy exactly 125 characters.
            max_chars=125,
            max_lines=4,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
        ),
    )

    assert result.changed is True
    assert len(result.text) <= 125
    assert len(result.text.splitlines()) <= 4
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
            # Prefix marker + interior signal + suffix marker + exit notice.
            max_lines=4,
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
            # Prefix marker + interior signal + suffix marker + exit notice.
            max_lines=4,
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
            # Prefix marker + interior signal + suffix marker + exit notice.
            max_lines=4,
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


def test_line_budget_preserves_task_exception_headers() -> None:
    headers = (
        "Task exception was never retrieved",
        "Exception was never retrieved",
        "During handling of the above exception, another exception occurred:",
        "The above exception was the direct cause of the following exception:",
    )

    for header in headers:
        raw = "\n".join(
            [
                *[f"setup {index}" for index in range(20)],
                header,
                *[f"noise {index}" for index in range(20)],
                "========================= 1 failed in 0.12s =========================",
            ]
        )

        result = reduce_text(
            raw,
            command="pytest -vv",
            exit_code=1,
            options=options(
                max_chars=10_000,
                # Prefix marker + interior signal + suffix marker + exit notice.
                max_lines=4,
                head_lines=0,
                tail_lines=0,
                important_context_lines=0,
            ),
        )

        assert result.changed is True
        assert header in result.text
        assert "1 failed in 0.12s" not in result.text


def test_multi_anchor_recovery_budget_does_not_silently_drop_exact_repro_tail() -> None:
    raw = "\n".join(
        [
            "1 failed",
            "BaseExceptionGroup: boom",
            "Task exception was never retrieved",
            *[f"q{index}" for index in range(11)],
        ]
    )

    transformed = transform_terminal_output(
        command="pytest -q",
        output=raw,
        exit_code=1,
        noisegate_max_chars=160,
        noisegate_max_lines=6,
        noisegate_head_lines=5,
        noisegate_tail_lines=2,
        noisegate_important_context_lines=2,
        noisegate_max_important_lines=4,
    )

    assert_fail_open_or_truthful_failure_excerpt(raw, transformed)


def test_tight_failed_test_fallback_marks_every_omitted_range_exact_repro() -> None:
    raw = "\n".join(
        [
            "1 failed",
            "FAILED tests/test_x.py::test_y - AssertionError: boom",
            "n2xxxxxxxxxx",
            "n3xxxxxxxxxx",
            "n4xxxxxxxxxx",
            "Task exception was never retrieved",
            "n6",
        ]
    )

    transformed = transform_terminal_output(
        command="pytest -q",
        output=raw,
        exit_code=1,
        noisegate_max_chars=180,
        noisegate_max_lines=4,
        noisegate_head_lines=4,
        noisegate_tail_lines=3,
        noisegate_important_context_lines=1,
        noisegate_max_important_lines=9,
    )

    assert transformed == "\n".join(
        [
            "[noisegate: omitted 1 lines]",
            "FAILED tests/test_x.py::test_y - AssertionError: boom",
            "[noisegate: omitted 5 lines]",
            "[noisegate: exit_code=1]",
        ]
    )
    assert len(transformed) == 136


@pytest.mark.parametrize("max_lines", [3, 4, 5])
@pytest.mark.parametrize("max_chars", [135, 136, 180])
@pytest.mark.parametrize(
    "secondary_header",
    [
        "Task exception was never retrieved",
        "BaseExceptionGroup: secondary boom",
        "During handling of the above exception, another exception occurred:",
        "The above exception was the direct cause of the following exception:",
    ],
)
def test_tight_failed_test_fallback_boundaries_are_truthfully_marked_or_fail_open(
    max_lines: int,
    max_chars: int,
    secondary_header: str,
) -> None:
    raw = "\n".join(
        [
            "1 failed",
            "FAILED tests/test_x.py::test_y - AssertionError: boom",
            "n2xxxxxxxxxx",
            "n3xxxxxxxxxx",
            "n4xxxxxxxxxx",
            secondary_header,
            "n6",
        ]
    )

    transformed = transform_terminal_output(
        command="pytest -q",
        output=raw,
        exit_code=1,
        noisegate_max_chars=max_chars,
        noisegate_max_lines=max_lines,
        noisegate_head_lines=4,
        noisegate_tail_lines=3,
        noisegate_important_context_lines=1,
        noisegate_max_important_lines=9,
    )

    assert_fail_open_or_truthful_failure_excerpt(raw, transformed)
    if transformed is not None and transformed != raw:
        assert len(transformed) <= max_chars
        assert len(transformed.splitlines()) <= max_lines


@pytest.mark.parametrize(
    ("lines", "max_chars"),
    [
        (["prefix", "ValueError: boom", "tail"], len("ValueError: boom")),
        (
            [
                "prefix",
                "FAILED tests/test_x.py::test_y - AssertionError: boom",
                "ValueError: boom",
                "tail",
            ],
            len("FAILED tests/test_x.py::test_y - AssertionError: boom\nValueError: boom"),
        ),
    ],
    ids=("single_anchor", "contiguous_anchors"),
)
def test_concrete_failure_fallback_rejects_unmarked_interior_subset(
    lines: list[str],
    max_chars: int,
) -> None:
    excerpt = engine._concrete_failure_excerpt_for_notices(
        "\n".join(lines),
        options(max_chars=max_chars, max_lines=2),
    )

    assert excerpt is None


def test_concrete_failure_direct_fit_source_remains_unmarked() -> None:
    raw = "\n".join(
        [
            "FAILED tests/test_x.py::test_y - AssertionError: boom",
            "ValueError: boom",
        ]
    )

    excerpt = engine._concrete_failure_excerpt_for_notices(
        raw,
        options(max_chars=len(raw), max_lines=2),
    )

    assert excerpt == raw
    assert "[noisegate: omitted" not in excerpt


@pytest.mark.parametrize("max_lines", [5, 6, 7])
@pytest.mark.parametrize("max_chars", [120, 160])
def test_multi_anchor_recovery_budget_respects_adjacent_boundaries(
    max_lines: int,
    max_chars: int,
) -> None:
    raw = "\n".join(
        [
            "1 failed",
            "BaseExceptionGroup: boom",
            "Task exception was never retrieved",
            *[f"q{index}" for index in range(11)],
        ]
    )

    transformed = transform_terminal_output(
        command="pytest -q",
        output=raw,
        exit_code=1,
        noisegate_max_chars=max_chars,
        noisegate_max_lines=max_lines,
        noisegate_head_lines=5,
        noisegate_tail_lines=2,
        noisegate_important_context_lines=2,
        noisegate_max_important_lines=4,
    )

    assert_fail_open_or_truthful_failure_excerpt(raw, transformed)
    if transformed is not None:
        assert len(transformed) <= max_chars
        assert len(transformed.splitlines()) <= max_lines


@pytest.mark.parametrize(
    "secondary_header",
    [
        "Task exception was never retrieved",
        "During handling of the above exception, another exception occurred:",
        "The above exception was the direct cause of the following exception:",
    ],
)
def test_multi_anchor_exception_headers_do_not_silently_drop_tail(
    secondary_header: str,
) -> None:
    raw = "\n".join(
        [
            "1 failed",
            "BaseExceptionGroup: boom",
            secondary_header,
            *[f"tail-{index}" for index in range(11)],
        ]
    )

    transformed = transform_terminal_output(
        command="pytest -q",
        output=raw,
        exit_code=1,
        noisegate_max_chars=160,
        noisegate_max_lines=6,
        noisegate_head_lines=5,
        noisegate_tail_lines=2,
        noisegate_important_context_lines=2,
        noisegate_max_important_lines=4,
    )

    assert_fail_open_or_truthful_failure_excerpt(raw, transformed)


def test_multi_anchor_direct_fit_without_omitted_range_remains_unmarked() -> None:
    lines = [
        "BaseExceptionGroup: boom",
        "Task exception was never retrieved",
    ]

    excerpt = engine._line_budgeted_important_excerpt(
        lines,
        list(range(len(lines))),
        options(
            max_chars=160,
            max_lines=4,
            important_context_lines=0,
        ),
        engine.TEST_PATTERNS,
    )

    assert excerpt == "\n".join(lines)
    assert "[noisegate: omitted" not in excerpt


def test_line_budget_prefers_chained_exception_header_over_adjacent_e_lines() -> None:
    raw = "\n".join(
        [
            *[f"setup {index}" for index in range(20)],
            "E       ValueError: first failure",
            "During handling of the above exception, another exception occurred:",
            "E       RuntimeError: second failure",
            *[f"noise {index}" for index in range(20)],
            "========================= 1 failed in 0.12s =========================",
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        exit_code=1,
        options=options(
            max_chars=10_000,
            # Prefix marker + interior signal + suffix marker + exit notice.
            max_lines=4,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
        ),
    )

    assert result.changed is True
    assert "During handling of the above exception" in result.text
    assert "1 failed in 0.12s" not in result.text


def test_char_budget_prefers_chained_exception_header_at_truthful_fit() -> None:
    raw = "\n".join(
        [
            *[f"setup {index}" for index in range(20)],
            "ValueError: first failure",
            "During handling of the above exception, another exception occurred:",
            "RuntimeError: second failure",
            *[f"noise {index}" for index in range(20)],
            "========================= 1 failed in 0.12s =========================",
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        exit_code=1,
        options=options(
            max_chars=152,
            max_lines=80,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
        ),
    )

    assert result.changed is True
    assert "During handling of the above exception" in result.text
    assert "ValueError: first failure" not in result.text


def test_line_budget_ranks_base_exception_group_as_detail() -> None:
    raw = "\n".join(
        [
            *[f"setup {index}" for index in range(20)],
            "BaseExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)",
            *[f"noise {index}" for index in range(20)],
            "FAILED tests/test_widget.py::test_widget - BaseExceptionGroup",
            *[f"teardown {index}" for index in range(20)],
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        exit_code=1,
        options=options(
            max_chars=10_000,
            # Prefix marker + interior signal + suffix marker + exit notice.
            max_lines=4,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
        ),
    )

    assert result.changed is True
    assert "BaseExceptionGroup: unhandled errors" in result.text
    assert "FAILED tests/test_widget.py::test_widget" not in result.text


def test_base_exception_group_header_ranks_above_pytest_summary() -> None:
    summary = "FAILED tests/test_widget.py::test_widget - BaseExceptionGroup"
    header = "BaseExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)"

    assert engine._failure_detail_sort_key(header, 1) < engine._failure_detail_sort_key(
        summary,
        0,
    )


def test_char_budget_prefers_base_exception_group_header_over_earlier_summary() -> None:
    raw = "\n".join(
        [
            "FAILED tests/test_widget.py::test_widget - BaseExceptionGroup",
            *[f"noise {index}" for index in range(20)],
            "BaseExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)",
            *[f"teardown {index}" for index in range(20)],
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        exit_code=1,
        options=options(
            # The two markers, header, and exit notice occupy exactly 154 characters.
            max_chars=154,
            max_lines=80,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
        ),
    )

    assert result.changed is True
    assert "BaseExceptionGroup: unhandled errors" in result.text
    assert "FAILED tests/test_widget.py::test_widget" not in result.text


def test_exception_group_tree_header_ranks_above_pytest_summary() -> None:
    raw = "\n".join(
        [
            "FAILED tests/test_widget.py::test_widget - BaseExceptionGroup",
            *[f"noise {index}" for index in range(20)],
            "  | BaseExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)",
            *[f"teardown {index}" for index in range(20)],
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -vv",
        exit_code=1,
        options=options(
            # The indented header makes the truthful four-line excerpt exactly 158 chars.
            max_chars=158,
            max_lines=80,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
        ),
    )

    assert result.changed is True
    assert "| BaseExceptionGroup: unhandled errors" in result.text
    assert "FAILED tests/test_widget.py::test_widget" not in result.text


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
            # The two markers, resolver signal, and exit notice are exactly 135 chars.
            max_chars=135,
            max_lines=4,
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
            if max_lines == 3:
                # Even one interior line needs two omission markers plus the exit notice.
                assert result.changed is False, case
                assert result.text == raw, case
                continue
            assert result.changed is True, case
            assert result.metadata["reducer"] == reducer, case
            assert "[noisegate: exit_code=1]" in result.text, case
            if max_lines == 4:
                # Keeping both signals needs two markers + two signals + the exit notice.
                assert (
                    "ModuleNotFoundError: No module named missing_pkg" in result.text
                    or "FAILED tests/test_demo.py::test_signal" in result.text
                ), case
                continue
            assert "ModuleNotFoundError: No module named missing_pkg" in result.text, case
            assert "FAILED tests/test_demo.py::test_signal" in result.text, case


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
            # Prefix marker + interior signal + suffix marker + exit notice.
            max_lines=4,
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


@pytest.mark.parametrize("unit", ["lines", "chars"])
def test_recovery_shortening_fails_open_when_diagnostic_and_exit_cannot_fit(
    unit: str,
) -> None:
    notice = f"[noisegate: omitted 123 {unit}]"
    raw = "\n".join(
        [
            notice,
            *[f"pre-{index}" for index in range(8)],
            "ValueError: boom",
            *[f"post-{index}" for index in range(8)],
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        exit_code=1,
        options=options(
            max_chars=80,
            max_lines=10,
            head_lines=1,
            tail_lines=1,
            important_context_lines=0,
        ),
    )

    assert result.changed is False
    assert result.text == raw


def test_failed_output_remaps_upstream_line_coverage_and_keeps_exit_notice() -> None:
    raw = "\n".join(
        [
            "pre-0-" + ("x" * 30),
            "pre-1-" + ("x" * 30),
            "[noisegate: omitted 8 lines]",
            "middle-0-" + ("x" * 30),
            "middle-1-" + ("x" * 30),
            "ValueError: boom",
            *[f"tail-{index}-" + ("x" * 30) for index in range(4)],
        ]
    )

    result = reduce_text(
        raw,
        command="pytest -q",
        exit_code=1,
        options=options(
            max_chars=10_000,
            max_lines=6,
            head_lines=1,
            tail_lines=1,
            important_context_lines=0,
        ),
    )

    body = "\n".join(
        line
        for line in result.text.splitlines()
        if line != "[noisegate: exit_code=1]"
    )
    assert result.changed is True
    assert "ValueError: boom" in result.text.splitlines()
    assert "[noisegate: exit_code=1]" in result.text.splitlines()
    assert engine._represented_line_coverage(body) == engine._represented_line_coverage(raw)


def test_upstream_omission_evidence_may_compact_when_exit_notice_already_fits() -> None:
    for prefix in ([], ["externalized_ref=foo"]):
        for unit in ("lines", "chars"):
            notice = f"[noisegate: omitted 3 {unit}]"
            raw = "\n".join(
                [
                    *prefix,
                    notice,
                    "ValueError: boom",
                    *[f"post-{index}" for index in range(12)],
                ]
            )

            result = reduce_text(
                raw,
                command="pytest -q",
                exit_code=1,
                options=options(
                    max_chars=130,
                    max_lines=7,
                    head_lines=1,
                    tail_lines=1,
                    important_context_lines=0,
                ),
            )

            assert result.changed is True
            assert result.text.splitlines().count(notice) == 1
            assert "[noisegate: exit_code=1]" in result.text.splitlines()


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


def test_artifact_path_does_not_turn_char_marker_collision_into_a_gain(tmp_path) -> None:
    raw = "HEAD-a\n[noisegate: omitted 32 chars]\nb-TAIL"
    artifact_dir = tmp_path / "artifacts"

    result = reduce_text(
        raw,
        command="pytest -q",
        options=options(
            max_chars=42,
            max_lines=50,
            head_lines=50,
            tail_lines=50,
            artifact_enabled=True,
            artifact_dir=artifact_dir,
        ),
    )

    assert result.changed is False
    assert result.text == raw
    assert not artifact_dir.exists()


def test_duplicate_tail_remap_survives_exit_and_artifact_notice_path(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budgets = capture_alignment_budgets(monkeypatch)
    spent_when_stored: list[int] = []
    store_artifact = engine._store_artifact

    def tracked_store_artifact(
        text: str,
        artifact_options: NoisegateOptions,
    ) -> dict[str, engine.JsonValue]:
        budget = engine._SOURCE_ALIGNMENT_WORK_BUDGET.get()
        assert budget is not None
        spent_when_stored.append(budget.spent)
        return store_artifact(text, artifact_options)

    monkeypatch.setattr(engine, "_store_artifact", tracked_store_artifact)
    raw = "\n".join(
        [
            "same",
            "[noisegate: omitted 100 lines]",
            *[f"filler-{index}-" + ("x" * 80) for index in range(5)],
            "same",
        ]
    )

    result = reduce_text(
        raw,
        command="make noisy",
        exit_code=7,
        options=options(
            max_chars=400,
            max_lines=4,
            head_lines=0,
            tail_lines=1,
            artifact_enabled=True,
            artifact_dir=tmp_path,
        ),
    )

    output_lines = result.text.splitlines()
    assert result.changed is True
    assert output_lines[:3] == [
        "[noisegate: omitted 106 lines]",
        "same",
        "[noisegate: exit_code=7]",
    ]
    assert output_lines[3].startswith("[noisegate artifact: id=")
    assert engine._represented_line_coverage("\n".join(output_lines[:2])) == 107
    artifact = result.metadata["artifact"]
    assert isinstance(artifact, dict)
    artifact_id = artifact["id"]
    assert isinstance(artifact_id, str)
    assert engine.ArtifactStore(tmp_path).read(artifact_id) == raw
    assert len(budgets) == 1
    assert spent_when_stored == [budgets[0].spent]
    assert budgets[0].alignment_calls > 1
    assert budgets[0].exhausted is False


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


@pytest.mark.parametrize(
    ("command", "long_signal", "short_signal"),
    [
        ("pytest -q", "ValueError: " + ("x" * 30), "TypeError: short"),
        ("npm test", "ValueError: " + ("x" * 30), "TypeError: short"),
        ("docker build .", "ValueError: " + ("x" * 30), "TypeError: short"),
    ],
)
@pytest.mark.parametrize(
    ("max_chars", "max_lines"),
    [(80, 50), (10_000, 3)],
    ids=("max_chars", "max_lines"),
)
def test_lcm_priority_uses_shorter_signal_when_marked_excerpt_fits(
    command: str,
    long_signal: str,
    short_signal: str,
    max_chars: int,
    max_lines: int,
) -> None:
    raw = "\n".join(
        [
            "externalized_ref=foo",
            *[f"filler-{index}" for index in range(10)],
            long_signal,
            "filler-last",
            short_signal,
        ]
    )

    result = reduce_text(
        raw,
        command=command,
        options=options(
            max_chars=max_chars,
            max_lines=max_lines,
            head_lines=0,
            tail_lines=0,
            important_context_lines=0,
            max_important_lines=2,
        ),
    )

    assert result.changed is True
    assert result.text == "\n".join(
        [
            "externalized_ref=foo",
            "[noisegate: omitted 12 lines]",
            short_signal,
        ]
    )


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


def test_marked_excerpt_second_budget_pass_preserves_exact_repro_coverage() -> None:
    raw = "\n".join(
        [
            "tests/test_x.py::test_y FAILED [100%]",
            "f61-1",
            "BaseExceptionGroup: boom",
            "Task exception was never retrieved",
            "f61-4",
            "f61-5",
            "FAILED tests/test_x.py::test_y - AssertionError: boom",
        ]
    )

    result = reduce_text(
        raw,
        command="python noisy.py",
        exit_code=0,
        options=NoisegateOptions(
            max_chars=138,
            max_lines=6,
            head_lines=4,
            tail_lines=4,
            important_context_lines=2,
            max_important_lines=10,
        ),
    )

    assert engine._represented_line_coverage(result.text) == len(raw.splitlines())
    if result.changed:
        assert len(result.text) <= 138
        assert len(result.text.splitlines()) <= 6


@pytest.mark.parametrize(
    ("max_chars", "max_lines"),
    [(10_000, 5), (138, 6)],
    ids=("line_cap", "char_cap"),
)
def test_marked_excerpt_budget_rewrite_requires_equal_represented_coverage(
    max_chars: int,
    max_lines: int,
) -> None:
    marked = "\n".join(
        [
            "tests/test_x.py::test_y FAILED [100%]",
            "f61-1",
            "BaseExceptionGroup: boom",
            "Task exception was never retrieved",
            "[noisegate: omitted 2 lines]",
            "FAILED tests/test_x.py::test_y - AssertionError: boom",
        ]
    )
    rewrite = engine._enforce_final_budget(
        marked,
        NoisegateOptions(
            max_chars=max_chars,
            max_lines=max_lines,
            head_lines=4,
            tail_lines=4,
            important_context_lines=2,
            max_important_lines=10,
        ),
        preserve_patterns=engine.CRITICAL_PATTERNS,
    )

    assert rewrite is None or (
        engine._represented_line_coverage(rewrite)
        == engine._represented_line_coverage(marked)
    )


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
            # Prefix marker + interior signal + suffix marker + exit notice.
            max_lines=4,
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
        # This stage's two markers, npm signal, and exit notice are exactly 122 chars.
        options=options(max_chars=122, max_lines=80, head_lines=0, tail_lines=0),
    )

    assert result.changed is True
    assert len(result.text) <= 122
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
