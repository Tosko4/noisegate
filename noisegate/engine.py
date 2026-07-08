from __future__ import annotations

import hashlib
import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from ._version import __version__
from .artifacts import DEFAULT_SIZE_CAP, ArtifactError, ArtifactStore, ArtifactTooLarge

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
MIN_HEAD_TAIL_CHARS = 2
BYPASS_MARKERS = (
    "NOISEGATE_BYPASS",
    "NOISEGATE_RAW",
    "[noisegate:bypass]",
    "[noisegate:raw]",
    "[noisegate bypass]",
)
PROTECTED_TOOL_NAMES = frozenset(
    {
        "memory",
        "read_file",
        "read_files",
        "read_text_file",
        "session_search",
        "skill_manage",
        "skill_view",
        "web_extract",
        "web_search",
        "write_file",
        "patch",
        "apply_patch",
        "edit_file",
        "replace_in_file",
    }
)
PROTECTED_TOOL_PREFIXES = ("hindsight_", "lcm_", "mcp_", "mcp__")
COMPACTABLE_TOOL_NAMES = frozenset({"terminal", "process", "read_terminal", "browser_console"})
LCM_EXTERNALIZED_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\[(?:Externalized|GC'd externalized) (?:tool output|payload):"
        r"[^\]\n]*\bref=[^;\]\s]+[^\]\n]*\]",
        r"\[Externalized LCM ingest payload:[^\]\n]*\bref=[^;\]\s]+[^\]\n]*\]",
        r'"externalized_ref"\s*:\s*"[^"\n]+"',
        r"\bexternalized_ref\b\s*[:=]\s*[^,\s}\]]+",
    )
)
SHELL_SEPARATORS = {"|", "||", "&&", ";", "&"}
SOURCE_SEARCH_COMMANDS = {"rg", "grep", "ag", "ack"}
OPTIONS_WITH_VALUES = {
    "-C",
    "-H",
    "-I",
    "-h",
    "-i",
    "-n",
    "-P",
    "-c",
    "-f",
    "-o",
    "-p",
    "-t",
    "-w",
    "--ansi",
    "--builder",
    "--cache-dir",
    "--call",
    "--cert",
    "--config",
    "--config-file",
    "--constraint",
    "--context",
    "--directory",
    "--dir",
    "--editable",
    "--env-file",
    "--extra-index-url",
    "--file",
    "--find-links",
    "--from",
    "--host",
    "--index-url",
    "--log-level",
    "--max-args",
    "--max-procs",
    "--option",
    "--package",
    "--parallel",
    "--profile",
    "--progress",

    "--prefix",
    "--project-directory",
    "--project-name",
    "--proxy",
    "--project",
    "--python",
    "--replace",
    "--requirement",
    "--spec",
    "--trusted-host",
    "--tlscacert",
    "--tlscert",
    "--tlskey",
    "--target-release",
    "--timeout",
    "--retries",
    "--with",
    "--with-editable",
    "--with-requirements",
    "--workspace",
    "--filter",
}
SOURCE_READ_COMMANDS = {
    "bat",
    "cat",
    "head",
    "jq",
    "less",
    "more",
    "nl",
    "sed",
    "tail",
    "yq",
}


@dataclass(frozen=True, slots=True)
class NoisegateOptions:
    enabled: bool = True
    mode: str = "auto"
    max_chars: int = 4_000
    max_lines: int = 160
    head_lines: int = 24
    tail_lines: int = 16
    important_context_lines: int = 2
    max_important_lines: int = 80
    preserve_diffs: bool = True
    artifact_enabled: bool = False
    artifact_dir: Path | None = None
    artifact_size_cap: int = DEFAULT_SIZE_CAP

    @classmethod
    def from_env(cls, **overrides: object) -> NoisegateOptions:
        enabled = True
        mode = "auto"
        env_mode = os.environ.get("NOISEGATE")
        if env_mode is not None and env_mode.strip().lower() in FALSE_VALUES | {"off"}:
            enabled = False
        if _env_flag("NOISEGATE_DISABLE", default=False):
            enabled = False
        if _env_flag("NOISEGATE_BYPASS", default=False) or _env_flag(
            "NOISEGATE_RAW",
            default=False,
        ):
            enabled = False
            mode = "off"

        artifact_dir = os.environ.get("NOISEGATE_ARTIFACT_DIR")
        options = cls(
            enabled=enabled,
            mode=mode,
            artifact_enabled=_env_flag("NOISEGATE_ARTIFACTS", default=False),
            artifact_dir=Path(artifact_dir).expanduser() if artifact_dir else None,
            artifact_size_cap=_parse_nonnegative_int(
                os.environ.get("NOISEGATE_ARTIFACT_SIZE_CAP"),
                DEFAULT_SIZE_CAP,
            ),
        )
        return options.with_mapping(overrides)

    def with_mapping(self, values: Mapping[str, object] | None) -> NoisegateOptions:
        if not values:
            return self
        updates: dict[str, object] = {}
        for raw_key, value in values.items():
            key = _option_key(str(raw_key))
            if key == "enabled":
                parsed = _parse_bool(value)
                if parsed is not None:
                    updates["enabled"] = parsed
            elif key in {"bypass", "raw", "disable"}:
                parsed = _parse_bool(value)
                if parsed:
                    updates["enabled"] = False
                    updates["mode"] = "off"
            elif key == "mode" and isinstance(value, str):
                mode = value.strip().lower()
                updates["mode"] = mode
                if mode == "off":
                    updates["enabled"] = False
            elif key in {
                "max_chars",
                "max_lines",
                "head_lines",
                "tail_lines",
                "important_context_lines",
                "max_important_lines",
                "artifact_size_cap",
            }:
                parsed_int = _parse_int(value, -1)
                if parsed_int >= 0:
                    updates[key] = parsed_int
            elif key == "preserve_diffs":
                parsed = _parse_bool(value)
                if parsed is not None:
                    updates["preserve_diffs"] = parsed
            elif key in {"artifacts", "artifact_enabled", "store_artifact"}:
                parsed = _parse_bool(value)
                if parsed is not None:
                    updates["artifact_enabled"] = parsed
            elif key == "artifact_dir" and value is not None:
                updates["artifact_dir"] = Path(str(value)).expanduser()

        return replace(self, **updates)


@dataclass(frozen=True, slots=True)
class ReducedOutput:
    text: str
    changed: bool
    metadata: dict[str, JsonValue]


def env_diagnostics(environ: Mapping[str, str] | None = None) -> list[str]:
    """Return human-readable warnings for ignored or fallback environment values."""
    env = environ if environ is not None else os.environ
    diagnostics: list[str] = []
    bool_vars = (
        ("NOISEGATE", "controls whether compaction is enabled"),
        ("NOISEGATE_DISABLE", "disables compaction when true"),
        ("NOISEGATE_BYPASS", "disables compaction when true"),
        ("NOISEGATE_RAW", "disables compaction when true"),
        ("NOISEGATE_ARTIFACTS", "enables private artifact storage when true"),
    )
    accepted = ", ".join(sorted(TRUE_VALUES | FALSE_VALUES))
    for name, description in bool_vars:
        value = env.get(name)
        if value is None or _parse_bool(value) is not None:
            continue
        diagnostics.append(
            f"{name}={value!r} is not recognized; ignored ({description}). "
            f"Use one of: {accepted}."
        )

    cap_value = env.get("NOISEGATE_ARTIFACT_SIZE_CAP")
    if cap_value is not None:
        parsed_cap = _parse_int(cap_value, -1)
        if parsed_cap < 0:
            diagnostics.append(
                "NOISEGATE_ARTIFACT_SIZE_CAP="
                f"{cap_value!r} is invalid; using {DEFAULT_SIZE_CAP}. "
                "Set a non-negative integer byte cap."
            )

    return diagnostics


def reduce_text(
    text: str,
    *,
    command: str | None = None,
    tool_name: str | None = None,
    source: str | None = None,
    exit_code: int | None = None,
    options: NoisegateOptions | None = None,
) -> ReducedOutput:
    try:
        return _reduce_text(
            text,
            command=command,
            tool_name=tool_name,
            source=source,
            exit_code=exit_code,
            options=options,
        )
    except Exception as exc:
        return _unchanged(text, "error", "generic", reason=f"fail_open:{type(exc).__name__}")


def _reduce_text(
    text: str,
    *,
    command: str | None = None,
    tool_name: str | None = None,
    source: str | None = None,
    exit_code: int | None = None,
    options: NoisegateOptions | None = None,
) -> ReducedOutput:
    options = options or NoisegateOptions.from_env()
    command_class = classify_command(command, text)

    if not options.enabled or options.mode == "off":
        return _unchanged(text, "disabled", command_class, reason="disabled")
    if not _has_usable_budget(text, options):
        return _unchanged(text, "invalid_budget", command_class, reason="invalid_budget")
    if _has_bypass_marker(text) or _has_bypass_marker(command or ""):
        return _unchanged(text, "bypass", command_class, reason="bypass_marker")
    if tool_name and not _is_compactable_tool_name(tool_name):
        return _unchanged(text, "protected_tool", command_class, reason="protected_tool")
    if command_class == "git_diff" and options.preserve_diffs:
        return _unchanged(text, "protected_diff", command_class, reason="diff_passthrough")
    if command_class == "patch":
        return _unchanged(text, "protected_patch", command_class, reason="patch_passthrough")
    if command_class == "file_read":
        return _unchanged(
            text,
            "protected_file_read",
            command_class,
            reason="file_read_passthrough",
        )
    if command_class == "source_search":
        return _unchanged(
            text,
            "protected_source_search",
            command_class,
            reason="source_search_passthrough",
        )
    if not _should_reduce(text, options):
        return _unchanged(text, "below_threshold", command_class, reason="below_threshold")

    reducer_name, compacted = _apply_reducer(text, command_class, options, exit_code)
    if compacted is not None and _dropped_lcm_externalized_match(before=text, after=compacted):
        compacted = None
    preserve_patterns = _preserve_patterns_for_output(command_class, text)
    if compacted is not None:
        compacted = _enforce_final_budget(
            compacted,
            options,
            preserve_patterns=preserve_patterns,
        )
    if compacted is None:
        return _unchanged(
            text,
            "no_gain",
            command_class,
            reason="reducer_no_output",
            attempted_reducer=reducer_name,
        )
    if compacted == text:
        return _unchanged(
            text,
            "no_gain",
            command_class,
            reason="reducer_unchanged",
            attempted_reducer=reducer_name,
        )
    if len(compacted) >= len(text):
        return _unchanged(
            text,
            "no_gain",
            command_class,
            reason="no_gain",
            attempted_reducer=reducer_name,
        )

    metadata = _metadata(
        original=text,
        compacted=compacted,
        reducer=reducer_name,
        command_class=command_class,
        mode=options.mode,
        tool_name=tool_name,
        source=source,
        exit_code=exit_code,
    )
    compacted_body = compacted
    if options.artifact_enabled:
        metadata["artifact"] = _plan_artifact(text, options)
        _drop_artifact_if_notice_cannot_fit(metadata, options, artifact_dir=options.artifact_dir)
    compacted = _append_recovery_notices(
        compacted_body,
        metadata,
        artifact_dir=options.artifact_dir,
        options=options,
        preserve_patterns=preserve_patterns,
    )
    if not _fits_budget(compacted, options):
        return _unchanged(
            text,
            "invalid_budget",
            command_class,
            reason="invalid_budget_after_notices",
            attempted_reducer=reducer_name,
        )
    if len(compacted) >= len(text):
        return _unchanged(
            text,
            "no_gain",
            command_class,
            reason="no_gain_after_notices",
            attempted_reducer=reducer_name,
        )
    if options.artifact_enabled:
        planned_artifact = metadata.get("artifact")
        if isinstance(planned_artifact, dict) and planned_artifact.get("stored") is True:
            planned_id = planned_artifact.get("id")
            if not isinstance(planned_id, str) or planned_id not in compacted:
                metadata["artifact"] = {
                    "stored": False,
                    "reason": "recovery_notice_dropped",
                    "size_bytes": planned_artifact.get("size_bytes"),
                }
                compacted = _append_recovery_notices(
                    compacted_body,
                    {key: value for key, value in metadata.items() if key != "artifact"},
                    artifact_dir=options.artifact_dir,
                    options=options,
                    preserve_patterns=preserve_patterns,
                )
            else:
                metadata["artifact"] = _store_artifact(text, options)
                compacted = _append_recovery_notices(
                    compacted_body,
                    metadata,
                    artifact_dir=options.artifact_dir,
                    options=options,
                    preserve_patterns=preserve_patterns,
                )
                if not _fits_budget(compacted, options):
                    return _unchanged(
                        text,
                        "invalid_budget",
                        command_class,
                        reason="invalid_budget_after_artifact_store",
                        attempted_reducer=reducer_name,
                    )
                if len(compacted) >= len(text):
                    return _unchanged(
                        text,
                        "no_gain",
                        command_class,
                        reason="no_gain_after_artifact_store",
                        attempted_reducer=reducer_name,
                    )
    return ReducedOutput(compacted, True, metadata)

def classify_command(command: str | None, text: str) -> str:
    command_l = (command or "").strip().lower()
    sample_l = text[:4_000].lower()
    text_l = text.lower()

    source_consumer_class = _source_consumer_command_class(
        command_l,
        sample_l,
        text_l,
    )
    if source_consumer_class is not None:
        return source_consumer_class
    if _looks_like_file_read_command(command_l):
        return "file_read"
    if _is_source_search_command(command_l):
        return "source_search"
    if _looks_like_v4a_patch(text):
        return "patch"
    if (
        _looks_like_diff_command(command_l)
        or "diff --git " in text
        or _looks_like_unified_diff(text)
    ):
        return "git_diff"
    if "git status" in command_l or ("on branch " in sample_l and "working tree" in sample_l):
        return "git_status"
    if "git log" in command_l:
        return "git_log"
    command_variants = _command_intent_variants(command_l)
    if any(_is_apt_command(variant) for variant in command_variants):
        return "apt"
    if any(_is_python_package_command(variant) for variant in command_variants):
        return "python_package"
    if _contains_command(command_l, ("pytest", "py.test")) or "=== failures ===" in sample_l:
        return "pytest"
    if "unittest" in command_l or re.search(r"ran \d+ tests?", sample_l):
        return "unittest"
    if any(_is_docker_log_command(variant) for variant in command_variants):
        return "docker_logs"
    if any(_is_docker_build_command(variant) for variant in command_variants) or (
        _looks_like_docker_build_output(text_l)
        and any(_can_infer_docker_build_from_output(variant) for variant in command_variants)
    ):
        return "docker_build"
    if any(_is_node_command(variant, sample_l) for variant in command_variants):
        return "node"
    return "generic"


def _is_protected_tool_name(tool_name: str | None) -> bool:
    if not tool_name:
        return False
    return tool_name in PROTECTED_TOOL_NAMES or tool_name.startswith(PROTECTED_TOOL_PREFIXES)


def _is_compactable_tool_name(tool_name: str | None) -> bool:
    if not tool_name or _is_protected_tool_name(tool_name):
        return False
    return tool_name in COMPACTABLE_TOOL_NAMES


def _lcm_externalized_patterns_for(text: str) -> tuple[re.Pattern[str], ...]:
    if _first_pattern_match(text, LCM_EXTERNALIZED_PATTERNS):
        return LCM_EXTERNALIZED_PATTERNS
    return ()


def _preserve_patterns_for_output(
    command_class: str,
    text: str,
) -> tuple[re.Pattern[str], ...] | None:
    lcm_patterns = _lcm_externalized_patterns_for(text)
    if command_class in {"pytest", "unittest"}:
        return _preservation_patterns(text, CRITICAL_PATTERNS)
    if command_class in {"apt", "python_package"}:
        return _priority_preservation_patterns(
            text,
            PACKAGE_PATTERNS,
            PACKAGE_HIGH_SIGNAL_PRIORITY_PATTERNS,
        )
    if command_class == "node":
        return _preservation_patterns(text, NODE_PATTERNS)
    if command_class == "docker_build":
        return _priority_preservation_patterns(
            text,
            DOCKER_BUILD_PRESERVATION_PATTERNS,
            DOCKER_BUILD_HIGH_SIGNAL_PRIORITY_PATTERNS,
        )
    if command_class == "docker_logs":
        return _preservation_patterns(text, DOCKER_LOG_PATTERNS)
    if command_class == "generic" and _first_pattern_match(text, CRITICAL_PATTERNS):
        return _preservation_patterns(text, CRITICAL_PATTERNS)
    return lcm_patterns or None


def _apply_reducer(
    text: str,
    command_class: str,
    options: NoisegateOptions,
    exit_code: int | None,
) -> tuple[str, str | None]:
    lcm_patterns = _lcm_externalized_patterns_for(text)
    if options.mode == "head_tail":
        if lcm_patterns:
            return "generic_head_tail", _important_lines(text, options, lcm_patterns)
        return "generic_head_tail", _head_tail(text, options)
    if command_class in {"pytest", "unittest"}:
        return command_class, _important_lines(text, options, TEST_PATTERNS + lcm_patterns)
    if command_class in {"apt", "python_package"}:
        return command_class, _important_lines(
            text,
            options,
            PACKAGE_PATTERNS + lcm_patterns,
            priority_patterns=PACKAGE_HIGH_SIGNAL_PRIORITY_PATTERNS,
        )
    if command_class == "node":
        return "node", _important_lines(
            text,
            options,
            NODE_PRESERVATION_PATTERNS + lcm_patterns,
        )
    if command_class == "docker_build":
        return "docker_build", _important_lines(
            text,
            options,
            DOCKER_BUILD_PRESERVATION_PATTERNS + lcm_patterns,
            priority_patterns=DOCKER_BUILD_HIGH_SIGNAL_PRIORITY_PATTERNS,
        )
    if command_class == "docker_logs":
        return "docker_logs", _important_lines(text, options, DOCKER_LOG_PATTERNS + lcm_patterns)
    if command_class == "git_status":
        return "git_status", _important_lines(text, options, GIT_STATUS_PATTERNS + lcm_patterns)
    if command_class == "git_log":
        return "git_log", _head_tail(text, replace(options, head_lines=40, tail_lines=8))
    if (
        command_class == "generic"
        and exit_code != 0
        and _first_pattern_match(text, CRITICAL_PATTERNS)
    ):
        return "generic_critical", _important_lines(text, options, CRITICAL_PATTERNS + lcm_patterns)
    if lcm_patterns:
        return "generic_head_tail", _important_lines(text, options, lcm_patterns)
    return "generic_head_tail", _head_tail(text, options)


TEST_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"={2,}.*(failures|errors|short test summary|failed|passed)",
        r"^(failed|error)\s+",
        r"\bFAILED\b|\bERROR\b",
        r"assertionerror|traceback|exception",
        r"^\s*E\s+",
        r"\d+\s+failed",
        r"tests?/.*::",
    )
)

CRITICAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bFAILED\b|\bERROR\b",
        r"^(failed|error)\s+",
        r"^(e:|err:|error:)",
        r"assertionerror|traceback|exceptiongroup|baseexception|\bexception\b\s*:",
        r"\bunhandled\s+exception\b",
        r"\bexception\s+in\b",
        r"^\s*E\s+",
        r"\d+\s+failed",
        r"\berror\b|\bfailed\b|\bfail\b",
        r"unable to locate package|no matching distribution found|resolutionimpossible",
        r"\b(conflict|conflicts|conflicting)\b",
        r"hash sum mismatch|dependency problems|unmet dependencies|permission denied",
    )
)

PACKAGE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^(e:|err:|error:)",
        r"unable to locate package|no matching distribution found|resolutionimpossible",
        r"\b(error|failed|failure|fail|conflict|conflicts|conflicting|traceback|exception)\b",
        r"hash sum mismatch|dependency problems|unmet dependencies|permission denied",
        r"npm err!|pnpm err!|yarn.*error|err_pnpm|elifecycle",
        r"^(warning:|warn:)",
    )
)

NODE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"npm err!|pnpm err!|yarn.*error|err_pnpm|elifecycle",
        r"\berror\b|\bfailed\b|\bfail\b",
        r"\bwarning\b|\bwarn\b",
        r"tests?.*(failed|passed)",
    )
)

NODE_PRESERVATION_PATTERNS = (*NODE_PATTERNS, *CRITICAL_PATTERNS)

DOCKER_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\berror\b|\bfailed\b|\bfail\b",
        r"dockerfile|unable to|denied|not found",
        r"^#\d+|^=>",
    )
)

DOCKER_BUILD_PRESERVATION_PATTERNS = (*DOCKER_PATTERNS, *CRITICAL_PATTERNS)

DOCKER_LOG_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\berror\b|\bfailed\b|\bfail\b",
        r"traceback|exception|fatal|critical|panic|segmentation fault|segfault",
        r"valueerror|typeerror|runtimeerror|assertionerror",
        r"unable to|denied|not found|permission denied",
    )
)

HIGH_SIGNAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"valueerror|typeerror|runtimeerror|assertionerror",
        r"traceback \(most recent call last\)",
        r"resolutionimpossible|because .*conflicts? with",
        r"no matching distribution found|unable to locate package",
        r"failed to solve|did not complete successfully",
        r"fatal|critical|panic|segmentation fault|segfault",
        r"permission denied|access denied|denied|not found",
    )
)

HIGH_SIGNAL_PRIORITY_PATTERNS = tuple(
    tuple(re.compile(pattern, re.IGNORECASE) for pattern in group)
    for group in (
        (
            r"traceback \(most recent call last\)",
            r"valueerror|typeerror|runtimeerror|assertionerror",
        ),
        (
            r"resolutionimpossible",
            r"because .*conflicts? with",
            r"no matching distribution found",
            r"unable to locate package",
        ),
        (
            r"failed to solve",
            r"did not complete successfully",
        ),
        (
            r"fatal|critical|panic|segmentation fault|segfault",
            r"permission denied|access denied",
        ),
        (
            r"denied|not found",
        ),
    )
)

PACKAGE_HIGH_SIGNAL_PRIORITY_PATTERNS = tuple(
    tuple(re.compile(pattern, re.IGNORECASE) for pattern in group)
    for group in (
        (
            r"resolutionimpossible",
            r"because .*conflicts? with",
            r"no matching distribution found",
            r"unable to locate package",
            r"dependency problems|unmet dependencies",
            r"hash sum mismatch",
        ),
        (
            r"traceback \(most recent call last\)",
            r"valueerror|typeerror|runtimeerror|assertionerror",
        ),
        (
            r"fatal|critical|panic|segmentation fault|segfault",
            r"permission denied|access denied",
        ),
        (
            r"failed|failure|fail",
        ),
        (
            r"^(e:|err:|error:)",
            r"\berror\b",
        ),
    )
)

DOCKER_BUILD_HIGH_SIGNAL_PRIORITY_PATTERNS = tuple(
    tuple(re.compile(pattern, re.IGNORECASE) for pattern in group)
    for group in (
        (
            r"failed to solve",
            r"did not complete successfully",
            r"traceback \(most recent call last\)",
            r"valueerror|typeerror|runtimeerror|assertionerror",
        ),
        (
            r"resolutionimpossible",
            r"because .*conflicts? with",
            r"no matching distribution found",
            r"unable to locate package",
        ),
        (
            r"fatal|critical|panic|segmentation fault|segfault",
            r"permission denied|access denied",
        ),
        (
            r"denied|not found",
        ),
    )
)


GIT_STATUS_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^on branch|working tree|changes|untracked|modified|deleted|renamed|new file",
        r"^\s*(modified|deleted|renamed|new file):",
        r"^\s*[MADRCU?]{1,2}\s+",
    )
)


def _important_lines(
    text: str,
    options: NoisegateOptions,
    patterns: tuple[re.Pattern[str], ...],
    *,
    priority_patterns: tuple[tuple[re.Pattern[str], ...], ...] = HIGH_SIGNAL_PRIORITY_PATTERNS,
) -> str | None:
    lines = text.splitlines()
    if (
        len(lines) <= options.head_lines + options.tail_lines + 1
        and len(lines) <= options.max_lines
    ):
        if len(text) > options.max_chars:
            return _char_head_tail_preserving_patterns(
                text,
                options,
                _priority_preservation_patterns(text, patterns, priority_patterns),
            )
        return _char_head_tail(text, options)

    important: list[int] = []
    for index, line in enumerate(lines):
        if any(pattern.search(line) for pattern in patterns):
            important.append(index)

    if len(important) > options.max_important_lines:
        priority = _important_priority_indices(lines, important, priority_patterns)
        if priority:
            important = _trim_indices_around_priority(
                important,
                priority,
                options.max_important_lines,
            )
        else:
            half = max(1, options.max_important_lines // 2)
            important = important[:half] + important[-half:]

    keep: set[int] = set(range(min(options.head_lines, len(lines))))
    tail_start = max(0, len(lines) - options.tail_lines)
    keep.update(range(tail_start, len(lines)))

    context = options.important_context_lines
    for index in important:
        start = max(0, index - context)
        end = min(len(lines), index + context + 1)
        keep.update(range(start, end))

    if len(keep) >= len(lines):
        if len(lines) > options.max_lines:
            budgeted = _line_budgeted_important_excerpt(
                lines,
                important,
                options,
                patterns,
                priority_patterns,
            )
            if budgeted is not None:
                return budgeted
            return _head_tail(text, options)
        if len(text) > options.max_chars:
            return _char_head_tail_preserving_patterns(
                text,
                options,
                _priority_preservation_patterns(text, patterns, priority_patterns),
            )
        return _char_head_tail(text, options)
    selected = _lines_with_markers(lines, sorted(keep))
    if _line_count(selected) > options.max_lines:
        budgeted = _line_budgeted_important_excerpt(
            lines,
            important,
            options,
            patterns,
            priority_patterns,
        )
        if budgeted is None:
            return None
        selected = budgeted
    if len(selected) > options.max_chars:
        if priority_patterns is not HIGH_SIGNAL_PRIORITY_PATTERNS:
            budgeted = _line_budgeted_important_excerpt(
                lines,
                important,
                options,
                patterns,
                priority_patterns,
            )
            if budgeted is not None:
                return budgeted
        return _char_head_tail_preserving_patterns(
            selected,
            options,
            _priority_preservation_patterns(selected, patterns, priority_patterns),
        )
    return selected


def _preservation_patterns(
    text: str,
    patterns: tuple[re.Pattern[str], ...],
) -> tuple[re.Pattern[str], ...]:
    base_patterns = patterns
    uses_test_or_critical_patterns = patterns is CRITICAL_PATTERNS or any(
        pattern in TEST_PATTERNS for pattern in patterns
    )
    if patterns in {NODE_PATTERNS, NODE_PRESERVATION_PATTERNS}:
        if _first_pattern_match(text, CRITICAL_PATTERNS):
            base_patterns = NODE_PRESERVATION_PATTERNS
    elif uses_test_or_critical_patterns:
        if _first_pattern_match(text, CRITICAL_PATTERNS):
            base_patterns = CRITICAL_PATTERNS
    elif _first_pattern_match(text, HIGH_SIGNAL_PATTERNS):
        base_patterns = HIGH_SIGNAL_PATTERNS
    elif _first_pattern_match(text, CRITICAL_PATTERNS):
        base_patterns = CRITICAL_PATTERNS

    lcm_patterns = _lcm_externalized_patterns_for(text)
    if lcm_patterns:
        return lcm_patterns + tuple(
            pattern for pattern in base_patterns if pattern not in lcm_patterns
        )
    return base_patterns


def _priority_preservation_patterns(
    text: str,
    patterns: tuple[re.Pattern[str], ...],
    priority_patterns: tuple[tuple[re.Pattern[str], ...], ...],
) -> tuple[re.Pattern[str], ...]:
    if patterns is CRITICAL_PATTERNS or any(pattern in TEST_PATTERNS for pattern in patterns):
        return _preservation_patterns(text, patterns)
    flattened = tuple(pattern for group in priority_patterns for pattern in group)
    if _first_pattern_match(text, flattened):
        lcm_patterns = _lcm_externalized_patterns_for(text)
        if lcm_patterns:
            return lcm_patterns + tuple(
                pattern for pattern in flattened if pattern not in lcm_patterns
            )
        return flattened
    return _preservation_patterns(text, patterns)


def _priority_indices(
    lines: list[str],
    indices: list[int],
    priority_patterns: tuple[tuple[re.Pattern[str], ...], ...] = HIGH_SIGNAL_PRIORITY_PATTERNS,
) -> list[int]:
    for pattern_group in priority_patterns:
        matches = [
            index
            for index in indices
            if any(pattern.search(lines[index]) for pattern in pattern_group)
        ]
        if matches:
            return matches
    return [
        index
        for index in indices
        if any(pattern.search(lines[index]) for pattern in CRITICAL_PATTERNS)
    ]


def _line_budgeted_important_excerpt(
    lines: list[str],
    important: list[int],
    options: NoisegateOptions,
    patterns: tuple[re.Pattern[str], ...],
    priority_patterns: tuple[tuple[re.Pattern[str], ...], ...] = HIGH_SIGNAL_PRIORITY_PATTERNS,
) -> str | None:
    if not important:
        return None
    lcm_priority = _matching_indices(lines, important, LCM_EXTERNALIZED_PATTERNS)
    signal_priority = _priority_indices(lines, important, priority_patterns)
    critical_priority = _matching_indices(lines, important, CRITICAL_PATTERNS)
    ranked_priority = sorted(
        signal_priority or critical_priority or important,
        key=lambda index: (_failure_detail_rank(lines[index]), index),
    )
    fallback_priority = sorted(
        [
            index
            for index in critical_priority
            if index not in signal_priority and index not in ranked_priority
        ],
        key=lambda index: (_failure_detail_rank(lines[index]), index),
    )
    priority = lcm_priority + [
        index
        for index in ranked_priority + fallback_priority
        if index not in lcm_priority
    ]
    best_rank = min(_failure_detail_rank(lines[index]) for index in ranked_priority)
    max_context = max(0, options.important_context_lines)

    # Externalized payload refs are recovery handles, not just interesting log
    # lines. If they appear alongside a failure cluster, keep the refs and at
    # least one failure anchor together when the line budget allows it. If the
    # budget is too tight for both, the fallback below prioritizes the refs so
    # the externalized-payload recovery path stays intact.
    multi_anchor_priority = lcm_priority + [
        index for index in ranked_priority[:1] if index not in lcm_priority
    ]
    if len(multi_anchor_priority) > 1:
        for context in range(max_context, -1, -1):
            keep: set[int] = set()
            for anchor in multi_anchor_priority:
                start = max(0, anchor - context)
                end = min(len(lines), anchor + context + 1)
                keep.update(range(start, end))
            candidate = _lines_with_markers(lines, sorted(keep))
            if _line_count(candidate) <= options.max_lines:
                return candidate

    for anchor in priority:
        if anchor not in lcm_priority and _would_hide_better_failure_anchor(
            best_rank,
            _failure_detail_rank(lines[anchor]),
        ):
            return None
        for context in range(max_context, -1, -1):
            start = max(0, anchor - context)
            end = min(len(lines), anchor + context + 1)
            candidate = _lines_with_surrounding_omission_markers(lines, start, end - 1)
            if _line_count(candidate) <= options.max_lines:
                if len(candidate) <= options.max_chars:
                    return candidate
                char_capped = _char_head_tail_preserving_patterns(
                    candidate,
                    options,
                    _preservation_patterns(candidate, patterns),
                )
                if (
                    char_capped is not None
                    and _fits_budget(char_capped, options)
                    and _contains_full_line(char_capped, lines[anchor])
                ):
                    return char_capped
                continue
    return None


def _failure_detail_rank(line: str) -> int:
    """Prefer diagnostic detail over progress/status lines when budgets are tight."""
    if _is_diagnostic_detail_line(line):
        return 0
    if re.search(r"npm err!|pnpm err!|err_pnpm|elifecycle|yarn.*error", line, re.IGNORECASE):
        return 0
    if re.search(r"\btraceback\b", line, re.IGNORECASE):
        return 0
    if re.search(r"\bFAILED\b.*tests?/.*::|tests?/.*::.*\bFAILED\b", line, re.IGNORECASE):
        return 1
    if re.search(r"^failed\s+|^error\s+tests?/.*::", line, re.IGNORECASE):
        return 1
    if re.search(r"={2,}.*(failures|errors|short test summary)", line, re.IGNORECASE):
        return 2
    if re.search(r"\d+\s+failed", line, re.IGNORECASE):
        return 3
    if re.search(r"^\s*(?:={2,}.*)?\d+\s+passed\b", line, re.IGNORECASE):
        return 4
    if _is_incidental_exception_line(line):
        return 6
    return 5


def _is_diagnostic_detail_line(line: str) -> bool:
    if re.search(r"^\s*E\s+", line):
        return True
    if _is_incidental_exception_line(line):
        return False
    return bool(
        re.search(
            r"\b(assertionerror|[a-z0-9_]*error|exceptiongroup|baseexception)\b(?::|$)"
            r"|\bexception\b\s*:"
            r"|\bunhandled\s+exception\b"
            r"|\bexception\s+in\b",
            line,
            re.IGNORECASE,
        )
    )


def _is_incidental_exception_line(line: str) -> bool:
    return bool(re.search(r"\bexception ignored\b", line, re.IGNORECASE))


def _important_priority_indices(
    lines: list[str],
    indices: list[int],
    priority_patterns: tuple[tuple[re.Pattern[str], ...], ...] = HIGH_SIGNAL_PRIORITY_PATTERNS,
) -> list[int]:
    lcm_priority = _matching_indices(lines, indices, LCM_EXTERNALIZED_PATTERNS)
    signal_priority = sorted(
        _priority_indices(lines, indices, priority_patterns),
        key=lambda index: (_failure_detail_rank(lines[index]), index),
    )
    critical_priority = sorted(
        [
            index
            for index in _matching_indices(lines, indices, CRITICAL_PATTERNS)
            if index not in signal_priority
        ],
        key=lambda index: (_failure_detail_rank(lines[index]), index),
    )
    return lcm_priority + [
        index
        for index in signal_priority + critical_priority
        if index not in lcm_priority
    ]


def _matching_indices(
    lines: list[str],
    indices: list[int],
    patterns: tuple[re.Pattern[str], ...],
) -> list[int]:
    return [
        index
        for index in indices
        if any(pattern.search(lines[index]) for pattern in patterns)
    ]


def _trim_indices_around_priority(
    indices: list[int],
    priority: list[int],
    limit: int,
) -> list[int]:
    if limit <= 0:
        return []
    selected: list[int] = []
    selected_set: set[int] = set()
    for index in priority:
        if index not in selected_set:
            selected.append(index)
            selected_set.add(index)
        if len(selected) >= limit:
            return sorted(selected)

    remaining = [index for index in indices if index not in selected_set]
    left = 0
    right = len(remaining) - 1
    take_left = True
    while remaining and len(selected) < limit and left <= right:
        if take_left:
            selected.append(remaining[left])
            left += 1
        else:
            selected.append(remaining[right])
            right -= 1
        take_left = not take_left
    return sorted(selected)


def _head_tail(text: str, options: NoisegateOptions) -> str | None:
    lines = text.splitlines()
    if len(lines) <= 1:
        return _char_head_tail(text, options)

    max_keep = min(
        max(1, options.head_lines + options.tail_lines),
        max(1, options.max_lines - 1),
        len(lines) - 1,
    )
    head_count = min(options.head_lines, max_keep)
    tail_count = max_keep - head_count
    if tail_count == 0 and max_keep > 1:
        tail_count = 1
        head_count = max_keep - 1

    if head_count + tail_count >= len(lines):
        return _char_head_tail(text, options)

    keep = list(range(head_count))
    tail_start = max(head_count, len(lines) - tail_count)
    keep.extend(range(tail_start, len(lines)))
    return _lines_with_markers(lines, keep)


def _enforce_final_budget(
    text: str,
    options: NoisegateOptions,
    *,
    preserve_patterns: tuple[re.Pattern[str], ...] | None = None,
) -> str | None:
    compacted = text
    if _line_count(compacted) > options.max_lines:
        if preserve_patterns is not None and _first_pattern_match(compacted, preserve_patterns):
            line_capped = _important_lines(compacted, options, preserve_patterns)
        else:
            line_capped = _head_tail(compacted, options)
        if line_capped is not None and len(line_capped) < len(compacted):
            if _dropped_lcm_externalized_match(before=compacted, after=line_capped):
                return None
            compacted = line_capped
    if len(compacted) > options.max_chars:
        if preserve_patterns is not None and _first_pattern_match(compacted, preserve_patterns):
            char_capped = _char_head_tail_preserving_patterns(
                compacted,
                options,
                preserve_patterns,
            )
        else:
            char_capped = _char_head_tail(compacted, options)
        if char_capped is not None and len(char_capped) < len(compacted):
            if _dropped_lcm_externalized_match(before=compacted, after=char_capped):
                return None
            compacted = char_capped
    if not _fits_budget(compacted, options):
        return None
    return compacted


def _char_head_tail_preserving_patterns(
    text: str,
    options: NoisegateOptions,
    patterns: tuple[re.Pattern[str], ...],
) -> str | None:
    if len(text) <= options.max_chars:
        return None
    matches = _ranked_pattern_line_matches(text, patterns)
    if not matches:
        return _char_head_tail(text, options)
    layout = _line_layout(text)
    best_rank = min(_rank_for_span_match(match, layout) for match in matches)
    for match in matches:
        if _would_hide_better_failure_anchor(best_rank, _rank_for_span_match(match, layout)):
            return None
        line_excerpt = _line_centered_excerpt(text, options, match, layout=layout)
        if line_excerpt is not None:
            return line_excerpt
    return None


def _rank_for_span_match(match: _SpanMatch, layout: _LineLayout) -> int:
    if match.detail_rank is not None:
        return match.detail_rank
    for index, (start, end) in enumerate(layout.offsets):
        if start <= match.start() <= end:
            return _failure_detail_rank(layout.lines[index])
    return 5


def _would_hide_better_failure_anchor(best_rank: int, candidate_rank: int) -> bool:
    return best_rank <= 1 and candidate_rank > 1


def _dropped_lcm_externalized_match(*, before: str, after: str) -> bool:
    return any(match not in after for match in _lcm_externalized_matches(before))


def _lcm_externalized_matches(text: str) -> list[str]:
    matches: list[str] = []
    for pattern in LCM_EXTERNALIZED_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0)
            if value not in matches:
                matches.append(value)
    return matches


def _match_centered_excerpt(
    text: str,
    options: NoisegateOptions,
    match: re.Match[str],
) -> str | None:
    budget = max(0, options.max_chars)
    match_len = match.end() - match.start()
    if budget <= 0 or match_len > budget:
        return None
    line_excerpt = _line_centered_excerpt(text, options, match)
    if line_excerpt is not None:
        return line_excerpt
    return None


def _line_centered_excerpt(
    text: str,
    options: NoisegateOptions,
    match: re.Match[str] | _SpanMatch,
    *,
    layout: _LineLayout | None = None,
) -> str | None:
    if "\n" not in text:
        return None
    layout = layout or _line_layout(text)
    lines = layout.lines
    offsets = layout.offsets

    match_line = match.line_index if isinstance(match, _SpanMatch) else None
    if (
        match_line is None
        or match_line < 0
        or match_line >= len(offsets)
        or not offsets[match_line][0] <= match.start() <= offsets[match_line][1]
    ):
        match_line = next(
            (
                index
                for index, (start, end) in enumerate(offsets)
                if start <= match.start() <= end
            ),
            None,
        )
    if match_line is None:
        return None

    line = lines[match_line]
    if len(line) > options.max_chars:
        return None

    if not _fits_budget(line, options):
        return None

    start_line = end_line = match_line
    while True:
        if end_line + 1 < len(lines):
            candidate = "\n".join(lines[start_line : end_line + 2])
            if _fits_budget(candidate, options):
                end_line += 1
                continue
        if start_line > 0:
            candidate = "\n".join(lines[start_line - 1 : end_line + 1])
            if _fits_budget(candidate, options):
                start_line -= 1
                continue
        break

    while True:
        candidate = _lines_with_surrounding_omission_markers(lines, start_line, end_line)
        if _fits_budget(candidate, options):
            return candidate
        if start_line == match_line and end_line == match_line:
            return None
        if end_line > match_line and (end_line - match_line) >= (match_line - start_line):
            end_line -= 1
        elif start_line < match_line:
            start_line += 1
        elif end_line > match_line:
            end_line -= 1
        else:
            return None


def _lines_with_surrounding_omission_markers(
    lines: list[str],
    start_line: int,
    end_line: int,
) -> str:
    selected = _lines_with_markers(lines, list(range(start_line, end_line + 1)))
    if end_line + 1 < len(lines):
        selected = f"{selected}\n[noisegate: omitted {len(lines) - end_line - 1} lines]"
    return selected


def _contains_full_line(text: str, line: str) -> bool:
    return line in text.splitlines()


def _match_centered_slice(
    text: str,
    options: NoisegateOptions,
    match: re.Match[str] | _SpanMatch,
) -> str | None:
    budget = max(0, options.max_chars)
    match_len = match.end() - match.start()
    if budget <= 0 or match_len > budget:
        return None
    slack = budget - match_len
    start = match.start() - slack // 2
    start = max(0, min(start, len(text) - budget))
    end = min(len(text), start + budget)
    if match.end() > end:
        end = match.end()
        start = max(0, end - budget)
    if start > match.start() or end < match.end():
        return None
    excerpt = text[start:end]
    if _has_partial_noisegate_marker(excerpt):
        return None
    if not _fits_budget(excerpt, options):
        return None
    return excerpt


@dataclass(frozen=True)
class _SpanMatch:
    start_index: int
    end_index: int
    line_index: int | None = None
    detail_rank: int | None = None

    def start(self) -> int:
        return self.start_index

    def end(self) -> int:
        return self.end_index


@dataclass(frozen=True, slots=True)
class _LineLayout:
    lines: list[str]
    offsets: list[tuple[int, int]]


def _line_layout(text: str) -> _LineLayout:
    lines: list[str] = []
    offsets: list[tuple[int, int]] = []
    cursor = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        start = cursor
        end = start + len(line)
        lines.append(line)
        offsets.append((start, end))
        cursor += len(raw_line)
    return _LineLayout(lines=lines, offsets=offsets)


def _has_partial_noisegate_marker(text: str) -> bool:
    for line in text.splitlines():
        if "[noisegate:" in line and "]" not in line:
            return True
        if "noisegate:" in line and "[noisegate:" not in line:
            return True
        if re.search(r"(^|\s)([a-z]*ted|omitted) \d+ (chars|lines)\]", line):
            return True
        if re.search(r"\d+ (chars|lines)\]", line) and not line.strip().startswith(
            "[noisegate: omitted "
        ):
            return True
        if re.search(r"^\s*(chars|lines)\]", line):
            return True
    return False


def _dropped_preserved_match(
    *,
    before: str,
    after: str,
    preserve_patterns: tuple[re.Pattern[str], ...] | None,
) -> bool:
    return (
        preserve_patterns is not None
        and _first_pattern_match(before, preserve_patterns) is not None
        and _first_pattern_match(after, preserve_patterns) is None
    )


def _dropped_preserved_line(
    *,
    before: str,
    after: str,
    preserve_patterns: tuple[re.Pattern[str], ...] | None,
) -> bool:
    if preserve_patterns is None:
        return False
    for line in before.splitlines():
        if any(pattern.search(line) for pattern in preserve_patterns) and line not in after:
            return True
    return False


def _first_pattern_match(
    text: str,
    patterns: tuple[re.Pattern[str], ...],
) -> re.Match[str] | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match is not None:
            return match
    return None


def _best_pattern_line_match(
    text: str,
    patterns: tuple[re.Pattern[str], ...],
) -> _SpanMatch | None:
    matches = _ranked_pattern_line_matches(text, patterns)
    return matches[0] if matches else None


def _ranked_pattern_line_matches(
    text: str,
    patterns: tuple[re.Pattern[str], ...],
) -> list[_SpanMatch]:
    candidates: list[tuple[tuple[int, int, int, int, int], _SpanMatch]] = []
    pattern_order_first = patterns is NODE_PATTERNS or _patterns_prefer_declared_order(
        patterns
    )
    offset = 0
    for line_index, line in enumerate(text.splitlines(keepends=True)):
        stripped_line = line.rstrip("\r\n")
        for pattern_index, pattern in enumerate(patterns):
            match = pattern.search(stripped_line)
            if match is None:
                continue
            detail_rank = _failure_detail_rank(stripped_line)
            if pattern_order_first:
                rank = (
                    pattern_index,
                    detail_rank,
                    line_index,
                    offset + match.start(),
                    offset + match.end(),
                )
            else:
                rank = (
                    detail_rank,
                    pattern_index,
                    line_index,
                    offset + match.start(),
                    offset + match.end(),
                )
            candidates.append(
                (
                    rank,
                    _SpanMatch(
                        offset + match.start(),
                        offset + match.end(),
                        line_index=line_index,
                        detail_rank=detail_rank,
                    ),
                )
            )
        offset += len(line)
    return [match for _, match in sorted(candidates, key=lambda candidate: candidate[0])]


def _patterns_prefer_declared_order(patterns: tuple[re.Pattern[str], ...]) -> bool:
    priority_sets = (
        HIGH_SIGNAL_PATTERNS,
        tuple(pattern for group in HIGH_SIGNAL_PRIORITY_PATTERNS for pattern in group),
        tuple(pattern for group in PACKAGE_HIGH_SIGNAL_PRIORITY_PATTERNS for pattern in group),
        tuple(pattern for group in DOCKER_BUILD_HIGH_SIGNAL_PRIORITY_PATTERNS for pattern in group),
    )
    return any(
        patterns is priority_set or patterns == priority_set
        for priority_set in priority_sets
    )


def _char_head_tail(text: str, options: NoisegateOptions) -> str | None:
    if len(text) <= options.max_chars:
        return None
    budget = max(0, options.max_chars)
    if budget == 0:
        return None
    omitted = 0
    marker = ""
    available = budget
    head_chars = tail_chars = 0

    for _ in range(3):
        marker = f"\n[noisegate: omitted {omitted} chars]\n"
        available = max(0, budget - len(marker))
        if available < MIN_HEAD_TAIL_CHARS:
            return None
        head_chars = max(1, (available * 3) // 5)
        tail_chars = max(1, available - head_chars)
        if head_chars + tail_chars >= len(text):
            return None
        omitted = len(text) - head_chars - tail_chars

    marker = f"\n[noisegate: omitted {omitted} chars]\n"
    available = max(0, budget - len(marker))
    if available < MIN_HEAD_TAIL_CHARS:
        return None
    head_chars = max(1, (available * 3) // 5)
    tail_chars = max(1, available - head_chars)
    overflow = head_chars + tail_chars + len(marker) - budget
    if overflow > 0:
        tail_chars = max(1, tail_chars - overflow)
    if head_chars + tail_chars >= len(text):
        return None
    omitted = len(text) - head_chars - tail_chars
    marker = f"\n[noisegate: omitted {omitted} chars]\n"
    return f"{text[:head_chars]}{marker}{text[-tail_chars:]}"


def _lines_with_markers(lines: list[str], keep: list[int]) -> str:
    output: list[str] = []
    previous = -1
    for index in keep:
        if index <= previous:
            continue
        gap = index - previous - 1
        if gap > 0:
            output.append(f"[noisegate: omitted {gap} lines]")
        output.append(lines[index])
        previous = index
    return "\n".join(output)


def _metadata(
    *,
    original: str,
    compacted: str,
    reducer: str,
    command_class: str,
    mode: str,
    tool_name: str | None,
    source: str | None,
    exit_code: int | None,
) -> dict[str, JsonValue]:
    original_lines = _line_count(original)
    compacted_lines = _line_count(compacted)
    metadata: dict[str, JsonValue] = {
        "version": __version__,
        "compacted": True,
        "mode": mode,
        "reducer": reducer,
        "command_class": command_class,
        "original_chars": len(original),
        "original_lines": original_lines,
        "omitted_chars": max(0, len(original) - len(compacted)),
        "omitted_lines": max(0, original_lines - compacted_lines),
    }
    if tool_name:
        metadata["tool_name"] = tool_name
    if source:
        metadata["source"] = source
    if exit_code is not None:
        metadata["exit_code"] = exit_code
    return metadata


def _unchanged(
    text: str,
    reducer: str,
    command_class: str,
    *,
    reason: str,
    attempted_reducer: str | None = None,
) -> ReducedOutput:
    metadata: dict[str, JsonValue] = {
        "version": __version__,
        "compacted": False,
        "mode": "passthrough",
        "reducer": reducer,
        "command_class": command_class,
        "reason": reason,
        "original_chars": len(text),
        "original_lines": _line_count(text),
        "omitted_chars": 0,
        "omitted_lines": 0,
    }
    if attempted_reducer is not None:
        metadata["attempted_reducer"] = attempted_reducer
    return ReducedOutput(text, False, metadata)


def _plan_artifact(text: str, options: NoisegateOptions) -> dict[str, JsonValue]:
    data = text.encode("utf-8")
    if len(data) > options.artifact_size_cap:
        return {
            "stored": False,
            "reason": "too_large",
            "size_bytes": len(data),
            "size_cap": options.artifact_size_cap,
        }
    digest = hashlib.sha256(data).hexdigest()
    return {
        "stored": True,
        "id": f"ng_{digest[:24]}",
        "sha256": digest,
        "size_bytes": len(data),
    }


def _drop_artifact_if_notice_cannot_fit(
    metadata: dict[str, JsonValue],
    options: NoisegateOptions,
    *,
    artifact_dir: Path | None = None,
) -> None:
    artifact = metadata.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("stored") is not True:
        return
    suffix = "\n" + "\n".join(_recovery_notices(metadata, artifact_dir=artifact_dir))
    if len(suffix) >= max(0, options.max_chars):
        metadata["artifact"] = {
            "stored": False,
            "reason": "recovery_notice_too_long",
            "size_bytes": artifact.get("size_bytes"),
        }


def _store_artifact(text: str, options: NoisegateOptions) -> dict[str, JsonValue]:
    try:
        store = ArtifactStore(options.artifact_dir, size_cap=options.artifact_size_cap)
        artifact = store.store(text)
        return {"stored": True, **artifact.to_metadata()}
    except ArtifactTooLarge as exc:
        return {
            "stored": False,
            "reason": "too_large",
            "size_bytes": exc.size_bytes,
            "size_cap": exc.size_cap,
        }
    except ArtifactError as exc:
        return {"stored": False, "reason": "artifact_error", "error": str(exc)}
    except Exception as exc:
        return {"stored": False, "reason": "artifact_error", "error": str(exc)}


def _append_recovery_notices(
    text: str,
    metadata: dict[str, JsonValue],
    *,
    artifact_dir: Path | None = None,
    options: NoisegateOptions | None = None,
    preserve_patterns: tuple[re.Pattern[str], ...] | None = None,
) -> str:
    notice_metadata = metadata
    if (
        preserve_patterns is not None
        and _first_pattern_match(text, preserve_patterns) is not None
        and _has_nonrecoverable_artifact(metadata)
    ):
        notice_metadata = dict(metadata)
        notice_metadata.pop("artifact", None)

    notices = _recovery_notices(notice_metadata, artifact_dir=artifact_dir)
    if not notices:
        return text
    suffix = "\n" + "\n".join(notices)
    if options is None:
        return f"{text}{suffix}"

    budget = max(0, options.max_chars)
    if budget == 0:
        return ""
    if len(text) + len(suffix) <= budget:
        candidate = f"{text}{suffix}"
        if _fits_budget(candidate, options):
            return candidate
        return text
    if len(suffix) >= budget:
        return _enforce_final_budget(
            text,
            options,
            preserve_patterns=preserve_patterns,
        ) or text

    text_budget = budget - len(suffix)
    reserved_options = replace(
        options,
        max_chars=text_budget,
        max_lines=max(1, options.max_lines - _line_count(suffix)),
    )
    shortened = _enforce_final_budget(
        text,
        reserved_options,
        preserve_patterns=preserve_patterns,
    )
    if shortened is not None and (
        _dropped_preserved_match(
            before=text,
            after=shortened,
            preserve_patterns=preserve_patterns,
        )
        or _dropped_preserved_line(
            before=text,
            after=shortened,
            preserve_patterns=preserve_patterns,
        )
    ):
        return text
    if shortened is None:
        notice_only = suffix.lstrip("\n")
        if (
            (preserve_patterns is None or _first_pattern_match(text, preserve_patterns) is None)
            and _has_recoverable_artifact(metadata)
            and _fits_budget(notice_only, options)
        ):
            return notice_only
        return text
    candidate = f"{shortened}{suffix}"
    if not _fits_budget(candidate, options):
        return text
    return candidate


def _has_nonrecoverable_artifact(metadata: dict[str, JsonValue]) -> bool:
    artifact = metadata.get("artifact")
    return isinstance(artifact, dict) and artifact.get("stored") is not True


def _has_recoverable_artifact(metadata: dict[str, JsonValue]) -> bool:
    artifact = metadata.get("artifact")
    return isinstance(artifact, dict) and artifact.get("stored") is True


def _recovery_notices(
    metadata: dict[str, JsonValue],
    *,
    artifact_dir: Path | None = None,
) -> list[str]:
    notices: list[str] = []
    exit_code = metadata.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        notices.append(f"[noisegate: exit_code={exit_code}]")

    artifact = metadata.get("artifact")
    if isinstance(artifact, dict):
        if artifact.get("stored") is True:
            artifact_id = artifact.get("id")
            sha256 = str(artifact.get("sha256") or "")[:16]
            command = f"noisegate cat {artifact_id}"
            if artifact_dir is not None:
                quoted_dir = shlex.quote(str(artifact_dir))
                command = f"noisegate cat --artifact-dir {quoted_dir} {artifact_id}"
            notices.append(
                "[noisegate artifact: "
                f"id={artifact_id}; sha256={sha256}; {command}]"
            )
        else:
            reason = artifact.get("reason", "not_stored")
            notices.append(f"[noisegate artifact: not stored; reason={reason}]")

    return notices


def _has_usable_budget(text: str, options: NoisegateOptions) -> bool:
    if options.max_chars <= 0 or options.max_lines <= 0:
        return False
    if len(text) <= options.max_chars and _line_count(text) <= options.max_lines:
        return True
    if options.max_lines < 3:
        return False
    max_omitted_chars = max(1, len(text) - MIN_HEAD_TAIL_CHARS)
    max_omitted_lines = max(1, _line_count(text) - MIN_HEAD_TAIL_CHARS)
    longest_marker = max(
        f"\n[noisegate: omitted {max_omitted_chars} chars]\n",
        f"[noisegate: omitted {max_omitted_lines} lines]",
        key=len,
    )
    return options.max_chars >= len(longest_marker) + MIN_HEAD_TAIL_CHARS


def _fits_budget(text: str, options: NoisegateOptions) -> bool:
    return len(text) <= options.max_chars and _line_count(text) <= options.max_lines


def _should_reduce(text: str, options: NoisegateOptions) -> bool:
    return len(text) > options.max_chars or _line_count(text) > options.max_lines


def _line_count(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines())


def _has_bypass_marker(text: str) -> bool:
    sample = text[:2_000]
    return any(marker in sample for marker in BYPASS_MARKERS)


def _contains_command(command: str, names: tuple[str, ...]) -> bool:
    tokens = re.split(r"[\s;&|()]+", command)
    return any(name in tokens for name in names)


def _looks_like_diff_command(command: str) -> bool:
    if not command:
        return False
    if bool(re.search(r"(^|[\s;&|])git\b.*\bdiff\b", command)) or command.startswith("diff "):
        return True
    if re.search(r"(^|[\s;&|])git\b.*\blog\b.*?(\s-p\b|\s--patch\b)", command):
        return True
    return bool(re.search(r"(^|[\s;&|])git\b.*\bshow\b", command)) and not bool(
        re.search(
            r"\s--(no-patch|stat|summary|name-only|name-status)\b",
            command,
        )
    )


def _looks_like_unified_diff(text: str) -> bool:
    return bool(
        re.search(r"(^|\n)---\s+", text)
        and re.search(r"(^|\n)\+\+\+\s+", text)
        and re.search(r"(^|\n)@@\s+-\d", text)
    )


def _looks_like_v4a_patch(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return len(lines) >= 2 and lines[0] == "*** Begin Patch" and lines[-1] == "*** End Patch"


def _looks_like_file_read_command(command: str) -> bool:
    if not command or _has_unsafe_shell_expansion(command):
        return False
    segments = _command_token_segments(command)
    for index, tokens in enumerate(segments):
        if _is_setup_segment(tokens):
            continue
        if not _tokens_start_file_read(tokens):
            continue
        later_segments = [
            segment
            for segment in segments[index + 1 :]
            if segment and not _is_setup_segment(segment)
        ]
        return not any(
            _tokens_start_compactable_command(segment) for segment in later_segments
        )
    return False


def _has_unsafe_shell_expansion(command: str) -> bool:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if quote == '"':
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                quote = None
                continue
            if char == "`" or (char == "$" and command[index + 1 : index + 2] == "("):
                return True
            continue
        if char == "\\":
            escaped = True
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char in "><`\n\r" or (char == "$" and command[index + 1 : index + 2] == "("):
            return True
    return quote is not None


def _looks_like_sed_print_script(token: str) -> bool:
    script = token.strip()
    return bool(
        re.fullmatch(r"\d+(,\d+)?p", script)
        or re.fullmatch(r"\d+,\$p", script)
        or re.fullmatch(r"\$p", script)
    )


def _looks_like_sed_search_script(token: str) -> bool:
    return bool(re.fullmatch(r"/.+/p", token.strip()))


def _looks_like_sed_file_read_tokens(tokens: list[str]) -> bool:
    if not tokens or Path(tokens[0]).name != "sed":
        return False
    saw_script = False
    has_file_arg = False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            has_file_arg = any(not item.startswith("-") for item in tokens[index + 1 :])
            break
        if token == "-i" or token == "--in-place" or token.startswith("-i"):
            return False
        if token in {"-e", "--expression"} and index + 1 < len(tokens):
            script = tokens[index + 1]
            if _looks_like_sed_search_script(script):
                return False
            saw_script = True
            index += 2
            continue
        if token.startswith("--expression="):
            script = token.split("=", 1)[1]
            if _looks_like_sed_search_script(script):
                return False
            saw_script = True
            index += 1
            continue
        if token in {"-f", "--file"}:
            index += 2
            continue
        if token.startswith("--file="):
            index += 1
            continue
        if token in {"-n", "-E", "-r", "-u", "-s"} or (
            token.startswith("-") and not _looks_like_sed_print_script(token)
        ):
            index += 1
            continue
        if not saw_script:
            if _looks_like_sed_search_script(token):
                return False
            saw_script = True
        else:
            has_file_arg = True
        index += 1
    return saw_script or has_file_arg


def _looks_like_git_show_file_read_tokens(tokens: list[str]) -> bool:
    if not tokens or Path(tokens[0]).name != "git" or "show" not in tokens:
        return False
    show_index = tokens.index("show")
    if any(token in {"-L", "--line-range"} for token in tokens[show_index + 1 :]):
        return False
    skip_next = False
    for token in tokens[show_index + 1 :]:
        if skip_next:
            skip_next = False
            continue
        if token == "--":
            continue
        if token in {"-l", "--format", "--pretty", "--date"}:
            skip_next = True
            continue
        if token.startswith(("-l", "--format=", "--pretty=", "--date=")):
            continue
        if token.startswith("-"):
            continue
        _, separator, path = token.partition(":")
        if separator and path:
            return True
    return False


def _is_apt_command(command: str) -> bool:
    for tokens in _command_segments_after_wrappers(command):
        if not tokens or Path(tokens[0]).name not in {"apt", "apt-get"}:
            continue
        rest = _skip_option_tokens(tokens[1:])
        if rest and rest[0] in {
            "update",
            "install",
            "upgrade",
            "dist-upgrade",
            "full-upgrade",
        }:
            return True
    return False


def _is_docker_build_command(command: str) -> bool:
    for tokens in _command_segments_after_wrappers(command):
        if not tokens or Path(tokens[0]).name != "docker":
            continue
        rest = _skip_option_tokens(tokens[1:])
        if not rest:
            continue
        if rest[0] == "build":
            return True
        if rest[0] == "buildx":
            buildx_rest = _skip_option_tokens(rest[1:])
            if buildx_rest and buildx_rest[0] in {"bake", "build"}:
                return True
            continue
        if rest[0] in {"builder", "image"}:
            nested_rest = _skip_option_tokens(rest[1:])
            if nested_rest and nested_rest[0] == "build":
                return True
            continue
        if rest[0] == "compose":
            compose_rest = _skip_option_tokens(rest[1:])
            if compose_rest and (
                compose_rest[0] == "build"
                or (compose_rest[0] in {"run", "up"} and "--build" in compose_rest)
            ):
                return True
            continue
    return False


def _is_docker_log_command(command: str) -> bool:
    for tokens in _command_segments_after_wrappers(command):
        if not tokens or Path(tokens[0]).name != "docker":
            continue
        rest = _skip_option_tokens(tokens[1:])
        if not rest:
            continue
        if rest[0] == "logs":
            return True
        if rest[0] == "container":
            container_rest = _skip_option_tokens(rest[1:])
            if container_rest and container_rest[0] == "logs":
                return True
            continue
        if rest[0] == "compose":
            compose_rest = _skip_option_tokens(rest[1:])
            if compose_rest and compose_rest[0] == "logs":
                return True
            continue
    return False


def _looks_like_docker_build_output(sample: str) -> bool:
    return bool(
        "failed to solve" in sample
        or "did not complete successfully" in sample
        or ("dockerfile" in sample and re.search(r"(?m)^(#\d+|=>)", sample))
    )


def _can_infer_docker_build_from_output(command: str) -> bool:
    for tokens in _command_segments_after_wrappers(command):
        if not tokens:
            continue
        command_name = Path(tokens[0]).name
        rest = _skip_option_tokens(tokens[1:])
        if command_name in {"make", "gmake", "just", "task", "ninja"}:
            return any(
                re.search(r"\b(build|image|docker|container)\b", token, re.IGNORECASE)
                for token in rest
            )
    return False



def _python_module_invocation(tokens: list[str]) -> tuple[str, list[str]] | None:
    if not tokens or not re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", Path(tokens[0]).name):
        return None
    index = 1
    value_options = {"-c", "-W", "-X"}
    while index < len(tokens):
        token = tokens[index]
        if token == "-m" and index + 1 < len(tokens):
            return tokens[index + 1], tokens[index + 2 :]
        if token in SHELL_SEPARATORS or not token.startswith("-"):
            return None
        option_name = token.split("=", 1)[0]
        index += 1
        if option_name in value_options and "=" not in token and index < len(tokens):
            index += 1
    return None


def _is_python_package_command(command: str) -> bool:
    for tokens in _command_segments_after_wrappers(command):
        if not tokens:
            continue
        token_name = Path(tokens[0]).name
        if token_name == "uv":
            rest = _skip_option_tokens(tokens[1:])
            if not rest:
                continue
            if rest[0] in {"sync", "add", "lock"}:
                return True
            if rest[0] == "pip":
                pip_rest = _skip_option_tokens(rest[1:])
                if pip_rest and pip_rest[0] in {"install", "sync"}:
                    return True
        if re.fullmatch(r"pip(?:\d+(?:\.\d+)?)?", token_name):
            rest = _skip_option_tokens(tokens[1:])
            if rest and rest[0] == "install":
                return True
        python_module = _python_module_invocation(tokens)
        if python_module is not None and python_module[0] == "pip":
            rest = _skip_option_tokens(python_module[1])
            if rest and rest[0] == "install":
                return True
    return False


def _is_node_command(command: str, sample: str) -> bool:
    command_names = [
        Path(tokens[0]).name for tokens in _command_segments_after_wrappers(command) if tokens
    ]
    if any(name in {"npm", "pnpm", "yarn"} for name in command_names):
        return True
    if any(name in {"node", "npx", "vitest", "jest", "tsx", "mocha"} for name in command_names):
        return True
    return "npm err!" in sample or "err_pnpm" in sample or "yarn run" in sample


def _is_source_search_command(command: str) -> bool:
    if not command:
        return False
    for tokens in _command_token_segments(command):
        if _is_setup_segment(tokens):
            continue
        if _tokens_start_source_search(tokens) or _tokens_pipe_to_source_search(tokens):
            return True
    return False


def _source_consumer_command_class(
    command: str,
    sample: str,
    text: str,
) -> str | None:
    for variant in _command_intent_variants(command):
        command_class = _background_tail_command_class(variant, sample, text)
        if command_class is not None:
            return command_class
    for variant in _command_intent_variants(command):
        command_class = _pipeline_xargs_compactable_class(variant, sample, text)
        if command_class is not None:
            return command_class
    return None


def _background_tail_command_class(command: str, sample: str, text: str) -> str | None:
    segments = _background_segments(command)
    for index, (separator, tokens) in enumerate(segments):
        if separator != "&" or not tokens:
            continue
        exact_class = _exact_class_for_tokens(tokens)
        if exact_class is not None:
            later_compactable_class = _later_compactable_class(segments[index + 1 :], sample, text)
            return later_compactable_class or exact_class
        compactable_class = _compactable_class_for_tokens(tokens, sample, text)
        if compactable_class is not None:
            return compactable_class
    return None


def _background_segments(command: str) -> list[tuple[str | None, list[str]]]:
    segments: list[tuple[str | None, list[str]]] = []
    separator: str | None = None
    current: list[str] = []
    for token in _shell_tokens(command):
        if token in {"&&", "||", ";", "&"}:
            if current:
                segments.append((separator, current))
                current = []
            separator = token
            continue
        current.append(token)
    if current:
        segments.append((separator, current))
    return segments


def _later_compactable_class(
    segments: list[tuple[str | None, list[str]]],
    sample: str,
    text: str,
) -> str | None:
    for _separator, tokens in segments:
        if not tokens or _is_setup_segment(tokens):
            continue
        command_class = _compactable_class_for_tokens(tokens, sample, text)
        if command_class is not None:
            return command_class
    return None


def _exact_class_for_tokens(tokens: list[str]) -> str | None:
    if _tokens_have_unsafe_shell_expansion(tokens):
        return None
    if _tokens_start_file_read(tokens):
        return "file_read"
    if _tokens_start_source_search(tokens) or _tokens_pipe_to_source_search(tokens):
        return "source_search"
    return None


def _tokens_have_unsafe_shell_expansion(tokens: list[str]) -> bool:
    return any(
        "<" in token or ">" in token or "`" in token or "$" in token for token in tokens
    )


def _pipeline_xargs_compactable_class(command: str, sample: str, text: str) -> str | None:
    for tokens in _command_token_segments(command):
        for index, token in enumerate(tokens[:-1]):
            if token != "|":
                continue
            upstream = tokens[:index]
            if not (_tokens_start_source_search(upstream) or _tokens_start_file_read(upstream)):
                continue
            downstream = _strip_command_wrappers(tokens[index + 1 :])
            payload = _command_runner_payload(downstream)
            if payload is None:
                continue
            command_class = _compactable_class_for_tokens(payload, sample, text)
            if command_class is not None:
                return command_class
    return None


def _compactable_class_for_tokens(
    tokens: list[str],
    sample: str,
    text: str,
) -> str | None:
    if not tokens:
        return None
    command = shlex.join(tokens)
    command_variants = _command_intent_variants(command)
    if any(_is_apt_command(variant) for variant in command_variants):
        return "apt"
    if any(_is_python_package_command(variant) for variant in command_variants):
        return "python_package"
    if any(
        _contains_command(variant, ("pytest", "py.test")) for variant in command_variants
    ) or "=== failures ===" in sample:
        return "pytest"
    if any("unittest" in variant for variant in command_variants) or re.search(
        r"ran \d+ tests?",
        sample,
    ):
        return "unittest"
    if any(_is_docker_log_command(variant) for variant in command_variants):
        return "docker_logs"
    if any(_is_docker_build_command(variant) for variant in command_variants) or (
        _looks_like_docker_build_output(text)
        and any(_can_infer_docker_build_from_output(variant) for variant in command_variants)
    ):
        return "docker_build"
    if any(_is_node_command(variant, sample) for variant in command_variants):
        return "node"
    return None


def _is_setup_segment(tokens: list[str]) -> bool:
    setup_commands = {".", "cd", "export", "popd", "pushd", "pwd", "set", "source", "true"}
    return bool(tokens and Path(tokens[0]).name in setup_commands)


def _tokens_start_source_search(tokens: list[str]) -> bool:
    tokens = _strip_command_wrappers(tokens)
    if tokens and Path(tokens[0]).name in {"bash", "sh", "zsh"}:
        shell_command = _shell_c_argument(tokens[1:])
        return _is_source_search_command(shell_command) if shell_command is not None else False
    while tokens and Path(tokens[0]).name in {"time", "gtime", "command"}:
        tokens = _skip_option_tokens(tokens[1:])
    runner_tokens = _command_runner_payload(tokens)
    if runner_tokens is not None:
        return _tokens_start_source_search(runner_tokens)
    if tokens and Path(tokens[0]).name == "git":
        rest = _skip_option_tokens(tokens[1:])
        return bool(rest and Path(rest[0]).name == "grep")
    if tokens and Path(tokens[0]).name == "xargs":
        rest = _skip_option_tokens(tokens[1:])
        return bool(rest and Path(rest[0]).name in SOURCE_SEARCH_COMMANDS)
    return bool(tokens and Path(tokens[0]).name in SOURCE_SEARCH_COMMANDS)


def _tokens_start_file_read(tokens: list[str]) -> bool:
    tokens = _strip_command_wrappers(tokens)
    if tokens and Path(tokens[0]).name in {"bash", "sh", "zsh"}:
        shell_command = _shell_c_argument(tokens[1:])
        return _looks_like_file_read_command(shell_command) if shell_command is not None else False
    while tokens and Path(tokens[0]).name in {"time", "gtime", "command"}:
        tokens = _skip_option_tokens(tokens[1:])
    runner_tokens = _command_runner_payload(tokens)
    if runner_tokens is not None:
        return _tokens_start_file_read(runner_tokens)
    if not tokens:
        return False
    token_name = Path(tokens[0]).name
    if token_name == "git":
        return _looks_like_git_show_file_read_tokens(tokens)
    if token_name == "sed":
        return _looks_like_sed_file_read_tokens(tokens)
    return token_name in SOURCE_READ_COMMANDS


def _tokens_start_compactable_command(tokens: list[str]) -> bool:
    tokens = _strip_command_wrappers(tokens)
    if not tokens:
        return False
    runner_tokens = _command_runner_payload(tokens)
    if runner_tokens is not None:
        return _tokens_start_compactable_command(runner_tokens)
    token_name = Path(tokens[0]).name
    if token_name in {"apt", "apt-get", "docker", "npm", "pnpm", "yarn"}:
        return True
    if token_name in {"uv", "pip", "pip3", "pytest", "py.test", "node"}:
        return True
    python_module = _python_module_invocation(tokens)
    return bool(python_module is not None and python_module[0] == "pip")


def _tokens_pipe_to_source_search(tokens: list[str]) -> bool:
    tokens = _strip_command_wrappers(tokens)
    for index, token in enumerate(tokens[:-1]):
        if token == "|":
            downstream = tokens[index + 1 :]
            if _tokens_start_source_search(downstream):
                upstream = tokens[:index]
                if _tokens_start_compactable_command(upstream):
                    continue
                return True
        if (
            Path(tokens[0]).name == "find"
            and token == "-exec"
            and Path(tokens[index + 1]).name in SOURCE_SEARCH_COMMANDS
        ):
            return True
    return False


def _command_runner_payload(tokens: list[str]) -> list[str] | None:
    if not tokens:
        return None
    command = Path(tokens[0]).name
    if command == "uvx":
        return _skip_option_tokens(tokens[1:])
    if command in {"uv", "poetry", "pipx"}:
        rest = _skip_option_tokens(tokens[1:])
        if rest and rest[0] == "run":
            return _skip_option_tokens(rest[1:])
        return None
    if command == "npx":
        shell_command = _shell_c_argument(tokens[1:])
        if shell_command is not None:
            return _shell_tokens(shell_command)
        return _skip_option_tokens(tokens[1:])
    if command == "xargs":
        return _xargs_payload_tokens(tokens)
    if command in {"npm", "pnpm", "yarn"}:
        rest = _skip_option_tokens(tokens[1:])
        if rest and rest[0] in {"dlx", "exec"}:
            shell_command = _shell_c_argument(rest[1:])
            if shell_command is not None:
                return _shell_tokens(shell_command)
            return _skip_option_tokens(rest[1:])
    return None


def _xargs_payload_tokens(tokens: list[str]) -> list[str]:
    value_options = {
        "-a",
        "-d",
        "-E",
        "-e",
        "-I",
        "-i",
        "-L",
        "-l",
        "-n",
        "-P",
        "-s",
        "--arg-file",
        "--delimiter",
        "--eof",
        "--max-args",
        "--max-chars",
        "--max-lines",
        "--max-procs",
        "--process-slot-var",
        "--replace",
    }
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in SHELL_SEPARATORS:
            return []
        if token == "--":
            return tokens[index + 1 :]
        if not token.startswith("-"):
            return tokens[index:]
        option_name = token.split("=", 1)[0]
        index += 1
        if option_name in value_options and "=" not in token and index < len(tokens):
            index += 1
    return []


def _command_intent_variants(command: str) -> tuple[str, ...]:
    if not command:
        return ("",)
    variants = [command]
    tokens = _shell_tokens(command)
    payload_tokens = _command_runner_payload(_strip_command_wrappers(tokens))
    if payload_tokens:
        payload_command = shlex.join(payload_tokens)
        if payload_command not in variants:
            variants.append(payload_command)
    shell_command = _shell_wrapped_command(tokens)
    if shell_command and shell_command not in variants:
        variants.append(shell_command)
    return tuple(variants)


def _strip_command_wrappers(tokens: list[str]) -> list[str]:
    tokens = _strip_assignment_tokens(tokens)
    while tokens:
        command = Path(tokens[0]).name
        command_name = Path(command).name
        if command_name == "sudo":
            tokens = _strip_assignment_tokens(
                _skip_wrapper_options(
                    tokens[1:],
                    value_options={
                        "-c",
                        "-d",
                        "-g",
                        "-p",
                        "-t",
                        "-u",
                        "--chdir",
                        "--group",
                        "--prompt",
                        "--user",
                    },
                )
            )
            continue
        if command_name == "env":
            tokens = _strip_assignment_tokens(
                _skip_wrapper_options(
                    tokens[1:],
                    value_options={
                        "-c",
                        "-s",
                        "-u",
                        "--chdir",
                        "--split-string",
                        "--unset",
                    },
                )
            )
            continue
        if command_name in {"time", "gtime", "command"}:
            tokens = _strip_assignment_tokens(
                _skip_wrapper_options(
                    tokens[1:],
                    value_options={"-f", "-o", "--format", "--output"},
                )
            )
            continue
        break
    return tokens


def _skip_wrapper_options(tokens: list[str], *, value_options: set[str]) -> list[str]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in SHELL_SEPARATORS:
            return []
        if not token.startswith("-"):
            break
        index += 1
        option_name = token.split("=", 1)[0]
        if option_name in value_options and "=" not in token and index < len(tokens):
            index += 1
    return tokens[index:]


def _shell_wrapped_command(tokens: list[str]) -> str | None:
    tokens = _strip_command_wrappers(tokens)
    if tokens and Path(tokens[0]).name in {"bash", "sh", "zsh"}:
        return _shell_c_argument(tokens[1:])
    return None


def _shell_c_argument(tokens: list[str]) -> str | None:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--call="):
            return token.split("=", 1)[1]
        has_short_c = (
            token.startswith("-") and not token.startswith("--") and token.endswith("c")
        )
        if (token in {"-c", "--call"} or has_short_c) and index + 1 < len(tokens):
            return tokens[index + 1]
        index += 1
    return None


def _strip_assignment_tokens(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens) and re.fullmatch(r"[a-z_][a-z0-9_]*=.*", tokens[index]):
        index += 1
    return tokens[index:]


def _command_token_segments(command: str) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in _shell_tokens(command):
        if token in {"&&", "||", ";", "&"}:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _command_execution_segments(command: str) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in _shell_tokens(command):
        if token in SHELL_SEPARATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _command_segments_after_wrappers(command: str) -> list[list[str]]:
    segments: list[list[str]] = []
    for variant in _command_intent_variants(command):
        for tokens in _command_execution_segments(variant):
            tokens = _strip_command_wrappers(tokens)
            if tokens and Path(tokens[0]).name in {"bash", "sh", "zsh"}:
                shell_command = _shell_c_argument(tokens[1:])
                if shell_command is not None:
                    segments.extend(_command_segments_after_wrappers(shell_command))
                continue
            while tokens and not _is_setup_segment(tokens):
                segments.append(tokens)
                payload_tokens = _command_runner_payload(tokens)
                if payload_tokens is None:
                    break
                tokens = _strip_command_wrappers(payload_tokens)
                if tokens and Path(tokens[0]).name in {"bash", "sh", "zsh"}:
                    shell_command = _shell_c_argument(tokens[1:])
                    if shell_command is not None:
                        segments.extend(_command_segments_after_wrappers(shell_command))
                    break
    return segments


def _skip_option_tokens(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in SHELL_SEPARATORS:
            return []
        if not token.startswith("-"):
            break
        index += 1
        option_name = token.split("=", 1)[0]
        if option_name in OPTIONS_WITH_VALUES and "=" not in token and index < len(tokens):
            index += 1
    return tokens[index:]


def _shell_tokens(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return command.split()


def _option_key(key: str) -> str:
    normalized = key.strip().lower().replace("-", "_")
    if normalized.startswith("noisegate_"):
        return normalized.removeprefix("noisegate_")
    return normalized


def _parse_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _parse_nonnegative_int(value: object, default: int) -> int:
    parsed = _parse_int(value, default)
    return parsed if parsed >= 0 else default


def _parse_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in TRUE_VALUES:
            return True
        if lowered in FALSE_VALUES:
            return False
    return None


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    parsed = _parse_bool(value)
    return default if parsed is None else parsed
