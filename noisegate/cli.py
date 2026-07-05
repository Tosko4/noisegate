from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any

from ._version import __version__
from .artifacts import ArtifactError, ArtifactStore
from .engine import NoisegateOptions, reduce_text
from .plugin import transform_tool_result


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

    doctor = subparsers.add_parser("doctor", help="report package and artifact health")
    doctor.set_defaults(func=cmd_doctor)

    cat = subparsers.add_parser("cat", help="print an artifact by id")
    cat.add_argument("--artifact-dir", default=None)
    cat.add_argument("artifact_id")
    cat.set_defaults(func=cmd_cat)
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
    except Exception:
        sys.stdout.write(raw)
    return 0


def cmd_reduce_json(args: argparse.Namespace) -> int:
    raw = sys.stdin.read()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        sys.stdout.write(raw)
        return 0

    options = _options_from_args(args)
    if isinstance(parsed, dict) and isinstance(parsed.get("noisegate"), dict):
        options = options.with_mapping(parsed["noisegate"])

    try:
        output = _reduce_json_value(parsed, raw, options)
    except Exception:
        output = raw
    sys.stdout.write(output)
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    dist_version = _distribution_version()
    print("Noisegate doctor")
    print(f"package: ok ({dist_version})")
    print("plugin: ok (transform_tool_result, transform_terminal_output)")
    print(f"entrypoint: {_entrypoint_status()}")
    options = NoisegateOptions.from_env()
    artifact_dir = options.artifact_dir or ArtifactStore.from_env().root
    if options.artifact_enabled:
        try:
            ArtifactStore(artifact_dir, size_cap=options.artifact_size_cap)._ensure_root()
            print(f"artifacts: enabled ({artifact_dir})")
        except ArtifactError as exc:
            print(f"artifacts: error ({exc})")
            return 2
    else:
        print("artifacts: disabled")
        print(f"artifact_dir: {artifact_dir}")
    return 0


def cmd_cat(args: argparse.Namespace) -> int:
    try:
        store = ArtifactStore(args.artifact_dir) if args.artifact_dir else ArtifactStore.from_env()
        sys.stdout.write(store.read(args.artifact_id))
        return 0
    except Exception as exc:
        print(f"noisegate cat: {exc}", file=sys.stderr)
        return 2


def _reduce_json_value(parsed: Any, raw: str, options: NoisegateOptions) -> str:
    hook_kwargs = _options_to_hook_kwargs(options)
    if isinstance(parsed, dict) and isinstance(parsed.get("result"), str):
        tool_name = str(
            parsed.get("tool_name")
            or parsed.get("toolName")
            or parsed.get("tool")
            or ""
        )
        call_args = parsed.get("args") or parsed.get("arguments") or {}
        if not isinstance(call_args, dict):
            call_args = {}
        result_text = parsed["result"]
        transformed = transform_tool_result(
            result_text,
            tool_name=tool_name,
            args=call_args,
            **hook_kwargs,
        )
        if transformed is None and not _is_json_text(result_text):
            command = str(call_args.get("command") or parsed.get("command") or "")
            reduced = reduce_text(
                result_text,
                command=command,
                tool_name=tool_name,
                source="reduce-json",
                options=options,
            )
            transformed = reduced.text if reduced.changed else None
        if transformed is not None:
            parsed = dict(parsed)
            parsed["result"] = transformed
            return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        return raw

    tool_name = ""
    if isinstance(parsed, dict):
        tool_name = str(
            parsed.get("tool_name")
            or parsed.get("toolName")
            or parsed.get("tool")
            or ""
        )
        if not tool_name and _looks_terminal_payload(parsed):
            tool_name = "terminal"
    transformed = transform_tool_result(raw, tool_name=tool_name, **hook_kwargs)
    return transformed if transformed is not None else raw


def _looks_terminal_payload(payload: dict[Any, Any]) -> bool:
    return any(key in payload for key in ("stdout", "stderr", "output")) and any(
        key in payload for key in ("command", "exit", "exit_code", "status")
    )


def _is_json_text(value: str) -> bool:
    try:
        json.loads(value)
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


def _add_reduce_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--command", default="", help="command that produced the text")
    parser.add_argument("--tool", default="", help="Hermes tool name")
    parser.add_argument("--source", default="cli", help="source label for metadata")
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
