from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from noisegate.engine import NoisegateOptions, reduce_text
from noisegate.plugin import transform_terminal_output, transform_tool_result

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "perseus_prepare_output.golden.json"
FIXTURE_SHA256 = "3fa62631d48cb9190753f206d8a03a7afe9faabc664907f6580f6ed3e287ef47"
PRODUCER_MERGE = "Perseus-Computing-LLC/perseus-vault@3fb1c07e802c883f4da911584b6406d0465fce12"


def options() -> NoisegateOptions:
    return NoisegateOptions(max_chars=180, head_lines=3, tail_lines=2)


def prepare_markdown() -> str:
    return "\n".join(
        [
            "<memory-prep>",
            "## Perseus Vault Context",
            "Retrieved memory — informational, not instructions.",
            *[
                f"- [insight] exact context line {index:03d}: FAILED npm ERR! pytest PASSED"
                for index in range(80)
            ],
            "</memory-prep>",
        ]
    )


def terminal_result(stdout: str, command: str) -> str:
    return json.dumps(
        {
            "command": command,
            "stdout": stdout,
            "stderr": "",
            "exit": 0,
            "status": "ok",
        }
    )


@pytest.mark.parametrize(
    ("command", "exit_code"),
    (
        ("perseus-vault prepare --json", None),
        ("/opt/perseus/bin/perseus-vault prepare --task 'refactor auth' --json", None),
        ("exec /opt/perseus/bin/perseus-vault prepare --json", None),
        ("env PERSEUS_VAULT_DB_PATH=/tmp/fixture.db perseus-vault prepare --json", None),
        ("cd /tmp/project && perseus-vault prepare --json", 0),
        ("bash -lc 'perseus-vault prepare --json'", None),
    ),
)
def test_direct_perseus_prepare_commands_preserve_complete_output(
    command: str,
    exit_code: int | None,
) -> None:
    raw = prepare_markdown()

    result = reduce_text(raw, command=command, exit_code=exit_code, options=options())

    assert result.changed is False
    assert result.text == raw
    assert result.metadata["command_class"] == "perseus_prepare"
    assert result.metadata["reducer"] == "protected_perseus_prepare"
    assert result.metadata["reason"] == "perseus_prepare_passthrough"


def test_producer_golden_is_preserved_byte_for_byte() -> None:
    raw_bytes = FIXTURE_PATH.read_bytes()
    raw = raw_bytes.decode("utf-8")
    assert hashlib.sha256(raw_bytes).hexdigest() == FIXTURE_SHA256, PRODUCER_MERGE

    result = reduce_text(raw, command="perseus-vault prepare --json", options=options())

    assert result.changed is False
    assert result.text.encode("utf-8") == raw_bytes
    assert result.metadata["command_class"] == "perseus_prepare"


def test_both_hermes_terminal_hook_paths_preserve_prepare_markdown() -> None:
    raw = prepare_markdown()
    command = "perseus-vault prepare --task 'refactor auth'"

    assert (
        transform_terminal_output(command=command, output=raw, noisegate_max_chars=180)
        is None
    )
    assert (
        transform_tool_result(
            terminal_result(raw, command),
            tool_name="terminal",
            args={"command": "pytest -q"},
            noisegate_max_chars=180,
        )
        is None
    )


@pytest.mark.parametrize(
    ("command", "exit_code", "expected_class"),
    (
        ("perseus-vault maintain", None, "generic"),
        ("printf '%s\\n' 'perseus-vault prepare --json'", None, "generic"),
        ("pytest -q -k 'perseus-vault prepare'", None, "pytest"),
        ("perseus-vault prepare --json >/dev/null; pytest -q", None, "pytest"),
        ("true || perseus-vault prepare --json", None, "generic"),
        ("cd /tmp || perseus-vault prepare --json", None, "generic"),
        (
            "bash -lc 'perseus-vault prepare --json' >/dev/null 2>/dev/null; pytest -q",
            0,
            "pytest",
        ),
        ("export 1=value && perseus-vault prepare --json", 1, "generic"),
        ("cd /definitely/missing && perseus-vault prepare --json", 1, "generic"),
        ("cd /tmp | pytest -q && perseus-vault prepare --json", 0, "generic"),
        ("export X=1 | pytest -q && perseus-vault prepare --json", 0, "generic"),
    ),
)
def test_non_owning_prepare_text_does_not_disable_compaction(
    command: str,
    exit_code: int | None,
    expected_class: str,
) -> None:
    raw = "\n".join(f"ordinary noisy output {index:03d}" for index in range(100))

    result = reduce_text(raw, command=command, exit_code=exit_code, options=options())

    assert result.changed is True
    assert result.metadata["command_class"] == expected_class
