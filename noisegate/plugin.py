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
    _source_alignment_work_operation,
    _SourceAlignmentWorkExhausted,
    _store_artifact,
    classify_command,
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
            if len(candidate) >= len(result):
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
        if options.artifact_enabled and sum(
            isinstance(payload.get(field), str) and bool(payload.get(field)) for field in fields
        ) > 1:
            options = replace(options, artifact_enabled=False)
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
        preview_plans_by_field: dict[str, _ArtifactPreviewPlan] = {}
        preserve_patterns_by_field: dict[str, tuple[re.Pattern[str], ...] | None] = {}
        reduce_options = replace(options, artifact_enabled=False)

        for field in fields:
            value = payload.get(field)
            if not isinstance(value, str):
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
        candidate = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(candidate) >= len(result):
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
            candidate = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
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
        owner: Any = json.loads(output)
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
                owner = json.loads(owner)
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
        tool_name in TERMINAL_TOOL_NAMES
        and isinstance(status, str)
        and status.lower() in {"failed", "failure", "error", "errored"}
    ):
        return 1
    return numeric_values[0] if numeric_values else None
