from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shlex
import sys
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from ._version import __version__
from .artifacts import ArtifactError, ArtifactStore
from .engine import (
    _SOURCE_ALIGNMENT_WORK_BUDGET,
    NoisegateOptions,
    _is_compactable_tool_name,
    _raise_if_source_alignment_work_exhausted,
    _reduce_text_in_operation,
    _source_alignment_work_operation,
    _SourceAlignmentWorkExhausted,
    env_diagnostics,
    reduce_text,
)
from .installer import DEFAULT_PACKAGE_SPEC, InstallHermesError, install_hermes
from .json_utils import DuplicateJSONKeyError, strict_json_loads
from .plugin import (
    WRAPPER_TOOL_NAMES,
    _artifact_preview_plan,
    _artifact_preview_plan_matches_serialized_output,
    _artifact_preview_plan_notice_present_in_text,
    _artifact_preview_plan_with_owner_prefix,
    _ArtifactNoticeOwnerStep,
    _ArtifactPreviewPlan,
    _command_sources_from_payload,
    _commands_from_source,
    _extract_exit_code,
    _nested_json_text_requires_exact,
    _resolve_wrapped_call,
    _select_command,
    _store_artifact_preview_plan,
    _tool_names_from_payload,
    _transform_tool_result_in_operation,
)
from .plugin import (
    _payload_tool_name as _strict_payload_tool_name,
)
from .wrap import DEFAULT_MAX_CAPTURE_BYTES, WrappedCommandInterrupted, run_wrapped_command


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noisegate",
        description="Gate the noise. Keep the signal.",
    )
    parser.add_argument("--version", action="version", version=f"noisegate {__version__}")
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    reduce_parser = subparsers.add_parser("reduce", help="reduce stdin text")
    _add_reduce_options(reduce_parser)
    reduce_parser.set_defaults(func=cmd_reduce)

    reduce_json = subparsers.add_parser("reduce-json", help="reduce a Hermes-like JSON envelope")
    _add_reduce_options(reduce_json)
    reduce_json.set_defaults(func=cmd_reduce_json)

    wrap = subparsers.add_parser("wrap", help="run a command and print compacted output")
    _add_reduce_options(wrap, default_source="wrap")
    wrap.add_argument("--raw", action="store_true", help="print captured output without compaction")
    wrap.add_argument("--full", action="store_true", help="alias for --raw")
    wrap.add_argument(
        "--max-capture-bytes",
        type=_nonnegative_int,
        default=DEFAULT_MAX_CAPTURE_BYTES,
        help="maximum combined stdout/stderr bytes to capture before truncating",
    )
    wrap.add_argument("argv", nargs=argparse.REMAINDER, help="command after --")
    wrap.set_defaults(func=cmd_wrap)

    doctor = subparsers.add_parser("doctor", help="report package and artifact health")
    doctor.set_defaults(func=cmd_doctor)

    install_hermes_parser = subparsers.add_parser(
        "install-hermes",
        help="install and enable Noisegate in the same Python environment as Hermes",
    )
    install_hermes_parser.add_argument(
        "--hermes",
        default="hermes",
        help="Hermes executable to inspect (default: hermes on PATH)",
    )
    install_hermes_parser.add_argument(
        "--package",
        default=DEFAULT_PACKAGE_SPEC,
        help=f"package spec to install into Hermes Python (default: {DEFAULT_PACKAGE_SPEC})",
    )
    install_hermes_parser.add_argument(
        "--installer",
        choices=["uv", "pip"],
        default=None,
        help="installer backend (default: uv when available, otherwise pip)",
    )
    install_hermes_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print planned commands without executing them",
    )
    install_hermes_parser.set_defaults(func=cmd_install_hermes)

    cat = subparsers.add_parser("cat", help="print an artifact by id")
    cat.add_argument("--artifact-dir", default=None)
    cat.add_argument("artifact_id")
    cat.set_defaults(func=cmd_cat)

    artifacts = subparsers.add_parser("artifacts", help="inspect the private artifact store")
    artifact_subparsers = artifacts.add_subparsers(dest="artifact_command", required=True)

    artifacts_list = artifact_subparsers.add_parser("list", help="list stored artifacts")
    artifacts_list.add_argument("--artifact-dir", default=None)
    artifacts_list.set_defaults(func=cmd_artifacts_list)

    artifacts_stats = artifact_subparsers.add_parser("stats", help="summarize stored artifacts")
    artifacts_stats.add_argument("--artifact-dir", default=None)
    artifacts_stats.set_defaults(func=cmd_artifacts_stats)

    artifacts_verify = artifact_subparsers.add_parser("verify", help="verify stored artifacts")
    artifacts_verify.add_argument("--artifact-dir", default=None)
    artifacts_verify.set_defaults(func=cmd_artifacts_verify)
    return parser


def cmd_reduce(args: argparse.Namespace) -> int:
    raw = sys.stdin.read()
    options = _options_from_args(args)
    try:
        reduced = reduce_text(
            raw,
            command=args.command,
            tool_name=args.tool,
            source=args.source,
            options=options,
        )
        sys.stdout.write(reduced.text)
        _maybe_print_metadata(args, _debug_metadata(reduced.metadata, raw, reduced.text))
    except Exception:
        sys.stdout.write(raw)
    return 0


def cmd_reduce_json(args: argparse.Namespace) -> int:
    raw = sys.stdin.read()
    try:
        with _source_alignment_work_operation():
            return _cmd_reduce_json_with_budget(args, raw)
    except Exception:
        sys.stdout.write(raw)
        return 0


def _cmd_reduce_json_with_budget(args: argparse.Namespace, raw: str) -> int:
    try:
        parsed = strict_json_loads(raw)
    except (json.JSONDecodeError, RecursionError, ValueError):
        sys.stdout.write(raw)
        _maybe_print_metadata(
            args,
            _json_debug_metadata(raw, raw, options=None, reason="invalid_json"),
        )
        return 0

    options = _options_from_args(args)
    if isinstance(parsed, dict) and isinstance(parsed.get("noisegate"), dict):
        options = options.with_mapping(parsed["noisegate"])
    metadata: dict[str, Any] = {}
    try:
        if options.artifact_enabled:
            preview_metadata: dict[str, Any] = {}
            preview_plans: list[_ArtifactPreviewPlan] = []
            preview = _reduce_json_value(
                parsed,
                raw,
                options,
                metadata_out=preview_metadata,
                defer_artifact_store=True,
                artifact_plans_out=preview_plans,
            )
            _raise_if_source_alignment_work_exhausted()
            if preview == raw:
                output = raw
                metadata = preview_metadata
                _discard_preview_artifact_plans(metadata)
            elif not all(
                _artifact_preview_plan_matches_serialized_output(plan, preview)
                for plan in preview_plans
            ):
                output = _reduce_json_value(
                    parsed,
                    raw,
                    options.with_mapping({"artifacts": False}),
                    metadata_out=metadata,
                )
            elif len(preview_plans) == 1:
                _raise_if_source_alignment_work_exhausted()
                if _store_artifact_preview_plan(preview_plans[0], options):
                    output = preview
                    metadata = preview_metadata
                else:
                    output = raw
                    metadata = preview_metadata
                    _discard_preview_artifact_plans(
                        metadata,
                        reason="artifact_store_failed",
                    )
            elif len(preview_plans) > 1:
                output = _reduce_json_value(
                    parsed,
                    raw,
                    options.with_mapping({"artifacts": False}),
                    metadata_out=metadata,
                )
            else:
                output = preview
                metadata = preview_metadata
        else:
            output = _reduce_json_value(parsed, raw, options, metadata_out=metadata)
        _raise_if_source_alignment_work_exhausted()
    except _SourceAlignmentWorkExhausted:
        raise
    except Exception:
        output = raw
        metadata = {}
    sys.stdout.write(output)
    if metadata:
        metadata = _json_metadata_with_envelope_metrics(metadata, raw, output, options)
    _maybe_print_metadata(args, metadata or _json_debug_metadata(raw, output, options))
    return 0


def cmd_wrap(args: argparse.Namespace) -> int:
    argv = _passthrough_argv(args.argv)
    if not argv:
        print("noisegate wrap: requires a command after --", file=sys.stderr)
        return 2

    options = _options_from_args(args)
    raw = bool(args.raw or args.full)
    command = args.command or shlex.join(argv)
    try:
        result = run_wrapped_command(
            argv,
            command=command,
            source=args.source,
            max_capture_bytes=args.max_capture_bytes,
            raw=raw,
            options=options,
        )
    except FileNotFoundError:
        print(f"noisegate wrap: command not found: {argv[0]}", file=sys.stderr)
        return 127
    except PermissionError:
        print(f"noisegate wrap: permission denied: {argv[0]}", file=sys.stderr)
        return 126
    except WrappedCommandInterrupted as exc:
        return 128 + exc.signum
    except KeyboardInterrupt:
        return 130

    sys.stdout.write(result.text)
    _maybe_print_metadata(args, _debug_metadata(result.metadata, result.stdout, result.text))
    return _normalize_process_exit_code(result.exit_code)


def cmd_doctor(_args: argparse.Namespace) -> int:
    dist_version = _distribution_version()
    print("Noisegate doctor")
    print(f"package: ok ({dist_version})")
    print("plugin: ok (transform_tool_result, transform_terminal_output)")
    print(f"entrypoint: {_entrypoint_status()}")
    diagnostics = env_diagnostics()
    if diagnostics:
        print("environment: warnings")
        for diagnostic in diagnostics:
            print(f"- {diagnostic}")
    else:
        print("environment: ok")
    options = NoisegateOptions.from_env()
    print(
        "config: "
        f"enabled={options.enabled} "
        f"mode={options.mode} "
        f"max_chars={options.max_chars} "
        f"max_lines={options.max_lines} "
        f"head_lines={options.head_lines} "
        f"tail_lines={options.tail_lines}"
    )
    artifact_dir = options.artifact_dir or ArtifactStore.from_env().root
    if options.artifact_enabled:
        try:
            ArtifactStore(artifact_dir, size_cap=options.artifact_size_cap)._ensure_root()
            print(f"artifacts: enabled ({artifact_dir})")
            print(f"artifact_size_cap: {options.artifact_size_cap}")
        except ArtifactError as exc:
            print(f"artifacts: error ({exc})")
            return 2
    else:
        print("artifacts: disabled (set NOISEGATE_ARTIFACTS=1 to enable)")
        print(f"artifact_dir: {artifact_dir}")
        print(f"artifact_size_cap: {options.artifact_size_cap}")
    return 0


def cmd_install_hermes(args: argparse.Namespace) -> int:
    try:
        plan = install_hermes(
            hermes=args.hermes,
            package_spec=args.package,
            installer=args.installer,
            dry_run=args.dry_run,
        )
    except InstallHermesError as exc:
        print(f"noisegate install-hermes: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(
            "Noisegate Hermes install plan "
            "(dry run; install/enable/doctor commands will not run)"
        )
        for line in plan.as_lines():
            print(line)
    else:
        print("Noisegate installed and enabled for Hermes")
        print(f"hermes: {plan.hermes_executable}")
        print(f"hermes_python: {plan.hermes_python}")
    return 0


def cmd_cat(args: argparse.Namespace) -> int:
    try:
        store = ArtifactStore(args.artifact_dir) if args.artifact_dir else ArtifactStore.from_env()
        sys.stdout.write(store.read(args.artifact_id))
        return 0
    except Exception as exc:
        print(f"noisegate cat: {exc}", file=sys.stderr)
        return 2


def cmd_artifacts_list(args: argparse.Namespace) -> int:
    try:
        store = _artifact_store_from_args(args)
        for artifact in store.list():
            print(
                f"{artifact.artifact_id} "
                f"size_bytes={artifact.size_bytes} "
                f"sha256={artifact.sha256} "
                f"modified_at={artifact.modified_at}"
            )
        return 0
    except Exception as exc:
        print(f"noisegate artifacts list: {exc}", file=sys.stderr)
        return 2


def cmd_artifacts_stats(args: argparse.Namespace) -> int:
    try:
        store = _artifact_store_from_args(args)
        stats = store.stats()
        print(f"artifacts: {stats['artifacts']}")
        print(f"total_size_bytes: {stats['total_size_bytes']}")
        return 0
    except Exception as exc:
        print(f"noisegate artifacts stats: {exc}", file=sys.stderr)
        return 2


def cmd_artifacts_verify(args: argparse.Namespace) -> int:
    try:
        store = _artifact_store_from_args(args)
        checks = store.verify()
    except Exception as exc:
        print(f"noisegate artifacts verify: {exc}", file=sys.stderr)
        return 2

    failed = [check for check in checks if not check.ok]
    if not checks:
        print("artifacts: none")
        return 0
    for check in checks:
        status = "ok" if check.ok else "error"
        print(f"{check.artifact_id} {status} reason={check.reason}")
    return 2 if failed else 0


def _discard_preview_artifact_plans(
    value: Any,
    *,
    reason: str = "outer_no_gain",
) -> None:
    if isinstance(value, dict):
        artifact = value.get("artifact")
        if isinstance(artifact, dict) and artifact.get("stored") is True:
            replacement: dict[str, Any] = {
                "stored": False,
                "reason": reason,
            }
            size_bytes = artifact.get("size_bytes")
            if isinstance(size_bytes, int) and not isinstance(size_bytes, bool):
                replacement["size_bytes"] = size_bytes
            value["artifact"] = replacement
        for child in value.values():
            _discard_preview_artifact_plans(child, reason=reason)
    elif isinstance(value, list):
        for child in value:
            _discard_preview_artifact_plans(child, reason=reason)


def _reduce_json_value(
    parsed: Any,
    raw: str,
    options: NoisegateOptions,
    *,
    metadata_out: dict[str, Any] | None = None,
    defer_artifact_store: bool = False,
    artifact_plans_out: list[_ArtifactPreviewPlan] | None = None,
) -> str:
    local_metadata: dict[str, Any] | None = {} if metadata_out is not None else None
    local_plans: list[_ArtifactPreviewPlan] | None = (
        [] if artifact_plans_out is not None else None
    )

    def reduce_value() -> str:
        output = _reduce_json_value_with_budget(
            parsed,
            raw,
            options,
            metadata_out=local_metadata,
            defer_artifact_store=defer_artifact_store,
            artifact_plans_out=local_plans,
        )
        _raise_if_source_alignment_work_exhausted()
        if metadata_out is not None and local_metadata:
            metadata_out.update(local_metadata)
        if artifact_plans_out is not None and local_plans:
            artifact_plans_out.extend(local_plans)
        return output

    if _SOURCE_ALIGNMENT_WORK_BUDGET.get() is not None:
        return reduce_value()
    try:
        with _source_alignment_work_operation():
            return reduce_value()
    except Exception:
        return raw


def _reduce_json_value_with_budget(
    parsed: Any,
    raw: str,
    options: NoisegateOptions,
    *,
    metadata_out: dict[str, Any] | None = None,
    defer_artifact_store: bool = False,
    artifact_plans_out: list[_ArtifactPreviewPlan] | None = None,
) -> str:
    hook_kwargs = _options_to_hook_kwargs(options)

    def tool_transform(
        result: str,
        *,
        artifact_plans_out: list[_ArtifactPreviewPlan] | None = None,
        **kwargs: Any,
    ) -> str | None:
        return _transform_tool_result_in_operation(
            result,
            defer_artifact_store=defer_artifact_store,
            artifact_plans_out=artifact_plans_out,
            **kwargs,
        )

    call_args: dict[str, Any] = _envelope_call_args(parsed) if isinstance(parsed, dict) else {}
    tool_name = ""
    resolved_wrapper_identity = False
    if isinstance(parsed, dict):
        explicit_tool_name = _envelope_tool_name(parsed)
        wrapper_alias = parsed.get("name")
        if isinstance(wrapper_alias, str) and wrapper_alias.strip():
            wrapper_alias = wrapper_alias.strip()
            if explicit_tool_name and wrapper_alias != explicit_tool_name:
                return raw
            explicit_tool_name = wrapper_alias
        if explicit_tool_name in WRAPPER_TOOL_NAMES:
            resolved_wrapper = _resolve_wrapped_call(parsed)
            if resolved_wrapper is None:
                return raw
            resolved_wrapper_identity = True
            tool_name = resolved_wrapper.tool_name
            call_args = dict(resolved_wrapper.call_args)
            wrapper_tool_names = _tool_names_from_payload(call_args)
            if len(wrapper_tool_names) > 1 or (
                wrapper_tool_names and tool_name not in wrapper_tool_names
            ):
                return raw
            wrapper_commands = {
                command
                for source in _command_sources_from_payload(call_args)
                for command in _commands_from_source(source)
            }
            if len(wrapper_commands) > 1:
                return raw
            if wrapper_commands:
                call_args["command"] = next(iter(wrapper_commands))
        else:
            tool_name = explicit_tool_name or _payload_tool_name(parsed, call_args)
        if tool_name and not _is_compactable_tool_name(tool_name):
            return raw
    if isinstance(parsed, dict) and "result" in parsed:
        result_value = parsed["result"]
        json_encoded_result = False
        if isinstance(result_value, str):
            try:
                json_encoded_result = _nested_json_text_requires_exact(result_value)
            except (DuplicateJSONKeyError, json.JSONDecodeError, ValueError, RecursionError):
                return raw
        nested_tool_name = _embedded_result_tool_name(result_value)
        preserve_wrapped_json_result = resolved_wrapper_identity and json_encoded_result
        if (
            nested_tool_name
            and tool_name
            and nested_tool_name != tool_name
            and not preserve_wrapped_json_result
        ):
            return raw
        nested_transform_tool_name = tool_name
        transformed: str | None = None
        injected_exit_keys: tuple[str, ...] = ()
        replace_with_json_value = False
        result_plans: list[_ArtifactPreviewPlan] = []
        local_result_plans: list[_ArtifactPreviewPlan] = []
        result_metadata: dict[str, Any] = {}

        if isinstance(result_value, str) and not preserve_wrapped_json_result:
            result_input, injected_exit_keys = _result_transform_input(result_value, parsed)
            result_call_args = _merge_wrapper_and_result_call_args(
                call_args,
                _result_call_args(result_value),
            )
            if result_call_args is None:
                return raw
            result_exit_code = _result_exit_code(result_value, parsed, tool_name)
            selected_command = _select_command(
                result_value,
                result_call_args,
                *_result_command_sources(result_value, parsed),
                exit_code=result_exit_code,
            )
            if selected_command:
                result_call_args = {**result_call_args, "command": selected_command}
            transformed = tool_transform(
                result_input,
                tool_name=nested_transform_tool_name,
                args=result_call_args,
                noisegate_exit_code=result_exit_code,
                artifact_plans_out=local_result_plans,
                **cast(Any, hook_kwargs),
            )
            if transformed is not None and injected_exit_keys:
                transformed = _remove_injected_exit_hints_from_json_text(
                    transformed,
                    injected_exit_keys,
                )
            if transformed is not None:
                result_owner = (
                    _ArtifactNoticeOwnerStep("result", parse_json=True),
                )
                result_plans.extend(
                    _artifact_preview_plan_with_owner_prefix(plan, result_owner)
                    for plan in local_result_plans
                )
            if (
                transformed is None
                and _is_compactable_tool_name(tool_name)
                and not _is_json_text(result_value)
            ):
                command = selected_command or _envelope_command(call_args)
                reduced = _reduce_text_in_operation(
                    result_value,
                    command=command,
                    tool_name=tool_name,
                    source="reduce-json",
                    exit_code=result_exit_code,
                    options=options,
                    defer_artifact_store=defer_artifact_store,
                )
                result_metadata.update(
                    _debug_metadata(reduced.metadata, result_value, reduced.text)
                )
                if reduced.changed:
                    transformed = reduced.text
                    if defer_artifact_store:
                        artifact = reduced.metadata.get("artifact")
                        if isinstance(artifact, dict) and artifact.get("stored") is True:
                            plan = _artifact_preview_plan(
                                result_value,
                                reduced.metadata,
                                artifact_dir=options.artifact_dir,
                                owner_path=(_ArtifactNoticeOwnerStep("result"),),
                            )
                            if plan is None or not _artifact_preview_plan_notice_present_in_text(
                                plan,
                                reduced.text,
                            ):
                                inline = _reduce_text_in_operation(
                                    result_value,
                                    command=command,
                                    tool_name=tool_name,
                                    source="reduce-json",
                                    exit_code=result_exit_code,
                                    options=options.with_mapping({"artifacts": False}),
                                    defer_artifact_store=defer_artifact_store,
                                )
                                result_metadata.clear()
                                result_metadata.update(
                                    _debug_metadata(
                                        inline.metadata,
                                        result_value,
                                        inline.text,
                                    )
                                )
                                transformed = inline.text if inline.changed else None
                            else:
                                result_plans.append(plan)
        elif isinstance(result_value, dict):
            nested_input, injected_exit_keys = _result_transform_input(result_value, parsed)
            result_call_args = _merge_wrapper_and_result_call_args(
                call_args,
                _result_call_args(result_value),
            )
            if result_call_args is None:
                return raw
            result_exit_code = _result_exit_code(result_value, parsed, tool_name)
            selected_command = _select_command(
                nested_input,
                result_call_args,
                *_result_command_sources(result_value, parsed),
                exit_code=result_exit_code,
            )
            if selected_command:
                result_call_args = {**result_call_args, "command": selected_command}
            transformed = tool_transform(
                nested_input,
                tool_name=nested_transform_tool_name,
                args=result_call_args,
                noisegate_exit_code=result_exit_code,
                artifact_plans_out=local_result_plans,
                **cast(Any, hook_kwargs),
            )
            replace_with_json_value = True
            if transformed is not None:
                result_owner = (_ArtifactNoticeOwnerStep("result"),)
                result_plans.extend(
                    _artifact_preview_plan_with_owner_prefix(plan, result_owner)
                    for plan in local_result_plans
                )

        if transformed is not None:
            parsed = dict(parsed)
            if replace_with_json_value:
                try:
                    transformed_value = strict_json_loads(transformed)
                except json.JSONDecodeError:
                    return raw
                if isinstance(transformed_value, dict):
                    _remove_injected_exit_hints(transformed_value, injected_exit_keys)
                parsed["result"] = transformed_value
            else:
                parsed["result"] = transformed
            candidate = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
            if _has_direct_text_payload(parsed):
                direct_plans: list[_ArtifactPreviewPlan] = []
                direct_transformed = _transform_direct_payload_preserving_json_result(
                    parsed,
                    tool_name=tool_name,
                    call_args=call_args,
                    normalize_wrapper_identity=resolved_wrapper_identity,
                    hook_kwargs=hook_kwargs,
                    tool_transform=tool_transform,
                    artifact_plans_out=direct_plans,
                )
                if direct_transformed is not None and len(direct_transformed) < len(raw):
                    accepted_plans = [*result_plans, *direct_plans]
                    if not all(
                        _artifact_preview_plan_matches_serialized_output(
                            plan,
                            direct_transformed,
                        )
                        for plan in accepted_plans
                    ):
                        return raw
                    if artifact_plans_out is not None:
                        artifact_plans_out.extend(accepted_plans)
                    return direct_transformed
            if len(candidate) >= len(raw):
                if metadata_out is not None:
                    metadata_out.update(result_metadata)
                return raw
            if not all(
                _artifact_preview_plan_matches_serialized_output(plan, candidate)
                for plan in result_plans
            ):
                return raw
            if artifact_plans_out is not None:
                artifact_plans_out.extend(result_plans)
            if metadata_out is not None:
                metadata_out.update(result_metadata)
            return candidate
        if not _has_direct_text_payload(parsed):
            if metadata_out is not None:
                metadata_out.update(result_metadata)
            return raw

    direct_plans: list[_ArtifactPreviewPlan] = []
    if isinstance(parsed, dict):
        transformed = _transform_direct_payload_preserving_json_result(
            parsed,
            tool_name=tool_name,
            call_args=call_args,
            normalize_wrapper_identity=resolved_wrapper_identity,
            hook_kwargs=hook_kwargs,
            tool_transform=tool_transform,
            artifact_plans_out=direct_plans,
        )
    else:
        transformed = tool_transform(
            raw,
            tool_name=tool_name,
            args=call_args,
            artifact_plans_out=direct_plans,
            **cast(Any, hook_kwargs),
        )
    if transformed is None or len(transformed) >= len(raw):
        return raw
    if not all(
        _artifact_preview_plan_matches_serialized_output(plan, transformed)
        for plan in direct_plans
    ):
        return raw
    if artifact_plans_out is not None:
        artifact_plans_out.extend(direct_plans)
    return transformed


def _transform_direct_payload_preserving_json_result(
    payload: dict[str, Any],
    *,
    tool_name: str,
    call_args: dict[str, Any],
    normalize_wrapper_identity: bool,
    hook_kwargs: dict[str, object],
    tool_transform: Callable[..., str | None],
    artifact_plans_out: list[_ArtifactPreviewPlan] | None = None,
) -> str | None:
    direct_payload = dict(payload)
    identity_fields: dict[str, Any] = {}
    if normalize_wrapper_identity:
        identity_keys = ("tool_name", "toolName", "tool", "name")
        identity_fields = {
            key: direct_payload[key]
            for key in identity_keys
            if key in direct_payload
        }
        for key in identity_keys:
            direct_payload.pop(key, None)
        direct_payload["tool_name"] = tool_name

    def restore_wrapper_identity(transformed_payload: dict[str, Any]) -> None:
        if not normalize_wrapper_identity:
            return
        for key in ("tool_name", "toolName", "tool", "name"):
            transformed_payload.pop(key, None)
        transformed_payload.update(identity_fields)

    result_value = payload.get("result")
    if isinstance(result_value, str) and _is_json_text(result_value):
        direct_payload.pop("result", None)
        if not _has_direct_text_payload(direct_payload):
            return None
        direct_raw = json.dumps(direct_payload, ensure_ascii=False, separators=(",", ":"))
        transformed = tool_transform(
            direct_raw,
            tool_name=tool_name,
            args=call_args,
            artifact_plans_out=artifact_plans_out,
            **cast(Any, hook_kwargs),
        )
        if transformed is None:
            return None
        try:
            transformed_payload = strict_json_loads(transformed)
        except json.JSONDecodeError:
            return None
        if not isinstance(transformed_payload, dict):
            return None
        transformed_payload["result"] = result_value
        restore_wrapper_identity(transformed_payload)
        return json.dumps(transformed_payload, ensure_ascii=False, separators=(",", ":"))

    direct_raw = json.dumps(direct_payload, ensure_ascii=False, separators=(",", ":"))
    transformed = tool_transform(
        direct_raw,
        tool_name=tool_name,
        args=call_args,
        artifact_plans_out=artifact_plans_out,
        **cast(Any, hook_kwargs),
    )
    if transformed is None or not normalize_wrapper_identity:
        return transformed
    try:
        transformed_payload = strict_json_loads(transformed)
    except json.JSONDecodeError:
        return None
    if not isinstance(transformed_payload, dict):
        return None
    restore_wrapper_identity(transformed_payload)
    return json.dumps(transformed_payload, ensure_ascii=False, separators=(",", ":"))


def _envelope_tool_name(payload: dict[Any, Any]) -> str:
    return _strict_payload_tool_name(payload)


def _payload_tool_name(
    payload: dict[Any, Any],
    call_args: dict[str, Any] | None = None,
) -> str:
    tool_name = _envelope_tool_name(payload)
    if not tool_name and (
        _looks_terminal_payload(payload, call_args)
        or _looks_terminal_result_payload(payload, call_args)
    ):
        return "terminal"
    return tool_name


_EXIT_HINT_KEYS = ("exit", "exit_code", "returncode", "return_code", "status")


def _envelope_exit_hint_keys(envelope: dict[Any, Any]) -> tuple[str, ...]:
    numeric_keys = []
    for key in ("exit", "exit_code", "returncode", "return_code"):
        value = envelope.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            numeric_keys.append(key)
    nonzero_numeric_keys = tuple(key for key in numeric_keys if envelope.get(key) != 0)
    if nonzero_numeric_keys:
        return nonzero_numeric_keys
    status = envelope.get("status")
    if (
        isinstance(status, str)
        and status.lower() in {"failed", "failure", "error", "errored"}
    ):
        return ("status",)
    return tuple(numeric_keys)


def _result_transform_input(
    result_value: Any,
    envelope: dict[Any, Any],
) -> tuple[str, tuple[str, ...]]:
    if isinstance(result_value, dict):
        payload = dict(result_value)
    elif isinstance(result_value, str):
        try:
            nested = strict_json_loads(result_value)
        except json.JSONDecodeError:
            return result_value, ()
        if not isinstance(nested, dict):
            return result_value, ()
        payload = dict(nested)
    else:
        return json.dumps(result_value, ensure_ascii=False, separators=(",", ":")), ()

    if any(key in payload for key in _EXIT_HINT_KEYS):
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")), ()

    injected_keys = []
    for key in _envelope_exit_hint_keys(envelope):
        payload[key] = envelope[key]
        injected_keys.append(key)
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        tuple(injected_keys),
    )


def _remove_injected_exit_hints_from_json_text(text: str, injected_keys: tuple[str, ...]) -> str:
    if not injected_keys:
        return text
    try:
        payload = strict_json_loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(payload, dict):
        return text
    _remove_injected_exit_hints(payload, injected_keys)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _remove_injected_exit_hints(payload: dict[Any, Any], injected_keys: tuple[str, ...]) -> None:
    for key in injected_keys:
        payload.pop(key, None)


def _embedded_result_tool_name(result_value: Any) -> str:
    nested = _result_mapping(result_value)
    if not isinstance(nested, dict):
        return ""
    names = _tool_names_from_payload(nested)
    if len(names) > 1:
        raise ValueError("ambiguous embedded result tool identity")
    return next(iter(names), "")


def _result_call_args(result_value: Any) -> dict[str, Any]:
    nested = _result_mapping(result_value)
    if not isinstance(nested, dict):
        return {}
    commands = {
        command
        for source in _command_sources_from_payload(nested)
        for command in _commands_from_source(source)
    }
    if len(commands) > 1:
        raise ValueError("conflicting result command ownership")
    return {"command": next(iter(commands))} if commands else {}


def _merge_wrapper_and_result_call_args(
    wrapper_args: dict[str, Any],
    result_args: dict[str, Any],
) -> dict[str, Any] | None:
    commands = {
        command
        for source in (wrapper_args, result_args)
        for command in _commands_from_source(source)
    }
    if len(commands) > 1:
        return None
    merged = dict(result_args)
    merged.update(wrapper_args)
    return merged


def _result_mapping(result_value: Any) -> dict[Any, Any] | None:
    if isinstance(result_value, dict):
        return result_value
    if isinstance(result_value, str):
        try:
            nested = strict_json_loads(result_value)
        except json.JSONDecodeError:
            return None
        if isinstance(nested, dict):
            return nested
    return None


def _result_command_sources(
    result_value: Any,
    envelope: dict[Any, Any],
) -> tuple[Mapping[str, Any], ...]:
    sources: list[Mapping[str, Any]] = []
    nested = _result_mapping(result_value)
    for payload in (nested, envelope):
        if isinstance(payload, dict):
            sources.extend(_command_sources_from_payload(payload))
    return tuple(sources)


def _result_exit_code(
    result_value: Any,
    envelope: dict[Any, Any],
    tool_name: str,
) -> int | None:
    nested = _result_mapping(result_value)
    if nested is not None:
        nested_tool_name = _envelope_tool_name(nested) or tool_name
        exit_code = _preferred_exit_code(nested, nested_tool_name)
        if exit_code is not None:
            return exit_code
    return _preferred_exit_code(envelope, tool_name)


def _preferred_exit_code(payload: dict[Any, Any], tool_name: str) -> int | None:
    keys = _envelope_exit_hint_keys(payload)
    if not keys:
        return None
    key = keys[0]
    if key == "status":
        return 1
    value = payload.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return _extract_exit_code(payload, tool_name)


def _looks_terminal_result_payload(
    payload: dict[Any, Any],
    call_args: dict[str, Any] | None = None,
) -> bool:
    result = payload.get("result")
    has_command = _has_command_hint(payload) or _has_command_hint(call_args or {})
    if isinstance(result, dict):
        embedded_tool_name = _envelope_tool_name(result)
        if embedded_tool_name and not _is_compactable_tool_name(embedded_tool_name):
            return False
        return _has_direct_text_payload(result) and (
            has_command
            or _has_exit_hint(payload)
            or _has_command_hint(result)
            or _has_exit_hint(result)
        )
    if isinstance(result, str):
        if (has_command or _has_exit_hint(payload)) and not _is_json_text(result):
            return True
        try:
            nested = strict_json_loads(result)
        except json.JSONDecodeError:
            return False
        return isinstance(nested, dict) and _has_direct_text_payload(nested) and (
            not (
                (embedded_tool_name := _envelope_tool_name(nested))
                and not _is_compactable_tool_name(embedded_tool_name)
            )
        ) and (
            has_command or _has_exit_hint(payload) or _has_exit_hint(nested)
        )
    return False


def _has_command_hint(payload: dict[Any, Any]) -> bool:
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


def _has_exit_hint(payload: dict[Any, Any]) -> bool:
    for key in ("exit", "exit_code", "returncode", "return_code"):
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return True
    return False


def _envelope_call_args(payload: dict[Any, Any]) -> dict[str, Any]:
    call_args: dict[str, Any] = {}
    for key in ("args", "arguments"):
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            _merge_call_arg_hints(call_args, candidate)
    _merge_call_arg_hints(call_args, payload)
    return call_args


def _merge_call_arg_hints(target: dict[str, Any], source: dict[Any, Any]) -> None:
    if _has_command_hint(target):
        return
    for key in ("command", "cmd", "shell_command", "code"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            target[key] = value
    argv = source.get("argv")
    if _usable_argv(argv):
        target["argv"] = argv


def _envelope_command(values: dict[str, Any]) -> str:
    for key in ("command", "cmd", "shell_command", "code"):
        value = values.get(key)
        if isinstance(value, str) and value.strip():
            return value
    argv = values.get("argv")
    if _usable_argv(argv):
        return shlex.join(cast(list[str], argv))
    return ""


def _has_direct_text_payload(payload: dict[Any, Any]) -> bool:
    return any(
        isinstance(payload.get(key), str)
        for key in ("stdout", "stderr", "output", "logs", "text", "content", "message")
    )


def _looks_terminal_payload(
    payload: dict[Any, Any],
    call_args: dict[str, Any] | None = None,
) -> bool:
    return any(key in payload for key in ("stdout", "stderr", "output")) and (
        _has_command_hint(payload)
        or _has_command_hint(call_args or {})
        or _has_exit_hint(payload)
    )


def _is_json_text(value: str) -> bool:
    try:
        strict_json_loads(value)
    except json.JSONDecodeError:
        return False
    return True


def _options_from_args(args: argparse.Namespace) -> NoisegateOptions:
    options = NoisegateOptions.from_env()
    updates: dict[str, object] = {}
    for key in ("max_chars", "max_lines", "head_lines", "tail_lines", "mode"):
        value = getattr(args, key, None)
        if value is not None:
            updates[key] = value
    if getattr(args, "store_artifact", False):
        updates["artifact_enabled"] = True
    if getattr(args, "artifact_dir", None):
        updates["artifact_dir"] = Path(args.artifact_dir)
    if getattr(args, "artifact_size_cap", None) is not None:
        updates["artifact_size_cap"] = args.artifact_size_cap
    return options.with_mapping(updates)


def _options_to_hook_kwargs(options: NoisegateOptions) -> dict[str, object]:
    values: dict[str, object] = {
        "noisegate_enabled": options.enabled,
        "noisegate_mode": options.mode,
        "noisegate_max_chars": options.max_chars,
        "noisegate_max_lines": options.max_lines,
        "noisegate_head_lines": options.head_lines,
        "noisegate_tail_lines": options.tail_lines,
        "noisegate_preserve_diffs": options.preserve_diffs,
        "noisegate_artifacts": options.artifact_enabled,
        "noisegate_artifact_size_cap": options.artifact_size_cap,
    }
    if options.artifact_dir is not None:
        values["noisegate_artifact_dir"] = str(options.artifact_dir)
    return values


def _maybe_print_metadata(args: argparse.Namespace, metadata: dict[str, Any]) -> None:
    if not getattr(args, "metadata", False):
        return
    try:
        sys.stderr.write(json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n")
        sys.stderr.flush()
    except OSError:
        # Diagnostics must never corrupt stdout or change the wrapped command's
        # exit behavior. If stderr is closed/full, keep the data path intact and
        # replace stderr so interpreter shutdown does not retry a failed flush.
        with suppress(OSError):
            sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
        return


def _debug_metadata(metadata: dict[str, Any], original: str, output: str) -> dict[str, Any]:
    original_lines = _line_count(original)
    output_lines = _line_count(output)
    return {
        **metadata,
        "output_chars": len(output),
        "output_lines": output_lines,
        "saved_chars": max(0, len(original) - len(output)),
        "saved_lines": max(0, original_lines - output_lines),
    }


def _json_debug_metadata(
    raw: str,
    output: str,
    options: NoisegateOptions | None,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    compacted = output != raw
    original_lines = _line_count(raw)
    output_lines = _line_count(output)
    mode = "unknown" if options is None else options.mode if options.enabled else "passthrough"
    saved_chars = max(0, len(raw) - len(output))
    saved_lines = max(0, original_lines - output_lines)
    return {
        "version": __version__,
        "compacted": compacted,
        "mode": mode,
        "source": "reduce-json",
        "reason": reason or ("changed" if compacted else "unchanged"),
        "original_chars": len(raw),
        "output_chars": len(output),
        "saved_chars": saved_chars,
        "omitted_chars": saved_chars,
        "original_lines": original_lines,
        "output_lines": output_lines,
        "saved_lines": saved_lines,
        "omitted_lines": saved_lines,
    }


def _json_metadata_with_envelope_metrics(
    metadata: dict[str, Any],
    raw: str,
    output: str,
    options: NoisegateOptions,
) -> dict[str, Any]:
    combined = dict(metadata)
    for key in (
        "original_chars",
        "output_chars",
        "saved_chars",
        "omitted_chars",
        "original_lines",
        "output_lines",
        "saved_lines",
        "omitted_lines",
    ):
        if key in metadata:
            combined[f"field_{key}"] = metadata[key]
    reason = str(metadata["reason"]) if "reason" in metadata else None
    combined.update(_json_debug_metadata(raw, output, options, reason=reason))
    return combined


def _line_count(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines())


def _artifact_store_from_args(args: argparse.Namespace) -> ArtifactStore:
    return ArtifactStore(args.artifact_dir) if args.artifact_dir else ArtifactStore.from_env()


def _add_reduce_options(parser: argparse.ArgumentParser, *, default_source: str = "cli") -> None:
    parser.add_argument("--command", default="", help="command that produced the text")
    parser.add_argument("--tool", default="", help="Hermes tool name")
    parser.add_argument("--source", default=default_source, help="source label for metadata")
    parser.add_argument("--mode", choices=["auto", "head_tail", "off"], default=None)
    parser.add_argument("--max-chars", type=int, default=None)
    parser.add_argument("--max-lines", type=int, default=None)
    parser.add_argument("--head-lines", type=int, default=None)
    parser.add_argument("--tail-lines", type=int, default=None)
    parser.add_argument(
        "--store-artifact",
        action="store_true",
        help="store original text privately",
    )
    parser.add_argument("--artifact-dir", default=None)
    parser.add_argument("--artifact-size-cap", type=int, default=None)
    parser.add_argument(
        "--metadata",
        "--debug",
        action="store_true",
        help="print Noisegate diagnostic metadata as JSON to stderr",
    )


def _passthrough_argv(argv: list[str]) -> list[str]:
    if argv and argv[0] == "--":
        return argv[1:]
    return argv


def _normalize_process_exit_code(exit_code: int) -> int:
    if exit_code < 0:
        return 128 + abs(exit_code)
    return exit_code


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _distribution_version() -> str:
    try:
        return importlib.metadata.version("noisegate-hermes")
    except importlib.metadata.PackageNotFoundError:
        return __version__


def _entrypoint_status() -> str:
    try:
        eps = importlib.metadata.entry_points().select(group="hermes_agent.plugins")
    except Exception:
        return "unknown"
    for ep in eps:
        if ep.name == "noisegate" and ep.value == "noisegate":
            return "ok (noisegate)"
    return "not installed"


if __name__ == "__main__":
    raise SystemExit(main())
