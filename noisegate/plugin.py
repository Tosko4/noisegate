from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any, Protocol, TypeAlias

from ._version import __version__
from .engine import (
    JsonValue,
    NoisegateOptions,
    _append_recovery_notices,
    _command_has_compactable_intent,
    _compactable_command_output_class,
    _drop_artifact_if_notice_cannot_fit,
    _exact_command_output_class,
    _is_compactable_tool_name,
    _plan_artifact,
    _preserve_patterns_for_output,
    _store_artifact,
    classify_command,
    reduce_text,
)

HookCallback: TypeAlias = Callable[..., str | None]

TERMINAL_TOOL_NAMES = frozenset({"terminal", "process", "read_terminal"})
TERMINAL_TEXT_FIELDS = ("stdout", "stderr", "output")
ALWAYS_PROTECTED_COMMAND_CLASSES = frozenset({"file_read", "source_search", "patch"})
CONDITIONALLY_PROTECTED_COMMAND_CLASSES = frozenset({"git_diff"})
EXACT_COMMAND_CLASSES = (
    ALWAYS_PROTECTED_COMMAND_CLASSES | CONDITIONALLY_PROTECTED_COMMAND_CLASSES
)
OUTPUT_ASSISTED_COMMAND_CLASSES = frozenset({"file_read", "source_search"})
NOISY_COMMAND_CLASSES = frozenset(
    {"apt", "docker_build", "docker_logs", "node", "pytest", "python_package", "unittest"}
)
COMMAND_ALIASES = ("command", "cmd", "shell_command", "code")
GENERIC_TEXT_FIELDS = (
    "stdout",
    "stderr",
    "output",
    "text",
    "content",
    "result",
    "message",
    "logs",
)


class HookRegistrar(Protocol):
    def register_hook(self, name: str, callback: HookCallback) -> None: ...


def register(ctx: HookRegistrar) -> None:
    ctx.register_hook("transform_tool_result", transform_tool_result)
    ctx.register_hook("transform_terminal_output", transform_terminal_output)


def transform_tool_result(
    result: str = "",
    *,
    tool_name: str = "",
    args: Mapping[str, Any] | None = None,
    arguments: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> str | None:
    try:
        override = kwargs.pop("noisegate_exit_code", None)
        exit_code_override = (
            override if isinstance(override, int) and not isinstance(override, bool) else None
        )
        if not isinstance(result, str):
            return None
        if tool_name and not _is_compactable_tool_name(tool_name):
            return None
        options = NoisegateOptions.from_env().with_mapping(kwargs)
        if not options.enabled or options.mode == "off":
            return None

        parsed = json.loads(result)
        args_map = args if isinstance(args, Mapping) else {}
        arguments_map = arguments if isinstance(arguments, Mapping) else {}
        call_args = _call_args(
            args,
            arguments,
            parsed,
            prefer_host_args=bool(tool_name),
        )
        if not tool_name and isinstance(parsed, Mapping):
            embedded_tool_name = _payload_tool_name(parsed)
            if embedded_tool_name:
                if not _is_compactable_tool_name(embedded_tool_name):
                    return None
                tool_name = embedded_tool_name
        if not tool_name:
            if not _looks_terminal_payload(parsed, call_args):
                return None
            tool_name = "terminal"

        if isinstance(parsed, str):
            command = _select_command(
                parsed,
                call_args,
                args_map,
                arguments_map,
                exit_code=exit_code_override,
            )
            reduce_options = replace(options, artifact_enabled=False)
            reduced = reduce_text(
                parsed,
                command=command,
                tool_name=tool_name,
                source="json_string",
                exit_code=exit_code_override,
                options=reduce_options,
            )
            if not reduced.changed:
                return None
            metadata = dict(reduced.metadata)
            text = reduced.text
            preserve_patterns = _preserve_patterns_for(
                command,
                parsed,
                exit_code=exit_code_override,
            )
            if options.artifact_enabled:
                metadata["artifact"] = _plan_artifact(parsed, options)
                _drop_artifact_if_notice_cannot_fit(
                    metadata,
                    options,
                    artifact_dir=options.artifact_dir,
                )
                text = _append_recovery_notices(
                    text,
                    metadata,
                    artifact_dir=options.artifact_dir,
                    options=options,
                    preserve_patterns=preserve_patterns,
                )
                _mark_artifact_notice_dropped_if_missing(metadata, text)
            candidate = json.dumps(text, ensure_ascii=False)
            if len(candidate) >= len(result):
                return None
            if options.artifact_enabled:
                artifact = metadata.get("artifact")
                if isinstance(artifact, dict) and artifact.get("stored") is True:
                    metadata["artifact"] = _store_artifact(parsed, options)
                    text = _append_recovery_notices(
                        reduced.text,
                        metadata,
                        artifact_dir=options.artifact_dir,
                        options=options,
                        preserve_patterns=preserve_patterns,
                    )
                    candidate = json.dumps(text, ensure_ascii=False)
            return candidate if len(candidate) < len(result) else None

        if not isinstance(parsed, dict):
            return None

        payload: dict[str, JsonValue] = dict(parsed)
        exit_code = _extract_exit_code(payload, tool_name)
        if exit_code is None:
            exit_code = exit_code_override
        fields = _candidate_fields(tool_name, payload)
        command_text = "\n".join(
            value
            for field in fields
            if isinstance((value := payload.get(field)), str)
        )
        command = _select_command(
            command_text,
            call_args,
            args_map,
            arguments_map,
            payload,
            exit_code=exit_code,
        )
        field_metadata: dict[str, JsonValue] = {}
        original_values: dict[str, str] = {}
        reduced_values: dict[str, str] = {}
        preserve_patterns_by_field: dict[str, tuple[re.Pattern[str], ...] | None] = {}
        reduce_options = replace(options, artifact_enabled=False)

        for field in fields:
            value = payload.get(field)
            if not isinstance(value, str):
                continue
            reduced = reduce_text(
                value,
                command=command,
                tool_name=tool_name,
                source=f"json_field:{field}",
                exit_code=exit_code,
                options=reduce_options,
            )
            if reduced.changed:
                metadata = dict(reduced.metadata)
                text = reduced.text
                preserve_patterns: tuple[re.Pattern[str], ...] | None = None
                if options.artifact_enabled:
                    preserve_patterns = _preserve_patterns_for(command, value, exit_code=exit_code)
                    metadata["artifact"] = _plan_artifact(value, options)
                    _drop_artifact_if_notice_cannot_fit(
                        metadata,
                        options,
                        artifact_dir=options.artifact_dir,
                    )
                    notice_metadata = dict(metadata)
                    notice_metadata["exit_code"] = None
                    text = _append_recovery_notices(
                        text,
                        notice_metadata,
                        artifact_dir=options.artifact_dir,
                        options=options,
                        preserve_patterns=preserve_patterns,
                    )
                    _mark_artifact_notice_dropped_if_missing(metadata, text)
                    if len(text) >= len(value):
                        continue
                payload[field] = text
                field_metadata[field] = metadata
                original_values[field] = value
                reduced_values[field] = reduced.text
                preserve_patterns_by_field[field] = preserve_patterns

        if not field_metadata:
            return None

        metadata_key = _metadata_key(payload)
        payload[metadata_key] = {
            "version": __version__,
            "compacted": True,
            "mode": options.mode,
            "original_result_chars": len(result),
            "fields": field_metadata,
        }
        candidate = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(candidate) >= len(result):
            return None

        if options.artifact_enabled:
            for field, value in original_values.items():
                metadata = field_metadata.get(field)
                reduced_text = reduced_values.get(field)
                if isinstance(metadata, dict) and isinstance(reduced_text, str):
                    artifact = metadata.get("artifact")
                    if isinstance(artifact, dict) and artifact.get("stored") is True:
                        metadata["artifact"] = _store_artifact(value, options)
                    notice_metadata = dict(metadata)
                    notice_metadata["exit_code"] = None
                    payload[field] = _append_recovery_notices(
                        reduced_text,
                        notice_metadata,
                        artifact_dir=options.artifact_dir,
                        options=options,
                        preserve_patterns=preserve_patterns_by_field.get(field),
                    )
            candidate = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return candidate if len(candidate) < len(result) else None
    except Exception:
        return None


def _mark_artifact_notice_dropped_if_missing(
    metadata: dict[str, JsonValue],
    text: str,
) -> None:
    artifact = metadata.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("stored") is not True:
        return
    artifact_id = artifact.get("id")
    if isinstance(artifact_id, str) and artifact_id in text:
        return
    metadata["artifact"] = {
        "stored": False,
        "reason": "recovery_notice_dropped",
        "size_bytes": artifact.get("size_bytes"),
    }


def _metadata_key(payload: Mapping[str, JsonValue]) -> str:
    if "noisegate" not in payload:
        return "noisegate"
    candidate = "_noisegate"
    while candidate in payload:
        candidate = f"_{candidate}"
    return candidate


def _preserve_patterns_for(
    command: str,
    text: str,
    *,
    exit_code: int | None = None,
) -> tuple[re.Pattern[str], ...] | None:
    command_class = classify_command(command, text, exit_code=exit_code)
    return _preserve_patterns_for_output(command_class, text)


def transform_terminal_output(
    *positional: Any,
    command: str = "",
    output: str = "",
    exit_code: int = 0,
    returncode: int | None = None,
    **kwargs: Any,
) -> str | None:
    try:
        if positional:
            if len(positional) >= 1 and not command and isinstance(positional[0], str):
                command = positional[0]
            if len(positional) >= 2 and not output and isinstance(positional[1], str):
                output = positional[1]
            if (
                len(positional) >= 3
                and isinstance(positional[2], int)
                and not isinstance(positional[2], bool)
            ):
                exit_code = positional[2]
        if not isinstance(output, str):
            return None
        options = NoisegateOptions.from_env().with_mapping(kwargs)
        # Hermes calls transform_terminal_output before its built-in terminal
        # redaction pass. Inline compaction is still safe because Hermes redacts
        # the returned string afterwards, but raw artifact storage would persist
        # pre-redaction output. Keep artifacts disabled for this early hook.
        options = replace(options, artifact_enabled=False)
        reduced = reduce_text(
            output,
            command=command,
            tool_name="terminal",
            source="terminal_output",
            exit_code=returncode if returncode is not None else exit_code,
            options=options,
        )
        return reduced.text if reduced.changed else None
    except Exception:
        return None


def _candidate_fields(tool_name: str, payload: Mapping[str, JsonValue]) -> tuple[str, ...]:
    candidates = TERMINAL_TEXT_FIELDS if tool_name in TERMINAL_TOOL_NAMES else GENERIC_TEXT_FIELDS
    return tuple(field for field in candidates if field in payload)


def _commands_from_source(source: Mapping[str, Any]) -> tuple[str, ...]:
    commands = tuple(
        value
        for key in COMMAND_ALIASES
        if isinstance((value := source.get(key)), str) and value.strip()
    )
    argv = source.get("argv")
    if isinstance(argv, list) and argv and all(isinstance(item, str) for item in argv):
        commands += (shlex.join(argv),)
    return commands
def _call_args(
    args: Mapping[str, Any] | None,
    arguments: Mapping[str, Any] | None,
    parsed: Any,
    *,
    prefer_host_args: bool = False,
) -> Mapping[str, Any]:
    parsed_sources: list[Mapping[str, Any]] = []
    if isinstance(parsed, Mapping):
        for key in ("args", "arguments"):
            candidate = parsed.get(key)
            if isinstance(candidate, Mapping):
                parsed_sources.append(candidate)
    host_sources = [
        candidate for candidate in (args, arguments) if isinstance(candidate, Mapping)
    ]
    if prefer_host_args and host_sources:
        sources = host_sources
    else:
        sources = [
            *host_sources,
            *parsed_sources,
        ] if prefer_host_args else [*parsed_sources, *host_sources]
    return _merge_command_hints(sources)


def _merge_command_hints(sources: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    merged: dict[str, Any] = {}
    for source in sources:
        if _has_command_hint(merged):
            break
        for key in ("command", "cmd", "shell_command", "code"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                merged[key] = value
        argv = source.get("argv")
        if _usable_argv(argv):
            merged["argv"] = argv
    return merged


def _payload_tool_name(payload: Mapping[str, Any]) -> str:
    return str(
        payload.get("tool_name")
        or payload.get("toolName")
        or payload.get("tool")
        or ""
    )


def _looks_terminal_payload(payload: Any, args: Mapping[str, Any] | None = None) -> bool:
    if not isinstance(payload, Mapping):
        return False
    return any(key in payload for key in ("stdout", "stderr", "output")) and (
        _has_command_hint(payload)
        or _has_command_hint(args or {})
        or _has_numeric_exit_hint(payload)
    )


def _has_numeric_exit_hint(payload: Mapping[str, Any]) -> bool:
    for key in ("exit", "exit_code", "returncode", "return_code"):
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return True
    return False


def _has_command_hint(payload: Mapping[str, Any]) -> bool:
    for key in ("command", "cmd", "shell_command", "code"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return _usable_argv(payload.get("argv"))


def _usable_argv(argv: object) -> bool:
    return (
        isinstance(argv, list)
        and bool(argv)
        and all(isinstance(item, str) for item in argv)
        and bool(argv[0].strip())
    )


def _select_command(
    text: str,
    *sources: Mapping[str, Any],
    exit_code: int | None = None,
) -> str:
    evidence = _command_evidence_text(text)
    candidates = [command for source in sources for command in _commands_from_source(source)]

    classified = [
        (
            command,
            classify_command(command, "", exit_code=exit_code),
            classify_command(command, evidence, exit_code=exit_code),
        )
        for command in candidates
    ]
    for command, command_class, _evidence_class in classified:
        if (
            command_class in ALWAYS_PROTECTED_COMMAND_CLASSES
            and not _command_has_compactable_intent(command)
        ):
            return command
    output_class = (
        _compactable_command_output_class(classified[0][0], evidence) if classified else None
    )
    if output_class in NOISY_COMMAND_CLASSES:
        for command, _command_class, _evidence_class in classified[1:]:
            if _exact_command_output_class(command, evidence, exit_code=exit_code) is not None:
                return command
    if (
        classified
        and output_class in NOISY_COMMAND_CLASSES
        and classified[0][2] == output_class
    ):
        return classified[0][0]
    for command, command_class, evidence_class in classified:
        if (
            command_class in ALWAYS_PROTECTED_COMMAND_CLASSES
            and evidence_class in EXACT_COMMAND_CLASSES
        ):
            return command
    for command, _, evidence_class in classified:
        if evidence_class in OUTPUT_ASSISTED_COMMAND_CLASSES:
            return command
    for command, command_class, evidence_class in classified:
        if (
            command_class in CONDITIONALLY_PROTECTED_COMMAND_CLASSES
            and evidence_class in EXACT_COMMAND_CLASSES
        ):
            return command
    return candidates[0] if candidates else ""


def _command_evidence_text(text: str) -> str:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(parsed, str):
        return parsed
    if isinstance(parsed, dict):
        fields = [parsed.get(field) for field in TERMINAL_TEXT_FIELDS]
        values = [value for value in fields if isinstance(value, str)]
        if values:
            return "\n".join(values)
    return text
def _extract_command(payload: Mapping[str, JsonValue], args: Mapping[str, Any]) -> str:
    for source in (args, payload):
        for key in ("command", "cmd", "shell_command", "code"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value
        argv = source.get("argv")
        if (
            isinstance(argv, list)
            and argv
            and all(isinstance(item, str) for item in argv)
            and isinstance(argv[0], str)
            and argv[0].strip()
        ):
            return shlex.join([str(item) for item in argv])
    return ""


def _extract_exit_code(payload: Mapping[str, JsonValue], tool_name: str) -> int | None:
    for key in ("exit", "exit_code", "returncode", "return_code"):
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    status = payload.get("status")
    if (
        tool_name in TERMINAL_TOOL_NAMES
        and isinstance(status, str)
        and status.lower() in {"failed", "failure", "error", "errored"}
    ):
        return 1
    return None
