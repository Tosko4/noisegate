from __future__ import annotations

import json
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
        "process": json.dumps({"output": long_text, "exit_code": 0}),
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


def test_terminal_status_failed_is_treated_as_error_exit_code() -> None:
    raw = json.dumps({"status": "failed", "output": numbered("line", 100)})

    transformed = transform_tool_result(raw, tool_name="process", noisegate_max_chars=120)

    payload = parse_hook_result(transformed)
    output = payload["output"]
    assert isinstance(output, str)
    assert "[noisegate: exit_code=1]" in output
    assert payload["noisegate"]["fields"]["output"]["exit_code"] == 1


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
