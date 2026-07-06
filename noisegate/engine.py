from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from ._version import __version__
from .artifacts import DEFAULT_SIZE_CAP, ArtifactError, ArtifactStore, ArtifactTooLarge

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
BYPASS_MARKERS = (
    "NOISEGATE_BYPASS",
    "NOISEGATE_RAW",
    "[noisegate:bypass]",
    "[noisegate:raw]",
    "[noisegate bypass]",
)
PROTECTED_TOOL_NAMES = frozenset(
    {
        "read_file",
        "read_files",
        "read_text_file",
        "skill_view",
        "write_file",
        "patch",
        "apply_patch",
        "edit_file",
        "replace_in_file",
    }
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
        env_mode = os.environ.get("NOISEGATE")
        if env_mode is not None and env_mode.strip().lower() in FALSE_VALUES | {"off"}:
            enabled = False
        if _env_flag("NOISEGATE_DISABLE", default=False):
            enabled = False

        artifact_dir = os.environ.get("NOISEGATE_ARTIFACT_DIR")
        options = cls(
            enabled=enabled,
            artifact_enabled=_env_flag("NOISEGATE_ARTIFACTS", default=False),
            artifact_dir=Path(artifact_dir).expanduser() if artifact_dir else None,
            artifact_size_cap=_parse_int(
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
    if _has_bypass_marker(text) or _has_bypass_marker(command or ""):
        return _unchanged(text, "bypass", command_class, reason="bypass_marker")
    if tool_name in PROTECTED_TOOL_NAMES:
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
    if compacted is not None:
        compacted = _enforce_final_budget(compacted, options)
    if compacted is None or compacted == text or len(compacted) >= len(text):
        return _unchanged(text, "no_gain", command_class, reason="no_gain")

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
    )
    if len(compacted) >= len(text):
        return _unchanged(text, "no_gain", command_class, reason="no_gain_after_notices")
    if options.artifact_enabled:
        planned_artifact = metadata.get("artifact")
        if isinstance(planned_artifact, dict) and planned_artifact.get("stored") is True:
            metadata["artifact"] = _store_artifact(text, options)
            compacted = _append_recovery_notices(
                compacted_body,
                metadata,
                artifact_dir=options.artifact_dir,
                options=options,
            )
            if len(compacted) >= len(text):
                return _unchanged(
                    text,
                    "no_gain",
                    command_class,
                    reason="no_gain_after_artifact_store",
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


def _apply_reducer(
    text: str,
    command_class: str,
    options: NoisegateOptions,
    exit_code: int | None,
) -> tuple[str, str | None]:
    del exit_code
    if options.mode == "head_tail":
        return "generic_head_tail", _head_tail(text, options)
    if command_class in {"pytest", "unittest"}:
        return command_class, _important_lines(text, options, TEST_PATTERNS)
    if command_class == "node":
        return "node", _important_lines(text, options, NODE_PATTERNS)
    if command_class == "docker_build":
        return "docker_build", _important_lines(text, options, DOCKER_PATTERNS)
    if command_class == "git_status":
        return "git_status", _important_lines(text, options, GIT_STATUS_PATTERNS)
    if command_class == "git_log":
        return "git_log", _head_tail(text, replace(options, head_lines=40, tail_lines=8))
    if command_class == "search":
        return "search", _head_tail(text, options)
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
        return _char_head_tail(text, options)

    important: list[int] = []
    for index, line in enumerate(lines):
        if any(pattern.search(line) for pattern in patterns):
            important.append(index)

    if len(important) > options.max_important_lines:
        critical = [
            index
            for index in important
            if any(pattern.search(lines[index]) for pattern in CRITICAL_PATTERNS)
        ]
        if critical:
            important = _trim_indices_around_priority(
                important,
                critical,
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
            return _head_tail(text, options)
        return _char_head_tail(text, options)
    selected = _lines_with_markers(lines, sorted(keep))
    if len(selected) > options.max_chars:
        preserved = _char_head_tail_preserving_patterns(selected, options, CRITICAL_PATTERNS)
        if preserved is not None:
            return preserved
    return selected


def _trim_indices_around_priority(
    indices: list[int],
    priority: list[int],
    limit: int,
) -> list[int]:
    if limit <= 0:
        return []
    selected: list[int] = []
    for index in priority:
        if index not in selected:
            selected.append(index)
        if len(selected) >= limit:
            return sorted(selected)

    remaining = [index for index in indices if index not in selected]
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


def _enforce_final_budget(text: str, options: NoisegateOptions) -> str:
    compacted = text
    if _line_count(compacted) > options.max_lines:
        line_capped = _head_tail(compacted, options)
        if line_capped is not None and len(line_capped) < len(compacted):
            compacted = line_capped
    if len(compacted) > options.max_chars:
        char_capped = _char_head_tail(compacted, options)
        if char_capped is not None and len(char_capped) < len(compacted):
            compacted = char_capped
    return compacted


def _char_head_tail_preserving_patterns(
    text: str,
    options: NoisegateOptions,
    patterns: tuple[re.Pattern[str], ...],
) -> str | None:
    if len(text) <= options.max_chars:
        return None
    matches = [match for pattern in patterns for match in pattern.finditer(text)]
    if not matches:
        return _char_head_tail(text, options)

    budget = max(0, options.max_chars)
    if budget == 0:
        return ""
    marker_a = "\n[noisegate: omitted before important output]\n"
    marker_b = "\n[noisegate: omitted after important output]\n"
    available = budget - len(marker_a) - len(marker_b)
    if available <= 0:
        return text[matches[0].start() : matches[0].start() + budget]

    head_chars = max(1, available // 5)
    tail_chars = max(1, available // 5)
    focus_chars = max(1, available - head_chars - tail_chars)
    match = matches[0]
    focus_start = max(head_chars, match.start() - focus_chars // 3)
    focus_end = min(len(text) - tail_chars, focus_start + focus_chars)
    focus_start = max(head_chars, focus_end - focus_chars)
    if focus_start >= focus_end:
        return _char_head_tail(text, options)
    return (
        f"{text[:head_chars]}{marker_a}"
        f"{text[focus_start:focus_end]}{marker_b}"
        f"{text[-tail_chars:]}"
    )


def _char_head_tail(text: str, options: NoisegateOptions) -> str | None:
    if len(text) <= options.max_chars:
        return None
    budget = max(0, options.max_chars)
    if budget == 0:
        return ""
    omitted = 0
    marker = ""
    available = budget
    head_chars = tail_chars = 0

    for _ in range(3):
        marker = f"\n[noisegate: omitted {omitted} chars]\n"
        available = max(0, budget - len(marker))
        if available <= 0:
            break
        head_chars = max(1, (available * 3) // 5)
        tail_chars = max(1, available - head_chars)
        if head_chars + tail_chars >= len(text):
            return None
        omitted = len(text) - head_chars - tail_chars

    marker = f"\n[noisegate: omitted {omitted} chars]\n"
    available = max(0, budget - len(marker))
    if available <= 0:
        return marker.strip()[:budget]
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


def _unchanged(text: str, reducer: str, command_class: str, *, reason: str) -> ReducedOutput:
    return ReducedOutput(
        text,
        False,
        {
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
        },
    )


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
) -> str:
    notices = _recovery_notices(metadata, artifact_dir=artifact_dir)
    if not notices:
        return text
    suffix = "\n" + "\n".join(notices)
    if options is None:
        return f"{text}{suffix}"

    budget = max(0, options.max_chars)
    if budget == 0:
        return ""
    if len(text) + len(suffix) <= budget:
        return f"{text}{suffix}"
    if len(suffix) >= budget:
        return suffix[:budget]

    text_budget = budget - len(suffix)
    reserved_options = replace(options, max_chars=text_budget)
    shortened = _enforce_final_budget(text, reserved_options)
    if len(shortened) > text_budget:
        shortened = shortened[:text_budget]
    return f"{shortened}{suffix}"


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
            sha256 = str(artifact.get("sha256") or "")[:12]
            notices.append(
                "[noisegate artifact: "
                f"id={artifact_id}; sha256={sha256}; cat {artifact_id}]"
            )
        else:
            reason = artifact.get("reason", "not_stored")
            notices.append(f"[noisegate artifact: not stored; reason={reason}]")

    return notices


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
