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
    _drop_artifact_if_notice_cannot_fit,
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
        if not _is_compactable_tool_name(tool_name) or not isinstance(result, str):
            return None
        options = NoisegateOptions.from_env().with_mapping(kwargs)
        if not options.enabled or options.mode == "off":
            return None

        parsed = json.loads(result)
        call_args = _combined_call_args(args, arguments)

        if isinstance(parsed, str):
            command = _extract_command({}, call_args)
            reduce_options = replace(options, artifact_enabled=False)
            reduced = reduce_text(
                parsed,
                command=command,
                tool_name=tool_name,
                source="json_string",
                options=reduce_options,
            )
            if not reduced.changed:
                return None
            metadata = dict(reduced.metadata)
            text = reduced.text
            preserve_patterns = _preserve_patterns_for(command, parsed)
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
        command = _extract_command(payload, call_args)
        exit_code = _extract_exit_code(payload, tool_name)
        fields = _candidate_fields(tool_name, payload)
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

        metadata_key = "_noisegate" if "noisegate" in payload else "noisegate"
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


def _preserve_patterns_for(
    command: str,
    text: str,
    *,
    exit_code: int | None = None,
) -> tuple[re.Pattern[str], ...] | None:
    command_class = classify_command(command, text, exit_code=exit_code)
    return _preserve_patterns_for_output(command_class, text)


def transform_terminal_output(
    *,
    command: str = "",
    output: str = "",
    exit_code: int = 0,
    returncode: int | None = None,
    **kwargs: Any,
) -> str | None:
    try:
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


def _combined_call_args(
    args: Mapping[str, Any] | None,
    arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    combined: dict[str, Any] = {}
    args_map = args if isinstance(args, Mapping) else {}
    arguments_map = arguments if isinstance(arguments, Mapping) else {}
    combined.update(arguments_map)
    combined.update(args_map)
    command = _extract_command({}, args_map) or _extract_command({}, arguments_map)
    if command:
        combined["command"] = command
    return combined


def _extract_command(payload: Mapping[str, JsonValue], args: Mapping[str, Any]) -> str:
    for source in (args, payload):
        for key in ("command", "cmd", "shell_command", "code"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value
    argv = args.get("argv")
    if isinstance(argv, list) and all(isinstance(item, str) for item in argv):
        return shlex.join(argv)
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
