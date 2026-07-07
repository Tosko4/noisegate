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
    command_raw = (command or "").strip()
    command_l = command_raw.lower()
    command_probe = _unwrap_shell_command(command_raw) or command_raw
    command_probe_l = command_probe.lower()
    sample_l = text[:4_000].lower()

    if _looks_like_file_read_command(command_raw):
        return "file_read"
    if _looks_like_v4a_patch(text):
        return "patch"
    if (
        _looks_like_diff_command(command_raw)
        or "diff --git " in text
        or _looks_like_unified_diff(text)
    ):
        return "git_diff"
    if "git status" in command_probe_l or ("on branch " in sample_l and "working tree" in sample_l):
        return "git_status"
    if "git log" in command_probe_l:
        return "git_log"
    os_package_command = _is_os_package_command(command_raw)
    python_package_command = _is_python_package_command(command_raw)
    node_command = _is_node_command(command_raw, sample_l)
    if os_package_command and _has_os_package_failure_signal(text):
        return "os_package"
    if python_package_command and _has_dependency_failure_signal(text):
        return "dependency_install"
    if node_command and _has_node_failure_signal(text):
        return "node"
    if _is_pytest_command(command_raw, sample_l):
        return "pytest"
    if "unittest" in command_l or re.search(r"ran \d+ tests?", sample_l):
        return "unittest"
    if os_package_command:
        return "os_package"
    if python_package_command:
        return "dependency_install"
    if node_command:
        return "node"
    if _is_docker_build_command(command_raw) or "dockerfile" in sample_l:
        return "docker_build"
    if _is_inventory_command(command_raw):
        return "inventory"
    if _is_search_command(command_raw):
        return "search"
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
        return CRITICAL_PATTERNS + lcm_patterns
    if command_class == "node":
        return NODE_PRESERVATION_PATTERNS + lcm_patterns
    if command_class == "os_package":
        return OS_PACKAGE_PATTERNS + CRITICAL_PATTERNS + lcm_patterns
    if command_class == "dependency_install":
        return DEPENDENCY_INSTALL_PATTERNS + CRITICAL_PATTERNS + lcm_patterns
    if command_class == "docker_build":
        return DOCKER_PATTERNS + CRITICAL_PATTERNS + lcm_patterns
    if command_class == "inventory":
        return INVENTORY_PATTERNS + CRITICAL_PATTERNS + lcm_patterns
    return lcm_patterns or None


def _apply_reducer(
    text: str,
    command_class: str,
    options: NoisegateOptions,
    exit_code: int | None,
) -> tuple[str, str | None]:
    del exit_code
    lcm_patterns = _lcm_externalized_patterns_for(text)
    if options.mode == "head_tail":
        if lcm_patterns:
            return "generic_head_tail", _important_lines(text, options, lcm_patterns)
        return "generic_head_tail", _head_tail(text, options)
    if command_class in {"pytest", "unittest"}:
        return command_class, _important_lines(text, options, TEST_PATTERNS + lcm_patterns)
    if command_class == "os_package":
        return "os_package", _important_lines(
            text,
            options,
            OS_PACKAGE_PATTERNS + CRITICAL_PATTERNS + lcm_patterns,
        )
    if command_class == "dependency_install":
        return "dependency_install", _important_lines(
            text,
            options,
            DEPENDENCY_INSTALL_PATTERNS + CRITICAL_PATTERNS + lcm_patterns,
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
            DOCKER_PATTERNS + CRITICAL_PATTERNS + lcm_patterns,
        )
    if command_class == "git_status":
        return "git_status", _important_lines(text, options, GIT_STATUS_PATTERNS + lcm_patterns)
    if command_class == "git_log":
        return "git_log", _head_tail(text, replace(options, head_lines=40, tail_lines=8))
    if command_class == "inventory":
        return "inventory", _important_lines(
            text,
            options,
            INVENTORY_PATTERNS + CRITICAL_PATTERNS + lcm_patterns,
        )
    if command_class == "search":
        if lcm_patterns:
            return "search", _important_lines(text, options, lcm_patterns)
        return "search", _head_tail(text, options)
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
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"assertionerror|traceback|exceptiongroup|baseexception|\bexception\b\s*:",
        r"\b[a-z0-9_]*error\b(?::|$)",
        r"\bunhandled\s+exception\b",
        r"\bexception\s+in\b",
        r"^\s*E\s+",
        r"^\s*E:",
        r"\d+\s+failed",
        r"^(failed|error)\s+",
        r"\bFAILED\b|\bERROR\b",
        r"\berror\b|\bfailed\b|\bfail\b",
        r"unable to locate package|could not get lock|gpg error|hash sum mismatch",
        r"dependency conflict|conflicting dependencies|resolutionimpossible",
        r"resolution failed|no solution found",
        r"npm err!|pnpm err!|yn\d{4}: error|err_pnpm|eresolve|elifecycle",
        r"failed to solve|dockerfile(?::| line)|exit code:|executor failed",
        r"permission denied|no such file|cannot access",
    )
)

OS_PACKAGE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"^err:\d+|^e:|^w:.*gpg error",
        r"unable to locate package|could not get lock|gpg error|hash sum mismatch|failed to fetch",
        r"does not have a release file|repository .* not signed|no_pubkey|404\s+not found",
        r"sub-process .* returned an error code|dpkg.*error|returned an error code",
        r"reading package lists\.\.\. done|fetched .* in .*|\d+ upgraded, .* newly installed",
        r"setting up \S+|processing triggers for",
    )
)

PYTHON_PACKAGE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"^error:|^error\b|resolutionimpossible|no solution found|resolution failed",
        r"dependency conflict|conflicting dependencies|failed to prepare distributions",
        r"could not find a version",
        r"no matching distribution|failed to build|because .* depends on|caused by:",
        r"successfully installed|resolved \d+ packages|installed \S+",
    )
)

DEPENDENCY_INSTALL_PATTERNS = PYTHON_PACKAGE_PATTERNS

NODE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"npm err!|pnpm err!|yarn.*error|yn\d{4}: error|err_pnpm|elifecycle|eresolve",
        r"\berror\b|\bfailed\b|\bfail\b|failed with errors|test failed",
        r"\bwarning\b|\bwarn\b",
        r"could not resolve|unable to resolve|peer dep|lockfile|e404|enotfound",
        r"added \d+ packages|found \d+ vulnerabilities|audited \d+ packages",
        r"tests?.*(failed|passed)|test suites?:",
    )
)

NODE_PRESERVATION_PATTERNS = (*NODE_PATTERNS, *CRITICAL_PATTERNS)

DOCKER_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"failed to solve",
        r"dockerfile(?::| line)",
        r"exit code:|executor failed|did not complete successfully",
        r"\berror\b|\bfailed\b|\bfail\b",
        r"unable to|denied|not found",
        r"^#\d+\s+error:|^=>\s+error:",
    )
)

INVENTORY_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"permission denied|no such file|cannot access|not found",
        r"^(find|ls|fd|tree):",
    )
)

COMMAND_CLASS_PRESERVE_PATTERNS = {
    "pytest": CRITICAL_PATTERNS,
    "unittest": CRITICAL_PATTERNS,
    "os_package": OS_PACKAGE_PATTERNS,
    "dependency_install": DEPENDENCY_INSTALL_PATTERNS,
    "node": NODE_PATTERNS,
    "docker_build": DOCKER_PATTERNS,
    "inventory": INVENTORY_PATTERNS,
}


def _preserve_patterns_for_command_class(command_class: str) -> tuple[re.Pattern[str], ...] | None:
    return COMMAND_CLASS_PRESERVE_PATTERNS.get(command_class)


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
                _preservation_patterns(text, patterns),
            )
        return _char_head_tail(text, options)

    important: list[int] = []
    for index, line in enumerate(lines):
        if any(pattern.search(line) for pattern in patterns):
            important.append(index)

    if len(important) > options.max_important_lines:
        priority = _important_priority_indices(lines, important)
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
            budgeted = _line_budgeted_important_excerpt(lines, important, options, patterns)
            if budgeted is not None:
                return budgeted
            return _head_tail(text, options)
        if len(text) > options.max_chars:
            return _char_head_tail_preserving_patterns(
                text,
                options,
                _preservation_patterns(text, patterns),
            )
        return _char_head_tail(text, options)
    selected = _lines_with_markers(lines, sorted(keep))
    if _line_count(selected) > options.max_lines:
        budgeted = _line_budgeted_important_excerpt(lines, important, options, patterns)
        if budgeted is None:
            return None
        selected = budgeted
    if len(selected) > options.max_chars:
        return _char_head_tail_preserving_patterns(
            selected,
            options,
            _preservation_patterns(selected, patterns),
        )
    return selected


def _preservation_patterns(
    text: str,
    patterns: tuple[re.Pattern[str], ...],
) -> tuple[re.Pattern[str], ...]:
    base_patterns = patterns
    if patterns in {NODE_PATTERNS, NODE_PRESERVATION_PATTERNS}:
        if _first_pattern_match(text, CRITICAL_PATTERNS):
            base_patterns = NODE_PRESERVATION_PATTERNS
    elif _first_pattern_match(text, CRITICAL_PATTERNS):
        base_patterns = CRITICAL_PATTERNS

    lcm_patterns = _lcm_externalized_patterns_for(text)
    if lcm_patterns:
        return lcm_patterns + tuple(
            pattern for pattern in base_patterns if pattern not in lcm_patterns
        )
    return base_patterns


def _line_budgeted_important_excerpt(
    lines: list[str],
    important: list[int],
    options: NoisegateOptions,
    patterns: tuple[re.Pattern[str], ...],
) -> str | None:
    if not important:
        return None
    lcm_priority = _matching_indices(lines, important, LCM_EXTERNALIZED_PATTERNS)
    critical_priority = _matching_indices(lines, important, CRITICAL_PATTERNS)
    ranked_priority = sorted(
        critical_priority or important,
        key=lambda index: (_failure_detail_rank(lines[index]), index),
    )
    priority = lcm_priority + [index for index in ranked_priority if index not in lcm_priority]
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
    if _prefers_previous_traceback_context(line):
        return -1
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


def _prefers_previous_traceback_context(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith("E "):
        return False
    return bool(
        re.search(
            r"^(?:[a-z_][a-z0-9_]*\.)*[a-z_]*[a-z0-9_]*(?:error|exception)\b(?::|$)",
            stripped,
            re.IGNORECASE,
        )
    )


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


def _important_priority_indices(lines: list[str], indices: list[int]) -> list[int]:
    lcm_priority = _matching_indices(lines, indices, LCM_EXTERNALIZED_PATTERNS)
    critical_priority = sorted(
        _matching_indices(lines, indices, CRITICAL_PATTERNS),
        key=lambda index: (_failure_detail_rank(lines[index]), index),
    )
    return lcm_priority + [index for index in critical_priority if index not in lcm_priority]


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
    prefer_previous = _prefers_previous_traceback_context(line)
    while True:
        expanded = False
        if prefer_previous and start_line > 0:
            candidate = "\n".join(lines[start_line - 1 : end_line + 1])
            if _fits_budget(candidate, options):
                start_line -= 1
                expanded = True
        if end_line + 1 < len(lines):
            candidate = "\n".join(lines[start_line : end_line + 2])
            if _fits_budget(candidate, options):
                end_line += 1
                expanded = True
        if not prefer_previous and start_line > 0:
            candidate = "\n".join(lines[start_line - 1 : end_line + 1])
            if _fits_budget(candidate, options):
                start_line -= 1
                expanded = True
        if not expanded:
            break

    while True:
        candidate = _lines_with_surrounding_omission_markers(lines, start_line, end_line)
        if _fits_budget(candidate, options):
            return candidate
        if start_line == match_line and end_line == match_line:
            return None
        if prefer_previous:
            if end_line > match_line:
                end_line -= 1
            elif start_line < match_line:
                start_line += 1
            else:
                return None
        elif end_line > match_line and (end_line - match_line) >= (match_line - start_line):
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
    match = _first_pattern_match(before, preserve_patterns)
    if match is None:
        return False
    line_start = before.rfind("\n", 0, match.start()) + 1
    line_end = before.find("\n", match.end())
    if line_end == -1:
        line_end = len(before)
    preserved_line = before[line_start:line_end]
    return preserved_line not in after


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
    pattern_order_first = patterns is NODE_PATTERNS
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
    for tokens in _shell_command_tokens(command):
        if not tokens:
            continue
        command_name = _command_basename(tokens[0]).lower()
        if command_name == "diff":
            return True
        if command_name != "git":
            continue
        index = 1
        while index + 1 < len(tokens) and tokens[index].lower() == "-c":
            index += 2
        if index >= len(tokens):
            continue
        subcommand = tokens[index].lower()
        args = [token.lower() for token in tokens[index + 1 :]]
        if subcommand == "diff":
            return True
        if subcommand == "log" and any(arg in {"-p", "--patch"} for arg in args):
            return True
        if subcommand == "show":
            excluded = {"--no-patch", "--stat", "--summary", "--name-only", "--name-status"}
            return not any(arg.split("=", 1)[0] in excluded for arg in args)
    command_l = command.lower()
    if bool(re.search(r"(^|[\s;&|])git\b.*\bdiff\b", command_l)) or command_l.startswith("diff "):
        return True
    if re.search(r"(^|[\s;&|])git\b.*\blog\b.*?(\s-p\b|\s--patch\b)", command_l):
        return True
    return bool(re.search(r"(^|[\s;&|])git\b.*\bshow\b", command_l)) and not bool(
        re.search(
            r"\s--(no-patch|stat|summary|name-only|name-status)\b",
            command_l,
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
    command = _strip_safe_fd_redirections(command)
    if (
        not command
        or _has_unquoted_shell_operator(command)
        or _has_unsafe_unwrapped_shell_payload(command)
    ):
        return False
    saw_file_read = False
    for tokens in _shell_command_tokens(command):
        if not tokens:
            continue
        command_name = _command_basename(tokens[0]).lower()
        if command_name == "cd":
            continue
        if command_name in {"cat", "head", "tail", "less", "more", "bat", "nl"}:
            saw_file_read = True
            continue
        if _looks_like_find_exec_file_read_tokens(tokens):
            saw_file_read = True
            continue
        if _looks_like_fd_exec_file_read_tokens(tokens):
            saw_file_read = True
            continue
        if _looks_like_git_show_file_read_tokens(tokens):
            saw_file_read = True
            continue
        if command_name != "sed":
            return False
        if "-i" in tokens or any(token.startswith("-i") and token != "-i" for token in tokens):
            return False
        if any(_looks_like_sed_print_script(token) for token in tokens[1:]):
            saw_file_read = True
            continue
        return False
    return saw_file_read


def _strip_safe_fd_redirections(command: str) -> str:
    command = re.sub(r"(?<!\S)(?:\d?>&[12])(?=\s|$)", "", command)
    command = re.sub(r"(?<!\S)\d?>\s*/dev/null(?=\s|$)", "", command)
    command = re.sub(r"(?<!\S)<\s+(?!<\()\S+(?=\s|$)", "", command)
    return re.sub(r"(?<!\S)\d?<(?![<(])\S+(?=\s|$)", "", command)


def _is_file_display_command_name(command_name: str) -> bool:
    return command_name in {"cat", "head", "tail", "less", "more", "bat", "nl"}


def _looks_like_file_display_tokens(tokens: list[str]) -> bool:
    if not tokens:
        return False
    command_name = _command_basename(tokens[0]).lower()
    if _is_file_display_command_name(command_name):
        return True
    if command_name != "sed":
        return False
    if "-i" in tokens or any(token.startswith("-i") and token != "-i" for token in tokens):
        return False
    return any(_looks_like_sed_print_script(token) for token in tokens[1:])


def _looks_like_find_exec_file_read_tokens(tokens: list[str]) -> bool:
    if not tokens or _command_basename(tokens[0]).lower() != "find":
        return False
    for index, token in enumerate(tokens[:-1]):
        if token not in {"-exec", "-execdir"}:
            continue
        return _looks_like_file_display_tokens(tokens[index + 1 :])
    return False


def _looks_like_fd_exec_file_read_tokens(tokens: list[str]) -> bool:
    if not tokens or _command_basename(tokens[0]).lower() != "fd":
        return False
    for index, token in enumerate(tokens[:-1]):
        if token not in {"-x", "--exec", "-X", "--exec-batch"}:
            continue
        return _looks_like_file_display_tokens(tokens[index + 1 :])
    return False


def _has_unquoted_shell_operator(command: str) -> bool:
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
        if (
            char == "&"
            and command[index - 1 : index] != "&"
            and command[index + 1 : index + 2] != "&"
        ):
            return True
    return False


def _looks_like_sed_print_script(token: str) -> bool:
    script = token.strip()
    return bool(
        re.fullmatch(r"\d+(,\d+)?p", script)
        or re.fullmatch(r"\d+,\$p", script)
        or re.fullmatch(r"\$p", script)
    )


def _looks_like_git_show_file_read_tokens(tokens: list[str]) -> bool:
    if not tokens or _command_basename(tokens[0]).lower() != "git" or "show" not in tokens:
        return False
    show_index = tokens.index("show")
    skip_next = False
    for token in tokens[show_index + 1 :]:
        if skip_next:
            skip_next = False
            continue
        if token == "--":
            continue
        if token in {"-l", "-L", "--format", "--pretty", "--date"}:
            skip_next = True
            continue
        if token.startswith(("-l", "-L", "--format=", "--pretty=", "--date=")):
            continue
        if token.startswith("-"):
            continue
        _, separator, file_path = token.partition(":")
        if separator and file_path:
            return True
    return False


_APT_OPERATIONS = {
    "install",
    "update",
    "upgrade",
    "remove",
    "purge",
    "autoremove",
    "dist-upgrade",
    "full-upgrade",
}
_APT_VALUE_OPTIONS = {
    "--target-release",
    "-c",
    "-o",
    "-t",
    "--config-file",
    "--option",
}
_PIP_VALUE_OPTIONS = {
    "--cache-dir",
    "--cert",
    "--client-cert",
    "--config-settings",
    "--constraint",
    "--exists-action",
    "--extra-index-url",
    "--find-links",
    "--global-option",
    "--index-url",
    "--keyring-provider",
    "--log",
    "--platform",
    "--prefix",
    "--progress-bar",
    "--proxy",
    "--python",
    "--report",
    "--requirement",
    "--retries",
    "--root",
    "--src",
    "--target",
    "--timeout",
    "--trusted-host",
}
_UV_GLOBAL_VALUE_OPTIONS = {
    "--cache-dir",
    "--color",
    "--config-file",
    "--config-setting",
    "--directory",
    "--index-url",
    "--keyring-provider",
    "--link-mode",
    "--project",
    "--python",
    "--resolution",
    "--settings",
}
_UV_RUN_VALUE_OPTIONS = _UV_GLOBAL_VALUE_OPTIONS | {
    "--allow-insecure-host",
    "--config-setting",
    "--config-settings-package",
    "--default-index",
    "--env-file",
    "--exclude-newer",
    "--exclude-newer-package",
    "--extra",
    "--extra-index-url",
    "--find-links",
    "--fork-strategy",
    "--group",
    "--index",
    "--index-strategy",
    "--index-url",
    "--keyring-provider",
    "--link-mode",
    "--no-binary-package",
    "--no-build-package",
    "--no-extra",
    "--no-group",
    "--no-sources-package",
    "--only-group",
    "--package",
    "--prerelease",
    "--python-platform",
    "--refresh-package",
    "--reinstall-package",
    "--resolution",
    "--upgrade-group",
    "--upgrade-package",
    "--with",
    "--with-editable",
    "--with-requirements",
    "-C",
    "-P",
    "-f",
    "-i",
    "-p",
    "-s",
    "-w",
}
_UV_PIP_VALUE_OPTIONS = _PIP_VALUE_OPTIONS | _UV_GLOBAL_VALUE_OPTIONS | {
    "--exclude-newer",
    "--find-links",
    "--index-strategy",
    "--prerelease",
    "--reinstall-package",
}
_DOCKER_GLOBAL_VALUE_OPTIONS = {
    "--config",
    "--context",
    "--host",
    "--log-level",
    "--tlscacert",
    "--tlscert",
    "--tlskey",
    "-c",
    "-H",
}
_DOCKER_COMPOSE_VALUE_OPTIONS = {
    "--ansi",
    "--env-file",
    "--file",
    "--parallel",
    "--progress",
    "--profile",
    "--project-directory",
    "--project-name",
    "-f",
    "-p",
}
_COVERAGE_RUN_VALUE_OPTIONS = {
    "--branch",
    "--concurrency",
    "--context",
    "--data-file",
    "--debug",
    "--include",
    "--omit",
    "--pylib",
    "--rcfile",
    "--source",
}
_PYTHON_VALUE_OPTIONS = {"-W", "-X"}
_ENV_VALUE_OPTIONS = {"-a", "-C", "-u", "--argv0", "--chdir", "--unset"}
_SUDO_VALUE_OPTIONS = {
    "-C",
    "-D",
    "-g",
    "-h",
    "-p",
    "-r",
    "-t",
    "-T",
    "-u",
    "--close-from",
    "--command-timeout",
    "--chdir",
    "--group",
    "--host",
    "--prompt",
    "--role",
    "--type",
    "--user",
}
_SHELL_LONG_VALUE_OPTIONS = {"--init-file", "--rcfile"}
_PYTHON_RUNNER_COMMANDS = {"poetry", "pipenv", "pdm", "hatch", "rye"}
_PYTHON_RUNNER_VALUE_OPTIONS = {
    "--directory",
    "--env",
    "--env-file",
    "--name",
    "--project",
    "--python",
    "--pyproject",
    "-C",
    "-e",
    "-n",
    "-p",
}
_SHELL_COMMAND_SEPARATORS = {"&&", "||", ";", "|", "&", "|&"}


def _looks_like_assignment(token: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token))


def _command_basename(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1]


def _normalize_command_tokens(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens):
        start_index = index
        while index < len(tokens) and _looks_like_assignment(tokens[index]):
            index += 1
        if index < len(tokens) and _command_basename(tokens[index]).lower() == "env":
            index = _skip_options(tokens, index + 1, _ENV_VALUE_OPTIONS)
            continue
        if index < len(tokens):
            wrapper_name = _command_basename(tokens[index]).lower()
            if wrapper_name in {"sudo", "doas"}:
                index = _skip_sudo_options(tokens, index + 1)
                continue
            if wrapper_name == "command":
                index = _skip_command_options(tokens, index + 1)
                continue
        if index == start_index:
            break
    return tokens[index:]


def _split_shell_tokens(command: str) -> list[str]:
    command = command.replace("\r\n", ";").replace("\n", ";").replace("\r", ";")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return command.split()


def _unwrap_shell_tokens(tokens: list[str]) -> str | None:
    if not tokens or _command_basename(tokens[0]).lower() not in {"bash", "sh", "zsh"}:
        return None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-") or token == "-":
            return None
        if token == "--":
            index += 1
            continue
        if token == "-c":
            return tokens[index + 1] if index + 1 < len(tokens) else None
        if token.startswith("--"):
            option = token.split("=", 1)[0]
            index += 1
            if option in _SHELL_LONG_VALUE_OPTIONS and "=" not in token and index < len(tokens):
                index += 1
            continue
        flags = token[1:]
        if "c" in flags:
            return tokens[index + 1] if index + 1 < len(tokens) else None
        index += 1
        if ("o" in flags or "O" in flags) and index < len(tokens):
            index += 1
    return None


def _unwrap_shell_command(command: str) -> str | None:
    return _unwrap_shell_tokens(_split_shell_tokens(command))


def _shell_command_tokens(command: str) -> list[list[str]]:
    if not command:
        return []
    tokens = _split_shell_tokens(command)
    unwrapped = _unwrap_shell_tokens(tokens)
    if unwrapped is not None:
        return _shell_command_tokens(unwrapped)
    commands: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_COMMAND_SEPARATORS:
            normalized = _normalize_command_tokens(current)
            unwrapped_current = _unwrap_shell_tokens(normalized)
            if unwrapped_current is not None:
                commands.extend(_shell_command_tokens(unwrapped_current))
            elif normalized:
                commands.append(normalized)
            current = []
            continue
        current.append(token)
    normalized = _normalize_command_tokens(current)
    unwrapped_current = _unwrap_shell_tokens(normalized)
    if unwrapped_current is not None:
        commands.extend(_shell_command_tokens(unwrapped_current))
    elif normalized:
        commands.append(normalized)
    return commands


def _has_unsafe_unwrapped_shell_payload(command: str) -> bool:
    if not command:
        return False
    tokens = _split_shell_tokens(command)
    unwrapped = _unwrap_shell_tokens(tokens)
    if unwrapped is not None:
        return _has_unquoted_shell_operator(unwrapped) or _has_unsafe_unwrapped_shell_payload(
            unwrapped
        )
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_COMMAND_SEPARATORS:
            if _normalized_command_has_unsafe_unwrapped_shell_payload(current):
                return True
            current = []
            continue
        current.append(token)
    return _normalized_command_has_unsafe_unwrapped_shell_payload(current)


def _normalized_command_has_unsafe_unwrapped_shell_payload(tokens: list[str]) -> bool:
    normalized = _normalize_command_tokens(tokens)
    unwrapped = _unwrap_shell_tokens(normalized)
    return unwrapped is not None and (
        _has_unquoted_shell_operator(unwrapped)
        or _has_unsafe_unwrapped_shell_payload(unwrapped)
    )


def _skip_options(tokens: list[str], start: int, value_options: set[str]) -> int:
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return index + 1
        if not token.startswith("-") or token == "-":
            return index
        option = token.split("=", 1)[0]
        index += 1
        if option in value_options and "=" not in token and index < len(tokens):
            index += 1
    return index


def _skip_sudo_options(tokens: list[str], start: int) -> int:
    value_short_options = {"C", "D", "g", "h", "p", "r", "t", "T", "u"}
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return index + 1
        if not token.startswith("-") or token == "-":
            return index
        option = token.split("=", 1)[0]
        index += 1
        if option in _SUDO_VALUE_OPTIONS and "=" not in token and index < len(tokens):
            index += 1
            continue
        if option.startswith("--"):
            continue
        flags = option[1:]
        for position, flag in enumerate(flags):
            if flag not in value_short_options:
                continue
            if position == len(flags) - 1 and index < len(tokens):
                index += 1
            break
    return index


def _skip_command_options(tokens: list[str], start: int) -> int:
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return index + 1
        if not token.startswith("-") or token == "-":
            return index
        index += 1
    return index


def _python_module_index(tokens: list[str], python_index: int) -> int | None:
    index = python_index + 1
    while index < len(tokens):
        token = tokens[index]
        if token == "-m":
            return index + 1 if index + 1 < len(tokens) else None
        if token.startswith("-m") and len(token) > 2:
            return index
        if not token.startswith("-") or token == "-":
            return None
        option = token.split("=", 1)[0]
        index += 1
        if option in _PYTHON_VALUE_OPTIONS and "=" not in token and index < len(tokens):
            index += 1
    return None


def _python_module_name(tokens: list[str], module_index: int) -> str:
    token = tokens[module_index]
    if token.startswith("-m") and len(token) > 2:
        return token[2:]
    return token


def _is_python_launcher(token: str) -> bool:
    executable = _command_basename(token).lower()
    return executable == "py" or re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable) is not None


def _runner_payload_tokens(tokens: list[str]) -> list[str] | None:
    if not tokens or _command_basename(tokens[0]).lower() not in _PYTHON_RUNNER_COMMANDS:
        return None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "run" and index + 1 < len(tokens):
            payload = tokens[index + 1 :]
            if payload and payload[0] == "--":
                payload = payload[1:]
            return payload or None
        if token == "--":
            index += 1
            continue
        if not token.startswith("-") or token == "-":
            return None
        option = token.split("=", 1)[0]
        index += 1
        if option in _PYTHON_RUNNER_VALUE_OPTIONS and "=" not in token and index < len(tokens):
            index += 1
    return None


def _coverage_payload_tokens(tokens: list[str]) -> list[str] | None:
    if not tokens or _command_basename(tokens[0]).lower() != "coverage":
        return None
    index = _skip_options(tokens, 1, set())
    if index >= len(tokens) or tokens[index].lower() != "run":
        return None
    payload_index = _skip_options(tokens, index + 1, _COVERAGE_RUN_VALUE_OPTIONS)
    return tokens[payload_index:] or None


def _is_pytest_tokens(tokens: list[str]) -> bool:
    if not tokens:
        return False
    command_name = _command_basename(tokens[0]).lower()
    if command_name in {"pytest", "py.test"}:
        return True
    if _is_python_launcher(tokens[0]):
        module_index = _python_module_index(tokens, 0)
        return module_index is not None and _python_module_name(tokens, module_index).lower() in {
            "pytest",
            "py.test",
        }
    if command_name == "uv":
        index = _skip_options(tokens, 1, _UV_GLOBAL_VALUE_OPTIONS)
        if index + 1 < len(tokens) and tokens[index].lower() == "run":
            index = _skip_options(tokens, index + 1, _UV_RUN_VALUE_OPTIONS)
            if (
                index < len(tokens)
                and _command_basename(tokens[index]).lower() in {"pytest", "py.test"}
            ):
                return True
            if index < len(tokens) and _is_python_launcher(tokens[index]):
                module_index = _python_module_index(tokens, index)
                return module_index is not None and _python_module_name(
                    tokens,
                    module_index,
                ).lower() in {
                    "pytest",
                    "py.test",
                }
    runner_payload = _runner_payload_tokens(tokens)
    if runner_payload and _is_pytest_tokens(runner_payload):
        return True
    coverage_payload = _coverage_payload_tokens(tokens)
    return _is_pytest_tokens(coverage_payload) if coverage_payload else False


def _is_pytest_command(command: str, sample: str) -> bool:
    if "=== failures ===" in sample:
        return True
    return any(_is_pytest_tokens(tokens) for tokens in _shell_command_tokens(command))


def _has_os_package_failure_signal(sample: str) -> bool:
    return bool(
        re.search(
            r"^\s*[ew]:|unable to locate package|could not get lock|gpg error|"
            r"hash sum mismatch|failed to fetch|not signed|no_pubkey|"
            r"sub-process .* returned an error code|dpkg.*error|404\s+not found",
            sample,
            re.IGNORECASE | re.MULTILINE,
        )
    )


def _has_dependency_failure_signal(sample: str) -> bool:
    return bool(
        re.search(
            r"^\s*error:|resolutionimpossible|no solution found|resolution failed|"
            r"dependency conflict|conflicting dependencies|failed to prepare distributions|"
            r"could not find a version|no matching distribution|failed to build|caused by:",
            sample,
            re.IGNORECASE | re.MULTILINE,
        )
    )


def _has_node_failure_signal(sample: str) -> bool:
    return bool(
        re.search(
            r"npm err!|pnpm err!|yn\d{4}: error|err_pnpm|eresolve|elifecycle|"
            r"failed with errors|test failed|could not resolve|unable to resolve",
            sample,
            re.IGNORECASE | re.MULTILINE,
        )
    )


def _is_os_package_command(command: str) -> bool:
    for tokens in _shell_command_tokens(command):
        if _is_search_tokens(tokens):
            continue
        for index, token in enumerate(tokens):
            command_name = _command_basename(token).lower()
            if command_name not in {"apt", "apt-get"}:
                continue
            subcommand_index = _skip_options(tokens, index + 1, _APT_VALUE_OPTIONS)
            if (
                subcommand_index < len(tokens)
                and tokens[subcommand_index].lower() in _APT_OPERATIONS
            ):
                return True
    return False


def _is_python_package_command(command: str) -> bool:
    for tokens in _shell_command_tokens(command):
        if _is_search_tokens(tokens) or _is_pytest_tokens(tokens):
            continue
        for index, token in enumerate(tokens):
            command_name = _command_basename(token).lower()
            if command_name == "uv":
                subcommand_index = _skip_options(tokens, index + 1, _UV_GLOBAL_VALUE_OPTIONS)
                if subcommand_index >= len(tokens):
                    continue
                subcommand = tokens[subcommand_index].lower()
                if subcommand in {"sync", "add", "lock"}:
                    return True
                if subcommand == "pip":
                    pip_index = _skip_options(
                        tokens,
                        subcommand_index + 1,
                        _UV_PIP_VALUE_OPTIONS,
                    )
                    if pip_index < len(tokens) and tokens[pip_index].lower() == "install":
                        return True
            if command_name in {"pip", "pip3"} or re.fullmatch(r"pip\d+(?:\.\d+)?", command_name):
                install_index = _skip_options(tokens, index + 1, _PIP_VALUE_OPTIONS)
                if install_index < len(tokens) and tokens[install_index].lower() == "install":
                    return True
            if _is_python_launcher(token):
                module_index = _python_module_index(tokens, index)
                if (
                    module_index is not None
                    and _python_module_name(tokens, module_index).lower() == "pip"
                ):
                    install_index = _skip_options(tokens, module_index + 1, _PIP_VALUE_OPTIONS)
                    if install_index < len(tokens) and tokens[install_index].lower() == "install":
                        return True
    return False


def _is_node_command(command: str, sample: str) -> bool:
    for tokens in _shell_command_tokens(command):
        if not tokens:
            continue
        command_name = _command_basename(tokens[0]).lower()
        if command_name in {"npm", "pnpm", "yarn"}:
            return True
        if command_name in {"node", "npx", "vitest", "jest", "tsx", "mocha"}:
            return True
    return "npm err!" in sample or "err_pnpm" in sample or "yarn run" in sample


def _is_docker_build_command(command: str) -> bool:
    for tokens in _shell_command_tokens(command):
        if not tokens or _command_basename(tokens[0]).lower() != "docker":
            continue
        index = _skip_options(tokens, 1, _DOCKER_GLOBAL_VALUE_OPTIONS)
        if index >= len(tokens):
            continue
        subcommand = tokens[index].lower()
        if subcommand == "build":
            return True
        if [token.lower() for token in tokens[index : index + 2]] in (
            ["buildx", "build"],
            ["image", "build"],
        ):
            return True
        if subcommand == "compose":
            compose_index = _skip_options(
                tokens,
                index + 1,
                _DOCKER_COMPOSE_VALUE_OPTIONS,
            )
            if compose_index >= len(tokens):
                continue
            compose_command = tokens[compose_index].lower()
            if compose_command == "build":
                return True
            if compose_command == "up" and any(
                token == "--build" or token.startswith("--build=")
                for token in tokens[compose_index + 1 :]
            ):
                return True
    return False


def _is_inventory_command(command: str) -> bool:
    for tokens in _shell_command_tokens(command):
        if not tokens:
            continue
        command_name = _command_basename(tokens[0]).lower()
        if command_name == "ls":
            return any(
                token.startswith("-") and ("R" in token or "r" in token)
                for token in tokens[1:]
            )
        if command_name == "tree":
            return True
        if command_name == "fd":
            return not any(token in {"-x", "--exec", "-X", "--exec-batch"} for token in tokens[1:])
        if command_name == "git":
            index = 1
            while index + 1 < len(tokens) and tokens[index].lower() == "-c":
                index += 2
            if index < len(tokens) and tokens[index].lower() == "ls-files":
                return True
        if command_name == "find" and not {"-exec", "-execdir"}.intersection(tokens):
            return True
    return False


def _is_search_tokens(tokens: list[str]) -> bool:
    return bool(tokens) and _command_basename(tokens[0]).lower() in {"rg", "grep", "ag", "ack"}


def _is_search_command(command: str) -> bool:
    return any(_is_search_tokens(tokens) for tokens in _shell_command_tokens(command))


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
