from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import noisegate.plugin as plugin
from noisegate.plugin import transform_terminal_output, transform_tool_result

LCM_REF = "20260708T120000Z-tool-call-abc123.json"
TOOL_PLACEHOLDER = (
    "[Externalized tool output: tool_call_id=call_abc123; "
    f"chars=120000; bytes=120321; ref={LCM_REF}]"
)
INGEST_PLACEHOLDER = (
    "[Externalized LCM ingest payload: kind=media_payload; field=image; "
    f"chars=98304; bytes=131072; ref={LCM_REF}]"
)
BASE64_PREVIEW = "iVBORw0KGgoAAAANSUhEUgAA" * 64
SECRET_LINE = "SECRET_TOKEN=should-not-be-written-by-early-hook"


def numbered(prefix: str, count: int) -> str:
    return "\n".join(f"{prefix} {index:03d}" for index in range(1, count + 1))


def terminal_result(stdout: str, *, command: str = "python noisy.py", exit_code: int = 0) -> str:
    return json.dumps(
        {
            "command": command,
            "stdout": stdout,
            "stderr": "",
            "exit_code": exit_code,
            "status": "failed" if exit_code else "ok",
        }
    )


def parse_hook_result(value: str | None) -> dict[str, Any]:
    assert isinstance(value, str)
    parsed = json.loads(value)
    assert isinstance(parsed, dict)
    return parsed


def run_cli(
    *args: str,
    input_text: str = "",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    full_env = os.environ.copy()
    full_env.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "noisegate.cli", *args],
        input=input_text,
        text=True,
        capture_output=True,
        env=full_env,
        check=False,
    )


def test_terminal_output_before_lcm_ingest_preserves_externalized_placeholder() -> None:
    raw = "\n".join(
        [
            numbered("pre-lcm terminal noise", 80),
            TOOL_PLACEHOLDER,
            numbered("post-lcm terminal noise", 80),
        ]
    )

    transformed = transform_terminal_output(
        command="python noisy.py",
        output=raw,
        noisegate_max_chars=420,
        noisegate_max_lines=12,
        noisegate_head_lines=2,
        noisegate_tail_lines=2,
    )

    assert isinstance(transformed, str)
    assert transformed != raw
    assert TOOL_PLACEHOLDER in transformed
    assert LCM_REF in transformed
    assert "[noisegate: omitted" in transformed


def test_mixed_failure_output_keeps_lcm_ref_and_failure_anchor() -> None:
    raw = "\n".join(
        [
            numbered("setup noise", 80),
            TOOL_PLACEHOLDER,
            numbered("middle noise", 80),
            "FAILED tests/test_lcm.py::test_externalized_recovery - AssertionError",
            numbered("tail noise", 80),
        ]
    )

    transformed = transform_terminal_output(
        command="pytest -q",
        output=raw,
        exit_code=1,
        noisegate_max_chars=700,
        noisegate_max_lines=14,
        noisegate_head_lines=2,
        noisegate_tail_lines=2,
    )

    assert isinstance(transformed, str)
    assert TOOL_PLACEHOLDER in transformed
    assert "FAILED tests/test_lcm.py::test_externalized_recovery" in transformed


def test_many_failure_lines_keep_lcm_ref_beyond_max_important_lines() -> None:
    failures = "\n".join(
        f"FAILED tests/test_many.py::test_{index:03d} - AssertionError"
        for index in range(100)
    )
    raw = "\n".join(
        [
            numbered("setup noise", 20),
            TOOL_PLACEHOLDER,
            numbered("middle noise", 20),
            failures,
            numbered("tail noise", 20),
        ]
    )

    transformed = transform_terminal_output(
        command="pytest -q",
        output=raw,
        exit_code=1,
        noisegate_max_chars=4000,
        noisegate_max_lines=60,
        noisegate_head_lines=2,
        noisegate_tail_lines=2,
        noisegate_max_important_lines=20,
    )

    assert isinstance(transformed, str)
    assert TOOL_PLACEHOLDER in transformed
    assert "FAILED tests/test_many.py::test_000" in transformed


def test_tight_mixed_failure_budget_preserves_lcm_ref_or_fails_open() -> None:
    raw = "\n".join(
        [
            numbered("setup noise", 20),
            TOOL_PLACEHOLDER,
            numbered("middle noise", 20),
            "FAILED tests/test_lcm.py::test_externalized_recovery - AssertionError: "
            + ("y" * 200),
            numbered("tail noise", 20),
        ]
    )

    transformed = transform_terminal_output(
        command="pytest -q",
        output=raw,
        exit_code=1,
        noisegate_max_chars=360,
        noisegate_max_lines=14,
        noisegate_head_lines=2,
        noisegate_tail_lines=2,
    )

    assert transformed is None or TOOL_PLACEHOLDER in transformed


def test_multiple_lcm_refs_are_all_preserved_or_compaction_fails_open() -> None:
    refs = [
        "[Externalized tool output: tool_call_id=call_"
        f"{index}; chars=120000; bytes=120321; ref=20260708T120000Z-tool-call-{index}.json]"
        for index in range(3)
    ]
    raw = "\n".join(
        [
            numbered("pre", 20),
            refs[0],
            numbered("mid1", 20),
            refs[1],
            numbered("mid2", 20),
            refs[2],
            numbered("tail", 20),
        ]
    )

    transformed = transform_terminal_output(
        command="python noisy.py",
        output=raw,
        exit_code=0,
        noisegate_max_chars=500,
        noisegate_max_lines=20,
        noisegate_head_lines=2,
        noisegate_tail_lines=2,
    )

    assert transformed is None or all(ref in transformed for ref in refs)


def test_gc_externalized_placeholder_is_preserved_exactly() -> None:
    gc_placeholder = (
        "[GC'd externalized tool output: tool_call_id=call_gc; "
        f"chars=220000; bytes=230000; ref={LCM_REF}]"
    )
    raw = "\n".join([numbered("pre", 80), gc_placeholder, numbered("post", 80)])

    transformed = transform_terminal_output(
        command="python noisy.py",
        output=raw,
        exit_code=0,
        noisegate_max_chars=450,
        noisegate_max_lines=14,
        noisegate_head_lines=2,
        noisegate_tail_lines=2,
    )

    assert isinstance(transformed, str)
    assert gc_placeholder in transformed


def test_artifact_enabled_tool_result_does_not_replace_lcm_ref_with_noisegate_artifact(
    monkeypatch,
) -> None:
    def fake_store(text: str, _options: object) -> dict[str, object]:
        return {
            "stored": True,
            "id": "ng_" + ("a" * 24),
            "sha256": "b" * 64,
            "size_bytes": len(text.encode()),
        }

    monkeypatch.setattr(plugin, "_store_artifact", fake_store)
    raw = terminal_result(
        "\n".join(
            [
                numbered("pre", 80),
                TOOL_PLACEHOLDER,
                numbered("post", 80),
            ]
        )
    )

    transformed = transform_tool_result(
        raw,
        tool_name="terminal",
        noisegate_max_chars=350,
        noisegate_max_lines=14,
        noisegate_head_lines=2,
        noisegate_tail_lines=2,
        noisegate_artifacts=True,
    )

    payload = parse_hook_result(transformed)
    stdout = payload["stdout"]
    assert isinstance(stdout, str)
    assert TOOL_PLACEHOLDER in stdout
    assert "[Externalized tool output:\n[noisegate: omitted" not in stdout
    artifact = payload["noisegate"]["fields"]["stdout"].get("artifact")
    if isinstance(artifact, dict) and artifact.get("stored") is True:
        assert artifact["id"] in stdout


def test_lcm_describe_externalized_ref_result_is_protected_exactly() -> None:
    raw = json.dumps(
        {
            "externalized_ref": LCM_REF,
            "kind": "tool_result",
            "tool_call_id": "call_abc123",
            "field_path": "stdout",
            "content_chars": 120000,
            "content_bytes": 120321,
            "content_preview": numbered("preview", 120),
        },
        ensure_ascii=False,
    )

    assert transform_tool_result(raw, tool_name="lcm_describe", noisegate_max_chars=120) is None


def test_lcm_expand_externalized_ref_raw_recovery_is_protected_and_needs_no_noisegate_artifact(
    tmp_path: Path,
) -> None:
    raw = json.dumps(
        {
            "externalized_ref": LCM_REF,
            "source_type": "externalized_payload",
            "kind": "tool_result",
            "tool_call_id": "call_abc123",
            "field_path": "stdout",
            "content_chars": 120000,
            "content": "\n".join(
                [
                    numbered("raw recovered line", 120),
                    SECRET_LINE,
                    BASE64_PREVIEW,
                    numbered("raw recovered tail", 120),
                ]
            ),
            "content_offset": 0,
            "content_truncated": True,
            "next_content_offset": 4000,
            "has_more": True,
        },
        ensure_ascii=False,
    )
    artifact_dir = tmp_path / "noisegate-artifacts"

    assert (
        transform_tool_result(
            raw,
            tool_name="lcm_expand",
            noisegate_max_chars=160,
            noisegate_artifacts=True,
            noisegate_artifact_dir=str(artifact_dir),
        )
        is None
    )
    assert not artifact_dir.exists()


def test_missing_externalized_file_error_stays_exact() -> None:
    raw = json.dumps({"error": f"Externalized payload {LCM_REF} not found in current session"})

    assert transform_tool_result(raw, tool_name="lcm_expand", noisegate_max_chars=40) is None


def test_externalization_failure_fallback_fails_open_without_data_loss(monkeypatch) -> None:
    raw = terminal_result(
        "\n".join(
            [
                "LCM externalization failed: falling back to inline terminal output",
                numbered("fallback raw line", 80),
                TOOL_PLACEHOLDER,
                numbered("fallback raw tail", 80),
            ]
        )
    )

    def broken_reduce_text(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated reducer failure after LCM fallback")

    monkeypatch.setattr(plugin, "reduce_text", broken_reduce_text)

    assert transform_tool_result(raw, tool_name="terminal", noisegate_max_chars=120) is None


def test_base64_media_payload_placeholder_is_preserved_without_early_artifact_persistence(
    tmp_path: Path,
) -> None:
    raw = "\n".join(
        [
            numbered("media decode noise", 60),
            INGEST_PLACEHOLDER,
            BASE64_PREVIEW,
            SECRET_LINE,
            numbered("media tail noise", 60),
        ]
    )
    artifact_dir = tmp_path / "artifacts"

    transformed = transform_terminal_output(
        command="python render_media.py",
        output=raw,
        noisegate_max_chars=520,
        noisegate_max_lines=14,
        noisegate_head_lines=2,
        noisegate_tail_lines=2,
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(artifact_dir),
    )

    assert isinstance(transformed, str)
    assert INGEST_PLACEHOLDER in transformed
    assert BASE64_PREVIEW not in transformed
    assert SECRET_LINE not in transformed
    assert "[noisegate artifact:" not in transformed
    assert not artifact_dir.exists()


def test_huge_json_payload_preserves_externalized_ref_metadata_without_artifact(
    tmp_path: Path,
) -> None:
    raw = json.dumps(
        {
            "externalized_ref": LCM_REF,
            "kind": "tool_result",
            "content": numbered("json payload", 500),
            "content_offset": 0,
            "next_content_offset": 4096,
            "has_more": True,
        },
        ensure_ascii=False,
    )
    artifact_dir = tmp_path / "artifacts"

    transformed = transform_tool_result(
        raw,
        tool_name="browser_console",
        noisegate_max_chars=500,
        noisegate_max_lines=18,
        noisegate_head_lines=2,
        noisegate_tail_lines=2,
        noisegate_artifact_dir=str(artifact_dir),
    )

    payload = parse_hook_result(transformed)
    assert payload["externalized_ref"] == LCM_REF
    assert payload["kind"] == "tool_result"
    assert "[noisegate: omitted" in payload["content"]
    assert set(payload["noisegate"]["fields"]) == {"content"}
    assert "artifact" not in payload["noisegate"]["fields"]["content"]
    assert not artifact_dir.exists()


def test_reduce_json_preserves_lcm_expand_externalized_ref_envelope() -> None:
    envelope = {
        "tool_name": "lcm_expand",
        "args": {"externalized_ref": LCM_REF, "content_offset": 4000},
        "result": json.dumps(
            {
                "externalized_ref": LCM_REF,
                "source_type": "externalized_payload",
                "content": numbered("paged raw recovery", 300),
                "content_offset": 4000,
                "content_truncated": True,
                "next_content_offset": 8000,
                "has_more": True,
            },
            ensure_ascii=False,
        ),
        "noisegate": {"max_chars": 120},
    }

    proc = run_cli("reduce-json", input_text=json.dumps(envelope, ensure_ascii=False))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == envelope
