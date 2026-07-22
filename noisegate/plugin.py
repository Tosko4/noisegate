from __future__ import annotations

import hashlib
import json
import re
import shlex
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from ._version import __version__
from .artifacts import ArtifactError, ArtifactStore, _capture_artifact_write_receipts
from .engine import (
    DIAGNOSTIC_LOCATION_PATTERNS,
    JsonValue,
    NoisegateOptions,
    _append_recovery_notices,
    _command_has_compactable_intent,
    _compactable_command_output_class,
    _drop_artifact_if_notice_cannot_fit,
    _exact_command_output_class,
    _fits_budget,
    _is_compactable_tool_name,
    _plan_artifact,
    _preserve_patterns_for_output,
    _raise_if_source_alignment_work_exhausted,
    _recovery_notices,
    _reduce_text_in_operation,
    _reduction_command_class,
    _source_alignment_work_operation,
    _SourceAlignmentWorkExhausted,
    _store_artifact,
    classify_command,
)
from .json_utils import DuplicateJSONKeyError, is_utf8_encodable, strict_json_loads

HookCallback: TypeAlias = Callable[..., str | None]

TERMINAL_TOOL_NAMES = frozenset({"terminal", "process", "read_terminal"})
WRAPPER_TOOL_NAMES = frozenset({"tool_call"})
FIELD_AWARE_WRITE_TOOL_NAMES = frozenset(
    {"write_file", "patch", "apply_patch", "edit_file", "replace_in_file"}
)
WRITE_DIAGNOSTIC_FIELDS = (
    "lsp_diagnostics",
    "diagnostics",
    "lint",
    "lint_output",
    "lint_errors",
    "typecheck",
    "typecheck_output",
    "pyright",
    "mypy",
    "tsc",
    "eslint",
    "errors",
    "warnings",
)
MAX_WRAPPER_JSON_CHARS = 65_536
MAX_NESTED_JSON_DEPTH = 8
MAX_NESTED_JSON_NODES = 512
JSON_NUMBER_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")
JSON5_OBJECT_KEY_RE = re.compile(
    r"(?:[A-Za-z_$][A-Za-z0-9_$.-]*|"
    r"[+-]?(?:(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|Infinity|NaN)|"
    r"[+-]?0[xX][0-9A-Fa-f]+)\s*:"
)
TERMINAL_TEXT_FIELDS = ("stdout", "stderr", "output")
PROCESS_TEXT_FIELDS = (
    "stdout",
    "stderr",
    "output",
    "logs",
    "log",
    "new_output",
    "output_preview",
)
ALWAYS_PROTECTED_COMMAND_CLASSES = frozenset(
    {
        "file_read",
        "source_search",
        "patch",
        "systemctl_show",
        "memory_retrieval",
        "perseus_prepare",
    }
)
CONDITIONALLY_PROTECTED_COMMAND_CLASSES = frozenset({"git_diff"})
EXACT_COMMAND_CLASSES = (
    ALWAYS_PROTECTED_COMMAND_CLASSES | CONDITIONALLY_PROTECTED_COMMAND_CLASSES
)
OUTPUT_ASSISTED_COMMAND_CLASSES = frozenset({"file_read", "source_search"})
NOISY_COMMAND_CLASSES = frozenset(
    {
        "apt",
        "docker_build",
        "docker_logs",
        "log_stream",
        "node",
        "pytest",
        "python_package",
        "unittest",
    }
)
COMMAND_ALIASES = ("command", "cmd", "shell_command", "code")
PROCESS_COMMAND_CONTAINERS = ("process", "session", "metadata")
PROCESS_LOG_ACTIONS = frozenset({"log", "poll", "wait"})
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


@dataclass(frozen=True, slots=True)
class _ArtifactNoticeOwnerStep:
    field: str
    parse_json: bool = False


@dataclass(frozen=True, slots=True)
class _ArtifactPreviewPlan:
    original_text: str
    artifact_id: str
    sha256: str
    size_bytes: int
    recovery_notice: str = ""
    owner_path: tuple[_ArtifactNoticeOwnerStep, ...] = ()


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
    kwargs.pop("defer_artifact_store", None)
    kwargs.pop("_share_alignment_budget", None)
    return _transform_tool_result(
        result,
        tool_name=tool_name,
        args=args,
        arguments=arguments,
        defer_artifact_store=False,
        artifact_plans_out=None,
        **kwargs,
    )


def _preview_tool_result(
    result: str = "",
    *,
    tool_name: str = "",
    args: Mapping[str, Any] | None = None,
    arguments: Mapping[str, Any] | None = None,
    artifact_plans_out: list[_ArtifactPreviewPlan] | None = None,
    **kwargs: Any,
) -> str | None:
    kwargs.pop("_share_alignment_budget", None)
    return _transform_tool_result(
        result,
        tool_name=tool_name,
        args=args,
        arguments=arguments,
        defer_artifact_store=True,
        artifact_plans_out=artifact_plans_out,
        **kwargs,
    )


def _transform_tool_result_in_operation(
    result: str = "",
    *,
    tool_name: str = "",
    args: Mapping[str, Any] | None = None,
    arguments: Mapping[str, Any] | None = None,
    defer_artifact_store: bool = False,
    artifact_plans_out: list[_ArtifactPreviewPlan] | None = None,
    **kwargs: Any,
) -> str | None:
    return _transform_tool_result(
        result,
        tool_name=tool_name,
        args=args,
        arguments=arguments,
        defer_artifact_store=defer_artifact_store,
        artifact_plans_out=artifact_plans_out,
        _share_alignment_budget=True,
        **kwargs,
    )


def _transform_tool_result(
    result: str,
    *,
    tool_name: str,
    args: Mapping[str, Any] | None,
    arguments: Mapping[str, Any] | None,
    defer_artifact_store: bool,
    artifact_plans_out: list[_ArtifactPreviewPlan] | None,
    _share_alignment_budget: bool = False,
    **kwargs: Any,
) -> str | None:
    local_plans: list[_ArtifactPreviewPlan] | None = (
        [] if artifact_plans_out is not None else None
    )

    def transform() -> str | None:
        transformed = _transform_tool_result_with_budget(
            result,
            tool_name=tool_name,
            args=args,
            arguments=arguments,
            defer_artifact_store=defer_artifact_store,
            artifact_plans_out=local_plans,
            **kwargs,
        )
        _raise_if_source_alignment_work_exhausted()
        if transformed is not None and not is_utf8_encodable(transformed):
            return None
        if transformed is not None and artifact_plans_out is not None and local_plans:
            artifact_plans_out.extend(local_plans)
        return transformed

    if _share_alignment_budget:
        return transform()
    try:
        with _source_alignment_work_operation():
            return transform()
    except Exception:
        return None


def _transform_tool_result_with_budget(
    result: str,
    *,
    tool_name: str,
    args: Mapping[str, Any] | None,
    arguments: Mapping[str, Any] | None,
    defer_artifact_store: bool,
    artifact_plans_out: list[_ArtifactPreviewPlan] | None,
    **kwargs: Any,
) -> str | None:
    try:
        override = kwargs.pop("noisegate_exit_code", None)
        disable_artifacts = bool(kwargs.pop("_disable_artifacts", False))
        exit_code_override = (
            override if isinstance(override, int) and not isinstance(override, bool) else None
        )
        if not isinstance(result, str):
            return None
        args_map = args if isinstance(args, Mapping) else {}
        arguments_map = arguments if isinstance(arguments, Mapping) else {}
        effective_tool_name = tool_name
        resolved_wrapper: _ResolvedWrapperCall | None = None
        if tool_name in WRAPPER_TOOL_NAMES:
            resolved_wrapper = _resolve_wrapped_call(args_map, arguments_map)
            if resolved_wrapper is None:
                return None
            effective_tool_name = resolved_wrapper.tool_name
        if (
            effective_tool_name
            and effective_tool_name not in FIELD_AWARE_WRITE_TOOL_NAMES
            and not _is_compactable_tool_name(effective_tool_name)
        ):
            return None
        options = NoisegateOptions.from_env().with_mapping(kwargs)
        if disable_artifacts:
            options = replace(options, artifact_enabled=False)
        if not options.enabled or options.mode == "off":
            return None

        parsed = strict_json_loads(result)
        if resolved_wrapper is not None:
            args_map = dict(resolved_wrapper.call_args)
            arguments_map = {}
            wrapper_tool_names = _tool_names_from_payload(
                args_map,
                root_name_is_wrapper_owned=True,
            )
            if len(wrapper_tool_names) > 1 or (
                wrapper_tool_names and effective_tool_name not in wrapper_tool_names
            ):
                return None
            result_sources = (
                list(_command_sources_from_payload(parsed))
                if isinstance(parsed, Mapping)
                else []
            )
            wrapper_sources = list(_command_sources_from_payload(args_map))
            commands = {
                command
                for source in (*wrapper_sources, *result_sources)
                for command in _commands_from_source(source)
            }
            if len(commands) > 1:
                return None
            if commands and not _has_command_hint(args_map):
                args_map["command"] = next(iter(commands))
            call_args = args_map
        else:
            call_args = _call_args(
                args,
                arguments,
                parsed,
                prefer_host_args=bool(tool_name),
            )
        tool_name = effective_tool_name
        if isinstance(parsed, Mapping):
            embedded_tool_names = _tool_names_from_payload(
                parsed,
                root_name_is_wrapper_owned=False,
            )
            if len(embedded_tool_names) > 1:
                return None
            if embedded_tool_names:
                embedded_tool_name = next(iter(embedded_tool_names))
                if (
                    embedded_tool_name not in FIELD_AWARE_WRITE_TOOL_NAMES
                    and not _is_compactable_tool_name(embedded_tool_name)
                ):
                    return None
                if tool_name and embedded_tool_name != tool_name:
                    return None
                tool_name = embedded_tool_name
        if not tool_name:
            if isinstance(parsed, Mapping) and "name" in parsed:
                return None
            if not _looks_terminal_payload(parsed, call_args):
                return None
            tool_name = "terminal"

        if tool_name in FIELD_AWARE_WRITE_TOOL_NAMES:
            if not isinstance(parsed, dict):
                return None
            write_exit_code = _extract_exit_code(parsed, tool_name)
            if write_exit_code is None:
                write_exit_code = exit_code_override
            return _transform_write_diagnostic_fields(
                result,
                parsed,
                tool_name=tool_name,
                call_args=call_args,
                args_map=args_map,
                arguments_map=arguments_map,
                exit_code=write_exit_code,
                options=options,
            )

        if isinstance(parsed, str):
            command = _select_command(
                parsed,
                call_args,
                args_map,
                arguments_map,
                exit_code=exit_code_override,
            )
            reduce_options = replace(options, artifact_enabled=False)
            reduced = _reduce_text_in_operation(
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
                tool_name=tool_name,
                exit_code=exit_code_override,
            )
            if options.artifact_enabled:
                metadata["artifact"] = _plan_artifact(parsed, options)
                notice_metadata = dict(metadata)
                notice_metadata["exit_code"] = None
                _drop_artifact_if_notice_cannot_fit(
                    notice_metadata,
                    options,
                    artifact_dir=options.artifact_dir,
                )
                metadata["artifact"] = notice_metadata["artifact"]
                text = _append_recovery_notices(
                    text,
                    notice_metadata,
                    artifact_dir=options.artifact_dir,
                    options=options,
                    preserve_patterns=preserve_patterns,
                    fail_open_text=parsed,
                )
                _mark_artifact_notice_dropped_if_missing(
                    metadata,
                    text,
                    artifact_dir=options.artifact_dir,
                )
            if not _valid_transformed_text(
                original=parsed,
                transformed=text,
                metadata=metadata,
                options=options,
            ):
                return None
            candidate = json.dumps(text, ensure_ascii=False)
            if len(candidate) >= len(result) or not is_utf8_encodable(candidate):
                return None
            if options.artifact_enabled and not defer_artifact_store:
                artifact = metadata.get("artifact")
                if isinstance(artifact, dict) and artifact.get("stored") is True:
                    _raise_if_source_alignment_work_exhausted()
                    metadata["artifact"] = _store_artifact(parsed, options)
                    notice_metadata = dict(metadata)
                    notice_metadata["exit_code"] = None
                    text = _append_recovery_notices(
                        reduced.text,
                        notice_metadata,
                        artifact_dir=options.artifact_dir,
                        options=options,
                        preserve_patterns=preserve_patterns,
                        fail_open_text=parsed,
                    )
                    if not _valid_transformed_text(
                        original=parsed,
                        transformed=text,
                        metadata=metadata,
                        options=options,
                    ):
                        return None
                    candidate = json.dumps(text, ensure_ascii=False)
            if len(candidate) >= len(result):
                return None
            if defer_artifact_store and artifact_plans_out is not None:
                plan = _artifact_preview_plan(
                    parsed,
                    metadata,
                    artifact_dir=options.artifact_dir,
                )
                artifact = metadata.get("artifact")
                if isinstance(artifact, dict) and artifact.get("stored") is True:
                    if plan is None or not _artifact_preview_plan_matches_serialized_output(
                        plan,
                        candidate,
                    ):
                        return None
                    artifact_plans_out.append(plan)
            return candidate

        if not isinstance(parsed, dict):
            return None

        payload: dict[str, JsonValue] = dict(parsed)
        exit_code = _extract_exit_code(payload, tool_name)
        if exit_code is None:
            exit_code = exit_code_override
        fields = _candidate_fields(tool_name, payload)
        if (
            resolved_wrapper is not None
            and isinstance(payload.get("result"), str)
            and "result" not in fields
        ):
            fields = (*fields, "result")
        if options.artifact_enabled and sum(
            isinstance(payload.get(field), str) and bool(payload.get(field)) for field in fields
        ) > 1:
            options = replace(options, artifact_enabled=False)
        command_text = "\n".join(
            value
            for field in fields
            if isinstance((value := payload.get(field)), str)
        )
        if tool_name == "process":
            command = _select_process_command(
                command_text,
                payload,
                call_args,
                args_map,
                arguments_map,
                exit_code=exit_code,
            )
        else:
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
        preview_plans_by_field: dict[str, _ArtifactPreviewPlan] = {}
        preserve_patterns_by_field: dict[str, tuple[re.Pattern[str], ...] | None] = {}
        reduce_options = replace(options, artifact_enabled=False)
        commandless_process_log_fallback_allowed = (
            tool_name != "process"
            or bool(command)
            or _process_commandless_log_fallback_allowed(
                payload,
                call_args,
                args_map,
                arguments_map,
            )
        )

        for field in fields:
            value = payload.get(field)
            if not isinstance(value, str):
                continue
            if (
                tool_name == "process"
                and not command
                and (
                    not commandless_process_log_fallback_allowed
                    or _reduction_command_class(
                        "",
                        value,
                        tool_name=tool_name,
                        exit_code=exit_code,
                    )
                    != "log_stream"
                )
            ):
                continue
            nested_json = _nested_json_text_requires_exact(value)
            if nested_json and field == "result" and tool_name in TERMINAL_TOOL_NAMES:
                nested_payload = strict_json_loads(value)
                if not isinstance(nested_payload, Mapping):
                    continue
                nested_call_args = dict(call_args)
                nested_commands = {
                    command
                    for source in (
                        *_command_sources_from_payload(nested_call_args),
                        *_command_sources_from_payload(nested_payload),
                    )
                    for command in _commands_from_source(source)
                }
                if len(nested_commands) > 1:
                    return None
                if nested_commands:
                    nested_call_args["command"] = next(iter(nested_commands))
                nested_kwargs = dict(kwargs)
                nested_transformed = _transform_tool_result_with_budget(
                    value,
                    tool_name=tool_name,
                    args=nested_call_args,
                    arguments=None,
                    defer_artifact_store=False,
                    artifact_plans_out=None,
                    _disable_artifacts=True,
                    **nested_kwargs,
                )
                if nested_transformed is not None:
                    payload[field] = nested_transformed
                    field_metadata[field] = {
                        "version": __version__,
                        "compacted": True,
                        "mode": options.mode,
                        "reducer": "nested_json",
                        "original_chars": len(value),
                        "omitted_chars": max(0, len(value) - len(nested_transformed)),
                    }
                continue
            if nested_json:
                continue
            reduced = _reduce_text_in_operation(
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
                    preserve_patterns = _preserve_patterns_for(
                        command,
                        value,
                        tool_name=tool_name,
                        exit_code=exit_code,
                    )
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
                        fail_open_text=value,
                    )
                    _mark_artifact_notice_dropped_if_missing(
                        metadata,
                        text,
                        artifact_dir=options.artifact_dir,
                    )
                if not _valid_transformed_text(
                    original=value,
                    transformed=text,
                    metadata=metadata,
                    options=options,
                ):
                    continue
                payload[field] = text
                field_metadata[field] = metadata
                original_values[field] = value
                reduced_values[field] = reduced.text
                preserve_patterns_by_field[field] = preserve_patterns
                plan = _artifact_preview_plan(
                    value,
                    metadata,
                    artifact_dir=options.artifact_dir,
                    owner_path=(_ArtifactNoticeOwnerStep(field),),
                )
                if plan is not None:
                    preview_plans_by_field[field] = plan

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
        candidate = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(candidate) >= len(result) or not is_utf8_encodable(candidate):
            return None

        if options.artifact_enabled and not defer_artifact_store:
            for field, value in original_values.items():
                metadata = field_metadata.get(field)
                reduced_text = reduced_values.get(field)
                if isinstance(metadata, dict) and isinstance(reduced_text, str):
                    artifact = metadata.get("artifact")
                    if isinstance(artifact, dict) and artifact.get("stored") is True:
                        _raise_if_source_alignment_work_exhausted()
                        metadata["artifact"] = _store_artifact(value, options)
                    notice_metadata = dict(metadata)
                    notice_metadata["exit_code"] = None
                    text = _append_recovery_notices(
                        reduced_text,
                        notice_metadata,
                        artifact_dir=options.artifact_dir,
                        options=options,
                        preserve_patterns=preserve_patterns_by_field.get(field),
                        fail_open_text=value,
                    )
                    if not _valid_transformed_text(
                        original=value,
                        transformed=text,
                        metadata=metadata,
                        options=options,
                    ):
                        return None
                    payload[field] = text
            candidate = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        if len(candidate) >= len(result):
            return None
        if defer_artifact_store and artifact_plans_out is not None:
            for field, metadata in field_metadata.items():
                artifact = metadata.get("artifact") if isinstance(metadata, dict) else None
                if not isinstance(artifact, dict) or artifact.get("stored") is not True:
                    continue
                plan = preview_plans_by_field.get(field)
                if plan is None or not _artifact_preview_plan_matches_serialized_output(
                    plan,
                    candidate,
                ):
                    return None
                artifact_plans_out.append(plan)
        return candidate
    except _SourceAlignmentWorkExhausted:
        raise
    except Exception:
        return None


def _mark_artifact_notice_dropped_if_missing(
    metadata: dict[str, JsonValue],
    text: str,
    *,
    artifact_dir: Path | None,
) -> None:
    artifact = metadata.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("stored") is not True:
        return
    notice = _artifact_recovery_notice(metadata, artifact_dir=artifact_dir)
    if notice is not None and notice in text.splitlines():
        return
    metadata["artifact"] = {
        "stored": False,
        "reason": "recovery_notice_dropped",
        "size_bytes": artifact.get("size_bytes"),
    }


def _artifact_preview_plan(
    original_text: str,
    metadata: Mapping[str, JsonValue],
    *,
    artifact_dir: Path | None = None,
    owner_path: tuple[_ArtifactNoticeOwnerStep, ...] = (),
) -> _ArtifactPreviewPlan | None:
    artifact = metadata.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("stored") is not True:
        return None
    artifact_id = artifact.get("id")
    sha256 = artifact.get("sha256")
    size_bytes = artifact.get("size_bytes")
    data = original_text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    recovery_notice = _artifact_recovery_notice(metadata, artifact_dir=artifact_dir)
    if (
        not isinstance(artifact_id, str)
        or artifact_id != f"ng_{digest[:24]}"
        or sha256 != digest
        or not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes != len(data)
        or recovery_notice is None
    ):
        return None
    return _ArtifactPreviewPlan(
        original_text=original_text,
        artifact_id=artifact_id,
        sha256=digest,
        size_bytes=size_bytes,
        recovery_notice=recovery_notice,
        owner_path=owner_path,
    )


def _artifact_recovery_notice(
    metadata: Mapping[str, JsonValue],
    *,
    artifact_dir: Path | None,
) -> str | None:
    notices = [
        notice
        for notice in _recovery_notices(dict(metadata), artifact_dir=artifact_dir)
        if notice.startswith("[noisegate artifact:")
    ]
    return notices[0] if len(notices) == 1 else None


def _artifact_preview_plan_notice_present_in_text(
    plan: _ArtifactPreviewPlan,
    text: str,
) -> bool:
    return bool(plan.recovery_notice) and plan.recovery_notice in text.splitlines()


def _artifact_preview_plan_with_owner_prefix(
    plan: _ArtifactPreviewPlan,
    prefix: tuple[_ArtifactNoticeOwnerStep, ...],
) -> _ArtifactPreviewPlan:
    return replace(plan, owner_path=(*prefix, *plan.owner_path))


def _artifact_preview_plan_matches_serialized_output(
    plan: _ArtifactPreviewPlan,
    output: str,
) -> bool:
    try:
        owner: Any = strict_json_loads(output)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return False
    for step in plan.owner_path:
        if not isinstance(owner, dict) or step.field not in owner:
            return False
        owner = owner[step.field]
        if step.parse_json:
            if not isinstance(owner, str):
                return False
            try:
                owner = strict_json_loads(owner)
            except (json.JSONDecodeError, RecursionError, ValueError):
                return False
    return isinstance(owner, str) and _artifact_preview_plan_notice_present_in_text(plan, owner)


def _store_artifact_preview_plan(
    plan: _ArtifactPreviewPlan,
    options: NoisegateOptions,
) -> bool:
    planned = _artifact_preview_plan(
        plan.original_text,
        {
            "artifact": {
                "stored": True,
                "id": plan.artifact_id,
                "sha256": plan.sha256,
                "size_bytes": plan.size_bytes,
            }
        },
        artifact_dir=options.artifact_dir,
        owner_path=plan.owner_path,
    )
    if planned != plan:
        return False

    store = ArtifactStore(
        options.artifact_dir,
        size_cap=options.artifact_size_cap,
    )
    try:
        try:
            root_stat = store.root.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISDIR(root_stat.st_mode):
                return False
            root = store._ensure_root()
            target = store._path_for(plan.artifact_id, root=root)
            try:
                target_stat = target.lstat()
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(target_stat.st_mode):
                    return False
    except (ArtifactError, OSError):
        return False

    receipt = None
    cleanup_receipt = None
    verified = False
    try:
        with _capture_artifact_write_receipts() as receipts:
            try:
                stored = _store_artifact(plan.original_text, options)
            finally:
                matching_receipts = []
                matching_receipt_keys: set[tuple[str, bool, int, int]] = set()
                for candidate in receipts:
                    artifact = candidate.artifact
                    if (
                        artifact.artifact_id != plan.artifact_id
                        or artifact.sha256 != plan.sha256
                        or artifact.size_bytes != plan.size_bytes
                    ):
                        continue
                    key = (
                        artifact.artifact_id,
                        candidate.created,
                        candidate.device,
                        candidate.inode,
                    )
                    if key in matching_receipt_keys:
                        continue
                    matching_receipt_keys.add(key)
                    matching_receipts.append(candidate)
                if len(matching_receipts) == 1:
                    receipt = matching_receipts[0]
                created_receipts = [
                    candidate for candidate in matching_receipts if candidate.created
                ]
                if len(created_receipts) == 1:
                    cleanup_receipt = created_receipts[0]
        if (
            stored.get("stored") is not True
            or receipt is None
            or stored.get("id") != plan.artifact_id
            or stored.get("sha256") != plan.sha256
            or stored.get("size_bytes") != plan.size_bytes
        ):
            return False
        root = store._ensure_root()
        target = store._path_for(plan.artifact_id, root=root)
        final_stat = target.lstat()
        if (
            not stat.S_ISREG(final_stat.st_mode)
            or final_stat.st_dev != receipt.device
            or final_stat.st_ino != receipt.inode
        ):
            return False
        resolved = store.read(plan.artifact_id)
        if resolved != plan.original_text:
            return False
        verified = True
        return True
    except Exception:
        return False
    finally:
        if not verified and cleanup_receipt is not None:
            store._remove_created_artifact(cleanup_receipt)


def _valid_transformed_text(
    *,
    original: str,
    transformed: str,
    metadata: dict[str, JsonValue],
    options: NoisegateOptions,
) -> bool:
    if len(transformed) >= len(original) or not _fits_budget(transformed, options):
        return False

    required_notices: list[str] = []
    exit_code = metadata.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
        required_notices.append(f"[noisegate: exit_code={exit_code}]")

    artifact = metadata.get("artifact")
    if isinstance(artifact, dict) and artifact.get("stored") is True:
        required_notices.extend(
            notice
            for notice in _recovery_notices(metadata, artifact_dir=options.artifact_dir)
            if notice.startswith("[noisegate artifact:")
        )

    transformed_lines = transformed.splitlines()
    return all(notice in transformed_lines for notice in required_notices)


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
    tool_name: str | None = None,
    exit_code: int | None = None,
) -> tuple[re.Pattern[str], ...] | None:
    command_class = _reduction_command_class(
        command,
        text,
        tool_name=tool_name,
        exit_code=exit_code,
    )
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
        with _source_alignment_work_operation():
            transformed = _transform_terminal_output_with_budget(
                *positional,
                command=command,
                output=output,
                exit_code=exit_code,
                returncode=returncode,
                **kwargs,
            )
            _raise_if_source_alignment_work_exhausted()
            return transformed
    except Exception:
        return None


def _transform_terminal_output_with_budget(
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
        if not options.enabled or options.mode == "off":
            return None
        # This pre-envelope hook may receive a complete machine-readable document;
        # preserve strict JSON exactly rather than reducing it as generic text.
        try:
            strict_json_loads(output)
        except json.JSONDecodeError:
            pass
        else:
            return None
        # Hermes calls transform_terminal_output before its built-in terminal
        # redaction pass. Inline compaction is still safe because Hermes redacts
        # the returned string afterwards, but raw artifact storage would persist
        # pre-redaction output. Keep artifacts disabled for this early hook.
        options = replace(options, artifact_enabled=False)
        selected_exit_code = (
            returncode
            if isinstance(returncode, int) and not isinstance(returncode, bool)
            else exit_code
            if isinstance(exit_code, int) and not isinstance(exit_code, bool)
            else None
        )
        reduced = _reduce_text_in_operation(
            output,
            command=command,
            tool_name="terminal",
            source="terminal_output",
            exit_code=selected_exit_code,
            options=options,
        )
        return reduced.text if reduced.changed else None
    except Exception:
        return None


def _candidate_fields(tool_name: str, payload: Mapping[str, JsonValue]) -> tuple[str, ...]:
    if tool_name == "process":
        candidates = PROCESS_TEXT_FIELDS
    elif tool_name in TERMINAL_TOOL_NAMES:
        candidates = TERMINAL_TEXT_FIELDS
    else:
        candidates = GENERIC_TEXT_FIELDS
    return tuple(field for field in candidates if field in payload)


def _transform_write_diagnostic_fields(
    result: str,
    parsed: Mapping[str, JsonValue],
    *,
    tool_name: str,
    call_args: Mapping[str, Any],
    args_map: Mapping[str, Any],
    arguments_map: Mapping[str, Any],
    exit_code: int | None,
    options: NoisegateOptions,
) -> str | None:
    """Compact only allowlisted diagnostic strings from write-like results."""

    fields = tuple(field for field in WRITE_DIAGNOSTIC_FIELDS if field in parsed)
    if not fields:
        return None

    diagnostic_values: dict[str, str] = {}
    for field in fields:
        value = parsed.get(field)
        if not isinstance(value, str) or _nested_json_text_requires_exact(value):
            return None
        diagnostic_values[field] = value

    # Every non-allowlisted field is exact evidence. This protects content,
    # source, diff, patch, result, output, text, and future source-bearing keys
    # without guessing their shape. Diagnostics also stay inline-only because
    # they may include private paths and source excerpts.
    reduce_options = replace(options, artifact_enabled=False)
    command_text = "\n".join(diagnostic_values.values())
    command = _select_command(
        command_text,
        call_args,
        args_map,
        arguments_map,
        parsed,
        exit_code=exit_code,
    )
    payload: dict[str, JsonValue] = dict(parsed)
    field_metadata: dict[str, JsonValue] = {}

    for field in fields:
        value = diagnostic_values[field]
        reduced = _reduce_text_in_operation(
            value,
            command=command,
            # The tool remains protected at the engine boundary. This scoped
            # plugin path has already selected one diagnostic string field.
            tool_name=None,
            source=f"json_field:{field}",
            exit_code=exit_code,
            options=reduce_options,
            extra_preserve_patterns=DIAGNOSTIC_LOCATION_PATTERNS,
        )
        if not reduced.changed:
            return None
        metadata = dict(reduced.metadata)
        metadata["tool_name"] = tool_name
        if not _valid_transformed_text(
            original=value,
            transformed=reduced.text,
            metadata=metadata,
            options=reduce_options,
        ):
            return None
        payload[field] = reduced.text
        field_metadata[field] = metadata

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
    candidate = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return candidate if len(candidate) < len(result) else None


def _nested_json_text_requires_exact(value: str) -> bool:
    """Validate JSON-encoded text recursively and keep the complete field exact."""

    remaining = MAX_NESTED_JSON_NODES

    def looks_structural(text: str) -> bool:
        sample = text if len(text) <= MAX_WRAPPER_JSON_CHARS else text[:256]
        stripped = sample.lstrip()
        if stripped.startswith("\ufeff"):
            stripped = stripped[1:].lstrip()
        if not stripped:
            return len(sample) < len(text)
        if stripped.startswith('"'):
            return True
        tail = stripped[1:].lstrip()
        if stripped.startswith("{"):
            return (
                not tail
                or tail.startswith(('"', "'", "}"))
                or JSON5_OBJECT_KEY_RE.match(tail) is not None
            )
        if stripped.startswith("["):
            array_like = (
                not tail
                or tail.startswith(('"', "'", "{", "[", "]", "-"))
                or tail[:1].isdigit()
                or tail.startswith(
                    ("true", "false", "null", "undefined", "NaN", "Infinity")
                )
            )
            if not array_like:
                return False
            if tail.startswith("-") or tail[:1].isdigit():
                closing = stripped.find("]")
                if closing >= 0 and stripped[closing + 1 :].strip():
                    return "," in stripped[1:closing]
            return True
        if stripped.rstrip() in {
            "true",
            "false",
            "null",
            "NaN",
            "Infinity",
            "-Infinity",
        }:
            return True
        return JSON_NUMBER_RE.fullmatch(stripped.rstrip()) is not None

    def looks_nested_json_candidate(text: str) -> bool:
        sample = text if len(text) <= MAX_WRAPPER_JSON_CHARS else text[:256]
        stripped = sample.lstrip()
        return looks_structural(text) or stripped.rstrip() in {
            "NaN",
            "Infinity",
            "-Infinity",
        }

    def inspect(node: Any, depth: int) -> None:
        nonlocal remaining
        if depth > MAX_NESTED_JSON_DEPTH or remaining <= 0:
            raise ValueError("nested JSON validation budget exhausted")
        remaining -= 1
        if isinstance(node, Mapping):
            for child in node.values():
                inspect(child, depth + 1)
            return
        if isinstance(node, list):
            for child in node:
                inspect(child, depth + 1)
            return
        if not isinstance(node, str):
            return
        json_like = looks_nested_json_candidate(node)
        if len(node) > MAX_WRAPPER_JSON_CHARS:
            if json_like:
                raise ValueError("nested JSON text exceeds size limit")
            return
        if not json_like:
            return
        try:
            decoded = strict_json_loads(node)
        except DuplicateJSONKeyError:
            raise
        except json.JSONDecodeError as exc:
            raise ValueError("malformed nested JSON text") from exc
        inspect(decoded, depth + 1)

    json_like = looks_structural(value)
    if len(value) > MAX_WRAPPER_JSON_CHARS:
        if json_like:
            raise ValueError("nested JSON text exceeds size limit")
        return False
    if not json_like:
        return False
    try:
        decoded = strict_json_loads(value)
    except DuplicateJSONKeyError:
        raise
    except json.JSONDecodeError as exc:
        raise ValueError("malformed JSON-like text") from exc
    inspect(decoded, 0)
    return True


@dataclass(frozen=True, slots=True)
class _ResolvedWrapperCall:
    tool_name: str
    call_args: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _WrapperIdentity:
    tool_name: str
    owner: Mapping[str, Any]
    owner_path: tuple[Mapping[str, Any], ...]


def _resolve_wrapped_call(*sources: Mapping[str, Any]) -> _ResolvedWrapperCall | None:
    remaining = 64
    visited: set[int] = set()

    def mapping(value: Any) -> Mapping[str, Any] | None:
        nonlocal remaining
        if isinstance(value, Mapping):
            return value
        if not isinstance(value, str):
            return None
        if len(value) > MAX_WRAPPER_JSON_CHARS:
            raise ValueError("encoded wrapper arguments are too large")
        stripped = value.strip()
        if not stripped.startswith("{"):
            return None
        remaining -= 1
        if remaining < 0:
            raise ValueError("wrapper traversal budget exhausted")
        parsed = strict_json_loads(value)
        return parsed if isinstance(parsed, Mapping) else None

    def enter(value: Mapping[str, Any], depth: int) -> None:
        nonlocal remaining
        if depth > 8:
            raise ValueError("wrapper traversal depth exhausted")
        remaining -= 1
        identity = id(value)
        if remaining < 0 or identity in visited:
            raise ValueError("wrapper traversal is cyclic, shared, or too large")
        visited.add(identity)

    def direct_identity(value: Mapping[str, Any], depth: int) -> _WrapperIdentity | None:
        enter(value, depth)
        identities: list[_WrapperIdentity] = []
        for key in ("tool_name", "toolName", "name"):
            if key not in value:
                continue
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                identities.append(_WrapperIdentity(candidate.strip(), value, (value,)))
            elif candidate not in (None, ""):
                raise ValueError("invalid wrapped tool identity")
        tool = value.get("tool")
        if isinstance(tool, str) and tool.strip():
            identities.append(_WrapperIdentity(tool.strip(), value, (value,)))
        else:
            tool_mapping = mapping(tool)
            if tool_mapping is not None:
                nested = direct_identity(tool_mapping, depth + 1)
                if nested is not None:
                    identities.append(
                        replace(nested, owner_path=(value, *nested.owner_path))
                    )
            elif "tool" in value and tool not in (None, ""):
                raise ValueError("invalid object-form wrapped tool identity")
        if len(identities) > 1:
            raise ValueError("ambiguous wrapped tool identity")
        return identities[0] if identities else None

    def argument_container(value: Mapping[str, Any]) -> Mapping[str, Any] | None:
        candidates: list[Mapping[str, Any]] = []
        for key in ("arguments", "args", "input"):
            if key not in value:
                continue
            candidate = mapping(value.get(key))
            if candidate is None:
                raise ValueError("invalid wrapped argument container")
            candidates.append(candidate)
        if len(candidates) > 1:
            raise ValueError("ambiguous wrapped argument containers")
        return candidates[0] if candidates else None

    def call_args(
        value: Mapping[str, Any],
        identity_owner: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        def validated(candidate: Mapping[str, Any]) -> Mapping[str, Any]:
            if len(set(_commands_from_source(candidate))) > 1:
                raise ValueError("conflicting wrapped command hints")
            return candidate

        owner_args = argument_container(identity_owner)
        parent_args = argument_container(value) if identity_owner is not value else None
        command_sources = [identity_owner]
        if identity_owner is not value:
            command_sources.append(value)
        command_sources.extend(
            candidate for candidate in (owner_args, parent_args) if candidate is not None
        )
        commands = {
            command
            for source in command_sources
            for command in _commands_from_source(source)
        }
        if len(commands) > 1:
            raise ValueError("conflicting wrapped command ownership")
        if owner_args is not None and parent_args is not None:
            raise ValueError("ambiguous wrapped argument ownership")
        if owner_args is not None:
            return validated(owner_args)
        if parent_args is not None:
            return validated(parent_args)
        if _has_command_hint(identity_owner):
            return validated(identity_owner)
        if identity_owner is not value and _has_command_hint(value):
            return validated(value)
        return {}

    def resolve(
        value: Mapping[str, Any],
        depth: int,
        *,
        allow_container: bool,
        inherited_commands: frozenset[str],
    ) -> _ResolvedWrapperCall | None:
        path_commands = set(inherited_commands)
        path_commands.update(_commands_from_source(value))
        if len(path_commands) > 1:
            raise ValueError("conflicting intermediate wrapper commands")
        identity = direct_identity(value, depth)
        if identity is not None:
            for owner in identity.owner_path:
                path_commands.update(_commands_from_source(owner))
            if len(path_commands) > 1:
                raise ValueError("conflicting nested wrapper ownership")
        name = identity.tool_name if identity is not None else ""
        if identity is not None and name not in WRAPPER_TOOL_NAMES:
            if any(
                key in owner
                for owner in identity.owner_path
                for key in ("call", "request")
            ):
                raise ValueError("ambiguous sibling wrapped call ownership")
            if any(
                key in owner
                for owner in identity.owner_path[1:-1]
                for key in ("args", "arguments", "input")
            ):
                raise ValueError("ambiguous intermediate wrapped argument ownership")
            resolved_args = call_args(value, identity.owner)
            path_commands.update(_commands_from_source(resolved_args))
            if len(path_commands) > 1:
                raise ValueError("conflicting wrapped command path")
            if path_commands and not _has_command_hint(resolved_args):
                resolved_args = {
                    **resolved_args,
                    "command": next(iter(path_commands)),
                }
            return _ResolvedWrapperCall(name, resolved_args)

        if name:
            container_keys = ("args", "arguments", "input", "call", "request")
        elif allow_container:
            if any(key in value for key in ("call", "request")) and any(
                key in value for key in ("args", "arguments", "input")
            ):
                raise ValueError("ambiguous identityless wrapped call ownership")
            container_keys = ("call", "request")
        else:
            container_keys = ()
        candidates: list[Mapping[str, Any]] = []
        container_owners = [identity.owner] if identity is not None else [value]
        if identity is not None and identity.owner is not value:
            container_owners.append(value)
        for owner in container_owners:
            for key in container_keys:
                if key not in owner:
                    continue
                candidate = mapping(owner.get(key))
                if candidate is None:
                    raise ValueError("invalid wrapped call container")
                candidates.append(candidate)
        if len(candidates) > 1:
            raise ValueError("ambiguous wrapped call containers")
        if not candidates:
            return None
        return resolve(
            candidates[0],
            depth + 1,
            allow_container=True,
            inherited_commands=frozenset(path_commands),
        )

    try:
        resolved_sources: list[_ResolvedWrapperCall] = []
        for source in sources:
            resolved = resolve(
                source,
                0,
                allow_container=True,
                inherited_commands=frozenset(),
            )
            if resolved is not None:
                resolved_sources.append(resolved)
            elif source:
                raise ValueError("detached wrapped argument source")
    except (json.JSONDecodeError, RecursionError, ValueError):
        return None
    if len(resolved_sources) != 1:
        return None
    return resolved_sources[0]


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


def _command_sources_from_payload(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    sources: list[Mapping[str, Any]] = []
    visited: set[int] = set()
    remaining = 64

    def visit(value: Mapping[str, Any], depth: int) -> None:
        nonlocal remaining
        if depth > 8 or remaining <= 0 or id(value) in visited:
            raise ValueError("ambiguous command ownership graph")
        remaining -= 1
        visited.add(id(value))
        sources.append(value)
        for key in ("args", "arguments", "input", "call", "request", "tool"):
            candidate: Any = value.get(key)
            if isinstance(candidate, str):
                if len(candidate) > MAX_WRAPPER_JSON_CHARS:
                    raise ValueError("encoded command ownership exceeds size limit")
                if not candidate.lstrip().startswith("{"):
                    continue
                candidate = strict_json_loads(candidate)
                if not isinstance(candidate, Mapping):
                    raise ValueError("encoded command ownership must be an object")
            if isinstance(candidate, Mapping):
                visit(candidate, depth + 1)

    visit(payload, 0)
    return tuple(sources)


def _tool_names_from_payload(
    payload: Mapping[str, Any],
    *,
    root_name_is_wrapper_owned: bool,
) -> frozenset[str]:
    names: set[str] = set()
    for index, source in enumerate(_command_sources_from_payload(payload)):
        if tool_name := _payload_tool_name(source):
            names.add(tool_name)
        if index == 0 and not root_name_is_wrapper_owned:
            continue
        nested_name = source.get("name")
        if isinstance(nested_name, str) and nested_name.strip():
            names.add(nested_name.strip())
        elif "name" in source and nested_name not in (None, ""):
            raise ValueError("invalid nested payload tool identity")
    return frozenset(names)


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
    for key in ("tool_name", "toolName"):
        candidate = payload.get(key)
        if key in payload and not isinstance(candidate, str) and candidate is not None:
            raise ValueError("invalid payload tool identity")
    names = {
        candidate.strip()
        for key in ("tool_name", "toolName")
        if isinstance((candidate := payload.get(key)), str) and candidate.strip()
    }
    tool = payload.get("tool")
    if isinstance(tool, str) and tool.strip():
        names.add(tool.strip())
    elif isinstance(tool, Mapping):
        nested_names = {
            candidate.strip()
            for key in ("tool_name", "toolName", "name", "tool")
            if isinstance((candidate := tool.get(key)), str) and candidate.strip()
        }
        if any(isinstance(tool.get(key), Mapping) for key in ("tool", "call", "request")):
            raise ValueError("nested payload tool identity is ambiguous")
        if len(nested_names) != 1:
            raise ValueError("ambiguous object-form payload tool identity")
        names.update(nested_names)
    elif "tool" in payload and tool not in (None, ""):
        raise ValueError("invalid object-form payload tool identity")
    if len(names) > 1:
        raise ValueError("ambiguous payload tool identity")
    return next(iter(names), "")


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


def _process_commandless_log_fallback_allowed(*sources: Mapping[str, Any]) -> bool:
    actions: list[str] = []
    visited: set[int] = set()
    remaining = 32
    valid = True

    def visit(source: Mapping[str, Any], depth: int) -> None:
        nonlocal remaining, valid
        if not valid or id(source) in visited:
            return
        if depth > 4 or remaining <= 0:
            valid = False
            return
        visited.add(id(source))
        remaining -= 1
        action = source.get("action")
        if action is not None:
            if not isinstance(action, str) or not action.strip():
                valid = False
                return
            actions.append(action.strip().lower())
        for key in PROCESS_COMMAND_CONTAINERS:
            nested = source.get(key)
            if isinstance(nested, Mapping):
                visit(nested, depth + 1)

    for source in sources:
        visit(source, 0)
    return valid and bool(actions) and all(action in PROCESS_LOG_ACTIONS for action in actions)


def _select_process_command(
    text: str,
    payload: Mapping[str, Any],
    *sources: Mapping[str, Any],
    exit_code: int | None = None,
) -> str:
    real_commands: list[str] = []

    def add_commands(source: Mapping[str, Any]) -> None:
        for command in _commands_from_source(source):
            if _looks_like_process_action_command(command):
                continue
            if command not in real_commands:
                real_commands.append(command)

    add_commands(payload)
    for nested in _nested_process_command_sources(payload):
        add_commands(nested)
    for source in sources:
        add_commands(source)

    command_sources = [{"command": command} for command in real_commands]
    return _select_command(text, *command_sources, exit_code=exit_code)


def _nested_process_command_sources(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    sources: list[Mapping[str, Any]] = []
    visited: set[int] = {id(payload)}
    remaining = 16

    def visit(owner: Mapping[str, Any], depth: int) -> None:
        nonlocal remaining
        if depth > 4 or remaining <= 0:
            raise ValueError("nested process command ownership exceeds limits")
        for key in PROCESS_COMMAND_CONTAINERS:
            candidate = owner.get(key)
            if not isinstance(candidate, Mapping):
                continue
            if id(candidate) in visited:
                raise ValueError("ambiguous nested process command ownership")
            remaining -= 1
            visited.add(id(candidate))
            sources.append(candidate)
            visit(candidate, depth + 1)

    visit(payload, 0)
    return tuple(sources)


def _looks_like_process_action_command(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return bool(
        len(tokens) >= 2
        and Path(tokens[0]).name.lower() == "process"
        and tokens[1].lower() in PROCESS_LOG_ACTIONS
    )


def _command_evidence_text(text: str) -> str:
    try:
        parsed = strict_json_loads(text)
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
    numeric_values: list[int] = []
    for key in ("exit", "exit_code", "returncode", "return_code"):
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            numeric_values.append(value)
    for value in numeric_values:
        if value != 0:
            return value
    status = payload.get("status")
    if (
        tool_name in TERMINAL_TOOL_NAMES | FIELD_AWARE_WRITE_TOOL_NAMES
        and isinstance(status, str)
        and status.lower() in {"failed", "failure", "error", "errored"}
    ):
        if tool_name == "process" and not numeric_values:
            return None
        return 1
    return numeric_values[0] if numeric_values else None
