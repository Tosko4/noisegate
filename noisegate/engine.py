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
    command_l = (command or "").strip().lower()
    sample_l = text[:4_000].lower()

    if (
        _looks_like_diff_command(command_l)
        or "diff --git " in text
        or _looks_like_unified_diff(text)
    ):
        return "git_diff"
    if _looks_like_file_read_command(command_l):
        return "file_read"
    if "git status" in command_l or ("on branch " in sample_l and "working tree" in sample_l):
        return "git_status"
    if "git log" in command_l:
        return "git_log"
    if _contains_command(command_l, ("pytest", "py.test")) or "=== failures ===" in sample_l:
        return "pytest"
    if "unittest" in command_l or re.search(r"ran \d+ tests?", sample_l):
        return "unittest"
    if _is_node_command(command_l, sample_l):
        return "node"
    if "docker build" in command_l or "docker compose" in command_l or "dockerfile" in sample_l:
        return "docker_build"
    if _is_search_command(command_l):
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
        return NODE_PATTERNS + lcm_patterns
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
    if command_class == "node":
        return "node", _important_lines(text, options, NODE_PATTERNS + lcm_patterns)
    if command_class == "docker_build":
        return "docker_build", _important_lines(text, options, DOCKER_PATTERNS + lcm_patterns)
    if command_class == "git_status":
        return "git_status", _important_lines(text, options, GIT_STATUS_PATTERNS + lcm_patterns)
    if command_class == "git_log":
        return "git_log", _head_tail(text, replace(options, head_lines=40, tail_lines=8))
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
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bFAILED\b|\bERROR\b",
        r"^(failed|error)\s+",
        r"assertionerror|traceback|exception",
        r"^\s*E\s+",
        r"\d+\s+failed",
        r"\berror\b|\bfailed\b|\bfail\b",
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

DOCKER_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\berror\b|\bfailed\b|\bfail\b",
        r"dockerfile|unable to|denied|not found",
        r"^#\d+|^=>",
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
            budgeted = _line_budgeted_important_excerpt(lines, important, options)
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
        budgeted = _line_budgeted_important_excerpt(lines, important, options)
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
    lcm_patterns = _lcm_externalized_patterns_for(text)
    if lcm_patterns:
        return lcm_patterns + tuple(pattern for pattern in patterns if pattern not in lcm_patterns)
    return CRITICAL_PATTERNS if _first_pattern_match(text, CRITICAL_PATTERNS) else patterns


def _line_budgeted_important_excerpt(
    lines: list[str],
    important: list[int],
    options: NoisegateOptions,
) -> str | None:
    if not important:
        return None
    lcm_priority = _matching_indices(lines, important, LCM_EXTERNALIZED_PATTERNS)
    critical_priority = _matching_indices(lines, important, CRITICAL_PATTERNS)
    priority = _important_priority_indices(lines, important)
    priority = priority or important
    max_context = max(0, options.important_context_lines)

    # Externalized payload refs are recovery handles, not just interesting log
    # lines. If they appear alongside a failure cluster, keep the refs and at
    # least one failure anchor together when the line budget allows it. If the
    # budget is too tight for both, the fallback below prioritizes the refs so
    # the externalized-payload recovery path stays intact.
    multi_anchor_priority = lcm_priority + [
        index for index in critical_priority[:1] if index not in lcm_priority
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
        for context in range(max_context, -1, -1):
            start = max(0, anchor - context)
            end = min(len(lines), anchor + context + 1)
            candidate = _lines_with_surrounding_omission_markers(lines, start, end - 1)
            if _line_count(candidate) <= options.max_lines:
                return candidate
    return None


def _important_priority_indices(lines: list[str], indices: list[int]) -> list[int]:
    lcm_priority = _matching_indices(lines, indices, LCM_EXTERNALIZED_PATTERNS)
    critical_priority = _matching_indices(lines, indices, CRITICAL_PATTERNS)
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
    match = _first_pattern_match(text, patterns)
    if match is None:
        return _char_head_tail(text, options)
    line_excerpt = _line_centered_excerpt(text, options, match)
    if line_excerpt is not None:
        return line_excerpt
    return None


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
    match: re.Match[str],
) -> str | None:
    if "\n" not in text:
        return None
    lines = text.splitlines()
    offsets: list[tuple[int, int]] = []
    cursor = 0
    for line in lines:
        start = cursor
        end = start + len(line)
        offsets.append((start, end))
        cursor = end + 1

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

    def start(self) -> int:
        return self.start_index

    def end(self) -> int:
        return self.end_index


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


def _looks_like_file_read_command(command: str) -> bool:
    if not command or re.search(r"[|;&><]", command):
        return False
    return bool(
        re.match(r"^(cat|head|tail|less|more|bat)\b", command)
        or re.match(r"^sed\s+(-n\s+)?(['\"]?\d+,?\d*p['\"]?)\s+\S+", command)
    )


def _is_node_command(command: str, sample: str) -> bool:
    if re.search(r"(^|[\s;&|])(npm|pnpm|yarn)\s+", command):
        return True
    if re.search(r"(^|[\s;&|])(node|npx|vitest|jest|tsx|mocha)\b", command):
        return True
    return "npm err!" in sample or "err_pnpm" in sample or "yarn run" in sample


def _is_search_command(command: str) -> bool:
    return bool(re.search(r"(^|[\s;&|])(rg|grep|ag|ack)(\s|$)", command))


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
