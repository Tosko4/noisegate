from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import noisegate.plugin as plugin
from noisegate.plugin import register, transform_terminal_output, transform_tool_result

Hook = Callable[..., str | None]


def numbered(prefix: str, count: int) -> str:
    return "\n".join(f"{prefix} {index:03d}" for index in range(1, count + 1))


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
    raw = terminal_result(exact, command="cat src/source_fixture.py")

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


def test_noisy_generic_json_string_field_can_be_compacted() -> None:
    raw = json.dumps({"logs": numbered("console log", 100), "ok": True})

    transformed = transform_tool_result(raw, tool_name="browser_console", noisegate_max_chars=120)

    payload = parse_hook_result(transformed)
    assert payload["ok"] is True
    assert "[noisegate: omitted" in payload["logs"]
    assert payload["noisegate"]["fields"]["logs"]["original_lines"] == 100


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
        noisegate_max_chars=140,
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
        noisegate_max_chars=140,
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


def test_transform_tool_result_preserves_existing_noisegate_key() -> None:
    raw = json.dumps({"noisegate": {"tool": "data"}, "stdout": numbered("line", 100)})

    transformed = transform_tool_result(raw, tool_name="terminal", noisegate_max_chars=200)

    payload = parse_hook_result(transformed)
    assert payload["noisegate"] == {"tool": "data"}
    assert payload["_noisegate"]["compacted"] is True


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
