from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from noisegate.plugin import register, transform_terminal_output, transform_tool_result

Hook = Callable[..., str | None]


def numbered(prefix: str, count: int) -> str:
    return "\n".join(f"{prefix} {index:03d}" for index in range(1, count + 1))


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
        tool_name="execute_code",
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


def test_read_file_and_write_file_are_not_touched() -> None:
    raw = terminal_result(numbered("file content", 100), command="cat important.txt")

    assert transform_tool_result(raw, tool_name="read_file", noisegate_max_chars=100) is None
    assert transform_tool_result(raw, tool_name="write_file", noisegate_max_chars=100) is None


def test_skill_view_is_not_touched() -> None:
    raw = json.dumps({"content": numbered("skill instruction", 120), "path": "SKILL.md"})

    assert transform_tool_result(raw, tool_name="skill_view", noisegate_max_chars=100) is None


def test_patch_tool_result_is_not_touched() -> None:
    raw = "\n".join(["*** Begin Patch", *numbered("+line", 100), "*** End Patch"])

    assert transform_tool_result(raw, tool_name="patch", noisegate_max_chars=100) is None


def test_bad_json_fails_open_for_tool_result_hook() -> None:
    raw = "{not json" + numbered("line", 100)

    assert transform_tool_result(raw, tool_name="terminal", noisegate_max_chars=100) is None


def test_generic_json_string_field_can_be_compacted() -> None:
    raw = json.dumps({"output": numbered("log", 100), "ok": True})

    transformed = transform_tool_result(raw, tool_name="web_extract", noisegate_max_chars=120)

    payload = parse_hook_result(transformed)
    assert payload["ok"] is True
    assert "[noisegate: omitted" in payload["output"]
    assert payload["noisegate"]["fields"]["output"]["original_lines"] == 100


def test_transform_tool_result_skips_json_rewrite_when_metadata_would_grow_result() -> None:
    raw = json.dumps({"stdout": "A" * 4010})

    transformed = transform_tool_result(raw, tool_name="terminal")

    assert transformed is None


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


def test_transform_tool_result_does_not_treat_http_status_code_as_exit_code() -> None:
    raw = json.dumps({"status_code": 200, "content": numbered("html", 200)})

    transformed = transform_tool_result(raw, tool_name="web_extract", noisegate_max_chars=500)

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
