from __future__ import annotations

import json

import pytest

from noisegate.engine import classify_command
from noisegate.plugin import transform_tool_result

PROCESS_TEXT_FIELDS = (
    "stdout",
    "stderr",
    "output",
    "logs",
    "log",
    "new_output",
    "output_preview",
)


def numbered(prefix: str, count: int = 160) -> str:
    return "\n".join(f"{prefix} {index:03d}" for index in range(count))


def make_noisy_text(label: str, count: int = 160) -> str:
    lines = [f"background noise {index:03d}" for index in range(count)]
    lines[count // 2] = f"ERROR {label} failed in the middle"
    return "\n".join(lines)


def compact_process(
    payload: dict[str, object],
    *,
    args: dict[str, object] | None = None,
    arguments: dict[str, object] | None = None,
    max_chars: int = 220,
    max_lines: int = 8,
) -> dict[str, object]:
    transformed = transform_tool_result(
        json.dumps(payload),
        tool_name="process",
        args=args,
        arguments=arguments,
        noisegate_max_chars=max_chars,
        noisegate_max_lines=max_lines,
        noisegate_head_lines=1,
        noisegate_tail_lines=1,
        noisegate_important_context_lines=0,
    )
    assert isinstance(transformed, str)
    parsed = json.loads(transformed)
    assert isinstance(parsed, dict)
    return parsed


@pytest.mark.parametrize("field", PROCESS_TEXT_FIELDS)
def test_process_text_fields_compact_without_dropping_metadata(field: str) -> None:
    payload: dict[str, object] = {
        field: make_noisy_text(field),
        "session_id": "proc_abc123",
        "pid": 4312,
        "action": "log",
        "status": "running",
        "returncode": 0,
        "exit_code": 0,
        "completion_reason": "still_running",
        "unrelated": {"keep": [1, "two", True]},
    }

    compacted = compact_process(payload, args={"action": "log"})

    text = compacted[field]
    assert isinstance(text, str)
    assert "[noisegate: omitted" in text
    for key, value in payload.items():
        if key != field:
            assert compacted[key] == value


@pytest.mark.parametrize(
    ("action", "field", "action_source"),
    [
        ("poll", "output_preview", "result"),
        ("log", "log", "args"),
        ("wait", "new_output", "arguments"),
    ],
)
def test_process_poll_log_wait_actions_compact_from_every_action_source(
    action: str,
    field: str,
    action_source: str,
) -> None:
    payload: dict[str, object] = {field: make_noisy_text(field), "session_id": "proc_1"}
    args: dict[str, object] | None = None
    arguments: dict[str, object] | None = None
    if action_source == "result":
        payload["action"] = action
    elif action_source == "args":
        args = {"action": action}
    else:
        arguments = {"action": action}

    compacted = compact_process(payload, args=args, arguments=arguments)

    assert "[noisegate: omitted" in str(compacted[field])


@pytest.mark.parametrize("field", ["output_preview", "new_output"])
def test_commandless_process_preview_without_log_evidence_stays_exact(field: str) -> None:
    text = numbered("exact source line", count=300)
    raw = json.dumps(
        {
            "action": "poll",
            "session_id": "proc_exact",
            field: text,
        }
    )

    assert (
        transform_tool_result(
            raw,
            tool_name="process",
            args={"action": "poll"},
            noisegate_max_chars=180,
        )
        is None
    )


@pytest.mark.parametrize("action", ["list", "status"])
def test_process_list_and_status_without_logs_stay_exact(action: str) -> None:
    raw = json.dumps(
        {
            "action": action,
            "processes": [
                {"session_id": "proc_1", "pid": 123, "status": "running"},
                {"session_id": "proc_2", "pid": 456, "status": "exited", "exit_code": 0},
            ],
            "count": 2,
        }
    )

    assert (
        transform_tool_result(
            raw,
            tool_name="process",
            args={"action": action},
            noisegate_max_chars=120,
        )
        is None
    )


@pytest.mark.parametrize("action", ["list", "status"])
@pytest.mark.parametrize("action_source", ["result", "args", "arguments"])
def test_commandless_process_list_and_status_with_diagnostic_text_stay_exact(
    action: str,
    action_source: str,
) -> None:
    text = make_noisy_text("status preview")
    payload: dict[str, object] = {
        "session_id": "proc_status",
        "output_preview": text,
        "pid": 123,
    }
    args: dict[str, object] | None = None
    arguments: dict[str, object] | None = None
    if action_source == "result":
        payload["action"] = action
    elif action_source == "args":
        args = {"action": action}
    else:
        arguments = {"action": action}

    raw = json.dumps(payload)
    assert (
        transform_tool_result(
            raw,
            tool_name="process",
            args=args,
            arguments=arguments,
            noisegate_max_chars=180,
        )
        is None
    )


def test_commandless_process_conflicting_actions_stay_exact() -> None:
    raw = json.dumps(
        {
            "action": "status",
            "session_id": "proc_conflict",
            "output_preview": make_noisy_text("conflicting action preview"),
        }
    )

    assert (
        transform_tool_result(
            raw,
            tool_name="process",
            args={"action": "poll"},
            noisegate_max_chars=180,
        )
        is None
    )


@pytest.mark.parametrize("alias", ["command", "cmd", "shell_command", "code", "argv"])
def test_process_real_command_alias_wins_over_synthetic_process_command(alias: str) -> None:
    real_command: object = (
        ["docker", "logs", "api service;cat important.py"]
        if alias == "argv"
        else "docker logs 'api service;cat important.py'"
    )
    payload = {
        alias: real_command,
        "output": "\n".join(
            [
                *[f"routine before {index:03d}" for index in range(80)],
                "connection refused while opening database socket",
                *[f"routine after {index:03d}" for index in range(80)],
            ]
        ),
        "session_id": "proc_real",
        "status": "running",
    }

    compacted = compact_process(
        payload,
        args={"action": "log", "command": "process log"},
        max_chars=190,
        max_lines=5,
    )

    metadata = compacted["noisegate"]
    assert isinstance(metadata, dict)
    fields = metadata["fields"]
    assert isinstance(fields, dict)
    output_metadata = fields["output"]
    assert isinstance(output_metadata, dict)
    assert output_metadata["command_class"] == "docker_logs"
    assert "connection refused while opening database socket" in str(compacted["output"])


@pytest.mark.parametrize("container", ["process", "session", "metadata"])
def test_process_nested_real_command_wins_over_top_level_process_action(container: str) -> None:
    failure = "FATAL nested process command owns this log"
    payload = {
        "command": "process log proc_nested",
        container: {"argv": ["docker", "logs", "nested service"]},
        "output": "\n".join(
            [
                *[f"nested noise before {index:03d}" for index in range(80)],
                failure,
                *[f"nested noise after {index:03d}" for index in range(80)],
            ]
        ),
    }

    compacted = compact_process(payload, args={"action": "log"}, max_chars=180, max_lines=5)

    assert failure in str(compacted["output"])
    metadata = compacted["noisegate"]
    assert isinstance(metadata, dict)
    fields = metadata["fields"]
    assert isinstance(fields, dict)
    field_metadata = fields["output"]
    assert isinstance(field_metadata, dict)
    assert field_metadata["command_class"] == "docker_logs"


def test_patch_detection_uses_executed_command_not_log_target_name() -> None:
    noisy = numbered("ordinary log line")

    docker_payload = compact_process(
        {"command": "docker logs patch-service", "output": noisy},
        args={"action": "log"},
    )
    tail_payload = compact_process(
        {"command": "tail -f patch.log", "output": noisy},
        args={"action": "log"},
    )

    assert "[noisegate: omitted" in str(docker_payload["output"])
    assert "[noisegate: omitted" in str(tail_payload["output"])

    patch_raw = json.dumps(
        {"command": "patch -p1 update.patch", "output": numbered("patch command output")}
    )
    assert (
        transform_tool_result(
            patch_raw,
            tool_name="process",
            args={"action": "wait"},
            noisegate_max_chars=220,
        )
        is None
    )


def test_docker_service_logs_preserve_middle_failure_anchor() -> None:
    failure = "FATAL worker panic: database connection refused"
    payload = {
        "command": "docker service logs --raw api",
        "logs": "\n".join(
            [
                *[f"service heartbeat before {index:03d}" for index in range(100)],
                failure,
                *[f"service heartbeat after {index:03d}" for index in range(100)],
            ]
        ),
        "session_id": "proc_service",
        "status": "running",
    }

    compacted = compact_process(payload, args={"action": "log"}, max_chars=180, max_lines=5)

    assert failure in str(compacted["logs"])
    metadata = compacted["noisegate"]
    assert isinstance(metadata, dict)
    fields = metadata["fields"]
    assert isinstance(fields, dict)
    logs_metadata = fields["logs"]
    assert isinstance(logs_metadata, dict)
    assert logs_metadata["command_class"] == "docker_logs"


@pytest.mark.parametrize(
    ("command", "expected_class", "failure"),
    [
        ("timeout 30 docker logs app", "docker_logs", "ERROR request timed out"),
        (
            "timeout --signal=TERM 30s journalctl -u api",
            "log_stream",
            "upstream service unreachable",
        ),
        ("timeout 2m tail -f app.log", "log_stream", "connection refused by peer"),
    ],
)
def test_timeout_wrapped_log_commands_compact_and_keep_middle_failures(
    command: str,
    expected_class: str,
    failure: str,
) -> None:
    payload = {
        "command": command,
        "output_preview": "\n".join(
            [
                *[f"wrapper noise before {index:03d}" for index in range(90)],
                failure,
                *[f"wrapper noise after {index:03d}" for index in range(90)],
            ]
        ),
        "status": "running",
    }

    compacted = compact_process(payload, args={"action": "poll"}, max_chars=170, max_lines=5)

    assert failure in str(compacted["output_preview"])
    metadata = compacted["noisegate"]
    assert isinstance(metadata, dict)
    fields = metadata["fields"]
    assert isinstance(fields, dict)
    field_metadata = fields["output_preview"]
    assert isinstance(field_metadata, dict)
    assert field_metadata["command_class"] == expected_class


def test_tail_follow_ignores_shell_redirection_when_identifying_log_target() -> None:
    failure = "ERROR worker failed after redirect"
    payload: dict[str, object] = {
        "command": "tail -f app.log 2>&1",
        "output_preview": "\n".join(
            [
                *[f"redirect noise before {index:03d}" for index in range(90)],
                failure,
                *[f"redirect noise after {index:03d}" for index in range(90)],
            ]
        ),
        "status": "running",
    }

    compacted = compact_process(payload, args={"action": "poll"}, max_chars=170, max_lines=5)

    assert classify_command("tail -f app.log 2>&1", str(payload["output_preview"])) == "log_stream"
    assert failure in str(compacted["output_preview"])
    assert "[noisegate: omitted" in str(compacted["output_preview"])


@pytest.mark.parametrize(
    "command",
    [
        "timeout --signal=TERM 30s cat 'important source.py'",
        "timeout -v 30s cat 'important source.py'",
        "timeout -vsTERM 30s cat 'important source.py'",
    ],
)
def test_timeout_wrapper_does_not_hide_exact_source_ownership(command: str) -> None:
    source = "\n".join(
        f"{index:03d}: exact source line with ERROR and failed text" for index in range(140)
    )
    raw = json.dumps(
        {
            "command": command,
            "output": source,
            "status": "exited",
            "exit_code": 0,
        }
    )

    assert (
        transform_tool_result(
            raw,
            tool_name="process",
            args={"action": "wait"},
            noisegate_max_chars=180,
        )
        is None
    )


def test_systemctl_show_preserves_exact_property_metadata() -> None:
    properties = "\n".join(
        [
            *[f"PropertyBefore{index}=value-{index}" for index in range(120)],
            "Description=Ran 10 tests during the previous activation",
            "FailureBanner==== failures ===",
            "ExecStart={ path=/usr/bin/example ; argv[]=/usr/bin/example --serve ; }",
            *[f"PropertyAfter{index}=value-{index}" for index in range(120)],
        ]
    )
    raw = json.dumps(
        {
            "command": "systemctl show example.service",
            "output": properties,
            "status": "exited",
            "exit_code": 0,
        }
    )

    assert classify_command("systemctl show example.service", properties) == "systemctl_show"
    assert (
        transform_tool_result(
            raw,
            tool_name="process",
            args={"action": "wait"},
            noisegate_max_chars=180,
        )
        is None
    )


@pytest.mark.parametrize(
    ("command", "failure_lines"),
    [
        ("pytest -q", ("FAILED tests/test_api.py::test_request", "AssertionError: wrong value")),
        ("npm test", ("npm ERR! code ELIFECYCLE", "Error: package script failed")),
        ("pip install .", ("ERROR: No matching distribution found for missing-dep",)),
        ("python worker.py", ("fatal: worker panic",)),
        (
            "python worker.py",
            ("Traceback (most recent call last):", "RuntimeError: terminal exception"),
        ),
    ],
)
def test_process_tight_budget_preserves_actionable_middle_failures(
    command: str,
    failure_lines: tuple[str, ...],
) -> None:
    output = "\n".join(
        [
            *[f"progress before {index:03d}" for index in range(100)],
            *failure_lines,
            *[f"progress after {index:03d}" for index in range(100)],
        ]
    )

    compacted = compact_process(
        {
            "command": command,
            "new_output": output,
            "status": "exited",
            "exit_code": 1,
        },
        args={"action": "wait"},
        max_chars=240,
        max_lines=6,
    )

    for failure_line in failure_lines:
        assert failure_line in str(compacted["new_output"])
    assert "[noisegate: exit_code=1]" in str(compacted["new_output"])


@pytest.mark.parametrize(
    "failure",
    [
        "permission denied opening socket",
        "connection refused by upstream",
        "request timeout contacting dependency",
        "service unreachable from worker",
    ],
)
def test_process_log_reducer_keeps_operational_failure_vocabulary(failure: str) -> None:
    payload: dict[str, object] = {
        "command": "docker service logs api",
        "log": "\n".join(
            [
                *[f"routine before {index:03d}" for index in range(100)],
                failure,
                *[f"routine after {index:03d}" for index in range(100)],
            ]
        ),
        "status": "running",
    }

    compacted = compact_process(payload, args={"action": "log"}, max_chars=170, max_lines=5)

    assert failure in str(compacted["log"])


@pytest.mark.parametrize(
    "failure",
    [
        "ERROR connection refused by primary database",
        "ERROR request timeout contacting primary database",
        "ERROR primary database unreachable from worker",
    ],
)
def test_process_log_prioritizes_operational_signal_inside_dense_error_noise(
    failure: str,
) -> None:
    payload: dict[str, object] = {
        "command": "docker service logs api",
        "log": "\n".join(
            [
                *[f"ERROR retry failed before {index:03d}" for index in range(100)],
                failure,
                *[f"ERROR retry failed after {index:03d}" for index in range(100)],
            ]
        ),
        "status": "running",
    }

    compacted = compact_process(payload, args={"action": "log"}, max_chars=400, max_lines=20)

    assert failure in str(compacted["log"])


def test_process_lcm_ref_and_failure_anchor_survive_together_when_they_fit() -> None:
    ref = "[Externalized tool output: ref=lcm://payload/abc123]"
    failure = "RuntimeError: actionable background failure"
    payload = {
        "command": "pytest -q",
        "new_output": "\n".join(
            [
                ref,
                *[f"collection noise {index:03d}" for index in range(80)],
                failure,
                *[f"teardown noise {index:03d}" for index in range(80)],
            ]
        ),
        "status": "failed",
    }

    compacted = compact_process(payload, args={"action": "wait"}, max_chars=210, max_lines=5)

    assert ref in str(compacted["new_output"])
    assert failure in str(compacted["new_output"])
    assert "[noisegate: exit_code=" not in str(compacted["new_output"])


@pytest.mark.parametrize(
    ("field", "command", "exact_text"),
    [
        (
            "logs",
            "cat important.py",
            "\n".join(f"source line {index:03d}: ERROR = 'exact'" for index in range(140)),
        ),
        (
            "output",
            "tail -f app.py",
            "\n".join(f"source line {index:03d}: ERROR = 'exact'" for index in range(140)),
        ),
        (
            "new_output",
            "git diff -- src/app.py",
            "\n".join(
                [
                    "diff --git a/src/app.py b/src/app.py",
                    "--- a/src/app.py",
                    "+++ b/src/app.py",
                    *[f"+added exact line {index:03d}" for index in range(140)],
                ]
            ),
        ),
        (
            "output_preview",
            "pytest -q",
            "\n".join(
                [
                    "*** Begin Patch",
                    "*** Update File: src/app.py",
                    *[f"+exact patch line {index:03d}" for index in range(140)],
                    "*** End Patch",
                ]
            ),
        ),
    ],
)
def test_exact_content_inside_process_text_fields_is_not_compacted(
    field: str,
    command: str,
    exact_text: str,
) -> None:
    raw = json.dumps({"command": command, field: exact_text, "session_id": "proc_exact"})

    assert (
        transform_tool_result(
            raw,
            tool_name="process",
            args={"action": "log"},
            noisegate_max_chars=180,
        )
        is None
    )
