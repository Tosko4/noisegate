from __future__ import annotations

import argparse
import importlib.metadata
import json
import shlex
import sys
from pathlib import Path
from typing import Any

from ._version import __version__
from .artifacts import ArtifactError, ArtifactStore
from .engine import NoisegateOptions, _is_compactable_tool_name, env_diagnostics, reduce_text
from .installer import DEFAULT_PACKAGE_SPEC, InstallHermesError, install_hermes
from .plugin import transform_tool_result
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
        print("Noisegate Hermes install plan")
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
        if (
            transformed is None
            and _is_compactable_tool_name(tool_name)
            and not _is_json_text(result_text)
        ):
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
