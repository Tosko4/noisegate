from __future__ import annotations

import hashlib
import os
import re
import shlex
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path

from ._version import __version__
from .artifacts import DEFAULT_SIZE_CAP, ArtifactError, ArtifactStore, ArtifactTooLarge

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class _SourceAlignmentWorkExhausted(Exception):
    """Internal signal that source alignment must fail open."""


class _ShellToken(str):
    """A shell token carrying lexical provenance for quoted operators."""

    redirection_operator_was_quoted: bool
    assignment_prefix_was_quoted: bool
    numeric_redirection_was_separated: bool
    was_quoted: bool

    def __new__(
        cls,
        value: str,
        *,
        redirection_operator_was_quoted: bool = False,
        assignment_prefix_was_quoted: bool = False,
        numeric_redirection_was_separated: bool = False,
        was_quoted: bool = False,
    ) -> _ShellToken:
        token = super().__new__(cls, value)
        token.redirection_operator_was_quoted = redirection_operator_was_quoted
        token.assignment_prefix_was_quoted = assignment_prefix_was_quoted
        token.numeric_redirection_was_separated = numeric_redirection_was_separated
        token.was_quoted = was_quoted
        return token


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
SECRET_ARTIFACT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"\b(?:api(?:[ \t]+|[_-]?)key|access[_-]?token|token|secret|password|passwd|"
        r"private[_-]?key|client[_-]?secret|credentials?|authorization|cookie|"
        r"session(?:id)?)\s*[:=]",
        r"^\s*(?:[<>*]\s*)?(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|x-auth-token|x-access-token|x-amz-security-token|x-goog-api-key)\s*[:=]",
        r"[\"'](?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|"
        r"x-auth-token|x-access-token|x-amz-security-token|x-goog-api-key|"
        r"api(?:[ \t]+|[_-]?)key|access[_-]?token|token|secret|password|passwd|"
        r"private[_-]?key|client[_-]?secret|credentials?)[\"']\s*:",
        r"^[ \t]*(?:(?:>[ \t]*)|(?:[-+*][ \t]+)|(?:\d+[.)][ \t]+))*"
        r"[\"']?-{5}BEGIN "
        r"(?:(?:[A-Z0-9]+ )*PRIVATE KEY|PGP PRIVATE KEY BLOCK)-{5}[\"']?[ \t]*\r?$",
        r"\btype\s*=\s*[\"']?password[\"']?(?=[\s/>]|$)",
        r"\bname\s*=\s*[\"']?(?:[A-Za-z0-9_.-]+\[)?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|token|secret|password|passwd|private[_-]?key|client[_-]?secret|credentials?|session(?:id)?)(?:\])?[\"']?(?=[\s;/>]|$)",
        r"\bbearer\s+[A-Za-z0-9._~+/=-]{12,}",
        r"(?:^|\s)--?(?:api[_-]?key|access[_-]?token|token|secret|password|passwd|private[_-]?key|client[_-]?secret|credentials?|authorization|cookie)\s+\S+",
        r"\b(?:password|passwd|authorization|cookie)\s+\S+",
        r"(?:^|\s)(?:-u|--user|--auth|--credentials?)(?:\s+|=)\S+",
        r"(?:^|\s)-u\S+:\S+",
    )
)
SECRET_ENV_ASSIGNMENT_KEY = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*\b(?=[\"']?\s*[:=])"
)
SECRET_ENV_KEY_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "API_KEY",
    "ACCESS_KEY",
    "PRIVATE_KEY",
    "CLIENT_SECRET",
    "CREDENTIAL",
    "AUTHORIZATION",
    "COOKIE",
    "SESSION",
)
SECRET_ENV_KEY_SUFFIXES = ("_PWD", "_PASS", "_AUTH")
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
MEMORY_RETRIEVAL_HELPERS = frozenset(
    {
        "hindsight_recall",
        "hindsight_reflect",
        "lcm_describe",
        "lcm_expand",
        "lcm_expand_query",
        "lcm_grep",
        "lcm_load_session",
        "session_search",
    }
)
HERMES_VALUELESS_GLOBAL_OPTIONS = frozenset(
    {
        "--accept-hooks",
        "--cli",
        "--dev",
        "--ignore-rules",
        "--ignore-user-config",
        "--no-restore-cwd",
        "--pass-session-id",
        "--safe-mode",
        "--tui",
        "--worktree",
        "--yolo",
        "-w",
    }
)
HERMES_REQUIRED_VALUE_GLOBAL_OPTIONS = frozenset(
    {
        "--model",
        "--provider",
        "--resume",
        "--skills",
        "--toolsets",
        "--usage-file",
        "-m",
        "-r",
        "-s",
        "-t",
    }
)
HERMES_OPTIONAL_VALUE_GLOBAL_OPTIONS = frozenset({"--continue", "-c"})
HERMES_ATTACHED_SHORT_VALUE_OPTIONS = ("-m", "-r", "-s", "-t")
HERMES_PROFILE_ID_PATTERN = re.compile(
    r"[a-z0-9][a-z0-9_-]{0,63}",
    re.IGNORECASE | re.ASCII,
)
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
LCM_EXTERNALIZED_PATTERN_IDS = frozenset(id(pattern) for pattern in LCM_EXTERNALIZED_PATTERNS)
OMISSION_NOTICE_PATTERN = re.compile(r"^\[noisegate: omitted \d+ (?:lines|chars)\]$")
LINE_OMISSION_NOTICE_PATTERN = re.compile(r"^\[noisegate: omitted (\d+) lines\]$")
CHAR_OMISSION_NOTICE_PATTERN = re.compile(r"^\[noisegate: omitted \d+ chars\]$")
_SOURCE_ALIGNMENT_WORK_LIMIT = 50_000
_SOURCE_ALIGNMENT_MAX_DEPTH = 512
_MEMORY_RETRIEVAL_SUBSTITUTION_LIMIT = 16


@dataclass(frozen=True)
class _SourceAlignmentSourceState:
    evidence_indices: dict[str, tuple[int, ...]]
    represented_prefix: tuple[int, ...]
    represented_ends: dict[int, tuple[int, ...]]
    source_is_omission: tuple[bool, ...]
    char_omission_indices: tuple[int, ...]


_SourceAlignmentSourceKey = str | tuple[str, ...]


@dataclass
class _SourceAlignmentWorkBudget:
    """Operation-scoped work and memoized results for source alignment."""

    limit: int
    spent: int = 0
    exhausted: bool = False
    alignment_calls: int = 0
    _source_cache: dict[
        _SourceAlignmentSourceKey,
        _SourceAlignmentSourceState | None,
    ] = field(default_factory=dict, repr=False)
    _alignment_cache: dict[
        tuple[_SourceAlignmentSourceKey, str],
        tuple[int, ...] | None,
    ] = field(default_factory=dict, repr=False)
    _remark_cache: dict[tuple[str, str], str | None] = field(
        default_factory=dict,
        repr=False,
    )

    def spend(self, amount: int = 1) -> None:
        if amount < 0:
            raise ValueError("alignment work amount must be non-negative")
        if self.exhausted or amount > self.limit - self.spent:
            self.exhausted = True
            raise _SourceAlignmentWorkExhausted
        self.spent += amount


_SOURCE_ALIGNMENT_WORK_BUDGET: ContextVar[_SourceAlignmentWorkBudget | None] = ContextVar(
    "noisegate_source_alignment_work_budget",
    default=None,
)


def _new_source_alignment_work_budget() -> _SourceAlignmentWorkBudget:
    return _SourceAlignmentWorkBudget(_SOURCE_ALIGNMENT_WORK_LIMIT)


@contextmanager
def _source_alignment_work_operation() -> Iterator[_SourceAlignmentWorkBudget]:
    """Install one fresh alignment budget for a complete public operation."""
    budget = _new_source_alignment_work_budget()
    token = _SOURCE_ALIGNMENT_WORK_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _SOURCE_ALIGNMENT_WORK_BUDGET.reset(token)


def _raise_if_source_alignment_work_exhausted() -> None:
    budget = _SOURCE_ALIGNMENT_WORK_BUDGET.get()
    if budget is None:
        raise RuntimeError("source alignment budget is not active")
    if budget.exhausted:
        raise _SourceAlignmentWorkExhausted


SHELL_SEPARATORS = {"|", "|&", "||", "&&", ";", "&", "(", ")", "{", "}"}
SOURCE_SEARCH_COMMANDS = {"rg", "grep", "ag", "ack"}
SOURCE_SEARCH_OPTIONS_WITH_VALUES = frozenset({
    "-A", "-B", "-C", "-f", "-g", "-m",
    "--after-context", "--before-context", "--colors", "--context", "--encoding",
    "--engine", "--field-context-separator", "--field-match-separator", "--glob",
    "--iglob", "--ignore-file", "--include", "--exclude", "--exclude-dir",
    "--exclude-from", "--label", "--max-count", "--path-separator", "--pre-glob",
    "--replace", "--sort", "--type", "--type-add",
})
RG_OPTIONS_WITH_VALUES = SOURCE_SEARCH_OPTIONS_WITH_VALUES | {
    "-E", "-j", "-M", "-r", "-t", "-T",
    "--context-separator", "--dfa-size-limit", "--max-columns", "--max-depth",
    "--regex-size-limit", "--sortr", "--threads", "--type-clear", "--type-not",
}
GREP_OPTIONS_WITH_VALUES = frozenset({
    "-A", "-B", "-C", "-D", "-d", "-e", "-f", "-m",
    "--after-context", "--before-context", "--binary-files", "--context",
    "--devices", "--directories", "--exclude", "--exclude-dir", "--exclude-from",
    "--file", "--group-separator", "--include", "--label", "--max-count", "--regexp",
})
GIT_GLOBAL_OPTIONS_WITH_VALUES = frozenset({
    "-C", "-c", "--attr-source", "--config-env", "--git-dir", "--namespace",
    "--super-prefix", "--work-tree",
})
GIT_GLOBAL_OPTIONS_WITHOUT_VALUES = frozenset({
    "-p", "--paginate", "-P", "--no-pager", "--bare", "--no-replace-objects",
    "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs",
    "--icase-pathspecs", "--no-optional-locks", "--no-advice", "--no-lazy-fetch",
})
OPTIONS_WITH_VALUES = {
    "-C",
    "-F",
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
    "--extra",
    "--env-file",
    "--extra-index-url",
    "--file",
    "--find-links",
    "--from",
    "--group",
    "--no-group",
    "--only-group",
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
    "--python-platform",
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
    "--cwd",
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
JQ_OPTION_ARITIES = {
    "--arg": 2, "--argfile": 2, "--argjson": 2,
    "--indent": 1, "--rawfile": 2, "--slurpfile": 2,
    "--from-file": 1, "--library-path": 1, "-f": 1, "-L": 1,
}
JQ_FILE_OPTIONS = frozenset({"--argfile", "--rawfile", "--slurpfile"})
YQ_OPTION_ARITIES = {
    "--front-matter": 1, "--input-format": 1, "--output-format": 1,
    "-I": 1, "-o": 1, "-p": 1,
}
YQ_INPUT_SUFFIXES = frozenset({
    ".base64", ".c", ".csv", ".h", ".hcl", ".i", ".ini", ".j", ".json", ".ky",
    ".kyaml", ".l", ".lua", ".p", ".properties", ".props", ".t", ".tf", ".toml",
    ".tsv", ".uri", ".x", ".xml", ".y", ".yaml", ".yml",
})


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
    return _run_text_reduction_operation(
        text,
        command=command,
        tool_name=tool_name,
        source=source,
        exit_code=exit_code,
        options=options,
        defer_artifact_store=False,
        share_alignment_budget=False,
    )


def _preview_reduce_text(
    text: str,
    *,
    command: str | None = None,
    tool_name: str | None = None,
    source: str | None = None,
    exit_code: int | None = None,
    options: NoisegateOptions | None = None,
) -> ReducedOutput:
    return _run_text_reduction_operation(
        text,
        command=command,
        tool_name=tool_name,
        source=source,
        exit_code=exit_code,
        options=options,
        defer_artifact_store=True,
        share_alignment_budget=False,
    )


def _reduce_text_in_operation(
    text: str,
    *,
    command: str | None = None,
    tool_name: str | None = None,
    source: str | None = None,
    exit_code: int | None = None,
    options: NoisegateOptions | None = None,
    defer_artifact_store: bool = False,
    extra_preserve_patterns: tuple[re.Pattern[str], ...] = (),
) -> ReducedOutput:
    return _run_text_reduction_operation(
        text,
        command=command,
        tool_name=tool_name,
        source=source,
        exit_code=exit_code,
        options=options,
        defer_artifact_store=defer_artifact_store,
        share_alignment_budget=True,
        extra_preserve_patterns=extra_preserve_patterns,
    )


def _run_text_reduction_operation(
    text: str,
    *,
    command: str | None,
    tool_name: str | None,
    source: str | None,
    exit_code: int | None,
    options: NoisegateOptions | None,
    defer_artifact_store: bool,
    share_alignment_budget: bool,
    extra_preserve_patterns: tuple[re.Pattern[str], ...] = (),
) -> ReducedOutput:
    if share_alignment_budget:
        _raise_if_source_alignment_work_exhausted()
        reduced = _reduce_text(
            text,
            command=command,
            tool_name=tool_name,
            source=source,
            exit_code=exit_code,
            options=options,
            defer_artifact_store=defer_artifact_store,
            extra_preserve_patterns=extra_preserve_patterns,
        )
        _raise_if_source_alignment_work_exhausted()
        return reduced

    try:
        with _source_alignment_work_operation():
            reduced = _reduce_text(
                text,
                command=command,
                tool_name=tool_name,
                source=source,
                exit_code=exit_code,
                options=options,
                defer_artifact_store=defer_artifact_store,
                extra_preserve_patterns=extra_preserve_patterns,
            )
            _raise_if_source_alignment_work_exhausted()
            return reduced
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
    defer_artifact_store: bool = False,
    extra_preserve_patterns: tuple[re.Pattern[str], ...] = (),
) -> ReducedOutput:
    options = options or NoisegateOptions.from_env()
    command_class = _reduction_command_class(
        command,
        text,
        tool_name=tool_name,
        exit_code=exit_code,
    )

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
    if command_class == "memory_retrieval":
        return _unchanged(
            text,
            "protected_memory_retrieval",
            command_class,
            reason="memory_retrieval_passthrough",
        )
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
    if command_class == "systemctl_show":
        return _unchanged(
            text,
            "protected_systemctl_show",
            command_class,
            reason="systemctl_show_passthrough",
        )
    if not _should_reduce(text, options):
        return _unchanged(text, "below_threshold", command_class, reason="below_threshold")

    preserve_patterns = _preserve_patterns_for_output(command_class, text)
    if extra_preserve_patterns:
        preserve_patterns = _combine_matching_patterns(
            text,
            preserve_patterns,
            extra_preserve_patterns,
        )
    exit_notices = _recovery_notices({"exit_code": exit_code})
    reducer_name, compacted = _apply_reducer(
        text,
        command_class,
        options,
        exit_code,
        extra_preserve_patterns=extra_preserve_patterns,
    )
    if compacted is not None and _omission_notices(text):
        compacted = _remark_excerpt_with_line_coverage(text, compacted)
        compacted = _ensure_ranked_diagnostic_after_line_coverage_remap(
            before=text,
            shortened=compacted,
            options=options,
            preserve_patterns=preserve_patterns,
            required_notices=exit_notices,
        )
    if compacted is not None and _dropped_lcm_externalized_match(
        before=text,
        after=compacted,
    ):
        compacted = None
    if compacted is not None:
        compacted = _enforce_final_budget(
            compacted,
            options,
            preserve_patterns=preserve_patterns,
        )
    if compacted is not None and _omission_notices(text):
        compacted = _ensure_ranked_diagnostic_after_line_coverage_remap(
            before=text,
            shortened=compacted,
            options=options,
            preserve_patterns=preserve_patterns,
            required_notices=exit_notices,
        )
    if compacted is not None and _dropped_omission_notice(before=text, after=compacted):
        compacted = None
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
    required_notices = _recovery_notices_for_text(
        compacted_body,
        metadata,
        artifact_dir=options.artifact_dir,
        preserve_patterns=preserve_patterns,
    )
    if _omission_notices(text):
        compacted_body = _ensure_ranked_diagnostic_after_line_coverage_remap(
            before=text,
            shortened=compacted_body,
            options=options,
            preserve_patterns=preserve_patterns,
            required_notices=required_notices,
        )
        if compacted_body is None:
            return _unchanged(
                text,
                "no_gain",
                command_class,
                reason="ranked_diagnostic_cannot_fit_with_notices",
                attempted_reducer=reducer_name,
            )
        _refresh_compacted_metrics(metadata, original=text, compacted=compacted_body)
    compacted = _append_recovery_notices(
        compacted_body,
        metadata,
        artifact_dir=options.artifact_dir,
        options=options,
        preserve_patterns=preserve_patterns,
        fail_open_text=text,
    )
    if _line_coverage_remap_dropped_ranked_diagnostic(
        before=text,
        after=compacted,
        options=options,
        preserve_patterns=preserve_patterns,
        required_notices=required_notices,
    ):
        return _unchanged(
            text,
            "no_gain",
            command_class,
            reason="ranked_diagnostic_dropped_after_notices",
            attempted_reducer=reducer_name,
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
    if options.artifact_enabled and not defer_artifact_store:
        planned_artifact = metadata.get("artifact")
        if isinstance(planned_artifact, dict) and planned_artifact.get("stored") is True:
            _raise_if_source_alignment_work_exhausted()
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
                    fail_open_text=text,
                )
            else:
                metadata["artifact"] = _store_artifact(text, options)
                required_notices = _recovery_notices_for_text(
                    compacted_body,
                    metadata,
                    artifact_dir=options.artifact_dir,
                    preserve_patterns=preserve_patterns,
                )
                if _omission_notices(text):
                    compacted_body = _ensure_ranked_diagnostic_after_line_coverage_remap(
                        before=text,
                        shortened=compacted_body,
                        options=options,
                        preserve_patterns=preserve_patterns,
                        required_notices=required_notices,
                    )
                    if compacted_body is None:
                        return _unchanged(
                            text,
                            "no_gain",
                            command_class,
                            reason="ranked_diagnostic_cannot_fit_after_artifact_store",
                            attempted_reducer=reducer_name,
                        )
                    _refresh_compacted_metrics(
                        metadata,
                        original=text,
                        compacted=compacted_body,
                    )
                compacted = _append_recovery_notices(
                    compacted_body,
                    metadata,
                    artifact_dir=options.artifact_dir,
                    options=options,
                    preserve_patterns=preserve_patterns,
                    fail_open_text=text,
                )
                if _line_coverage_remap_dropped_ranked_diagnostic(
                    before=text,
                    after=compacted,
                    options=options,
                    preserve_patterns=preserve_patterns,
                    required_notices=required_notices,
                ):
                    return _unchanged(
                        text,
                        "no_gain",
                        command_class,
                        reason="ranked_diagnostic_dropped_after_artifact_store",
                        attempted_reducer=reducer_name,
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

def classify_command(command: str | None, text: str, *, exit_code: int | None = None) -> str:
    command_s = (command or "").strip()
    command_l = command_s.lower()
    sample_l = text[:4_000].lower()
    text_l = text.lower()
    command_variants = _command_intent_variants(command_s)

    if _looks_like_memory_retrieval_command(command_s):
        return "memory_retrieval"
    if _is_source_search_command(
        command_s,
        sample=sample_l,
        text=text,
        exit_code=exit_code,
    ):
        return "source_search"
    if _is_patch_command(command_s):
        return "patch"
    if _looks_like_file_read_command(
        command_s,
        sample=sample_l,
        text=text_l,
        exit_code=exit_code,
    ):
        return "file_read"

    if _looks_like_v4a_patch(text):
        return "patch"
    if (
        _looks_like_diff_command(command_l)
        or "diff --git " in text
        or _looks_like_unified_diff(text)
    ):
        return "git_diff"

    source_consumer_class = _source_consumer_command_class(
        command_s,
        sample_l,
        text_l,
        exit_code=exit_code,
    )
    if source_consumer_class is not None:
        return source_consumer_class

    if "git status" in command_l or ("on branch " in sample_l and "working tree" in sample_l):
        return "git_status"
    if "git log" in command_l:
        return "git_log"
    for substitution_command in (command_l, *command_variants):
        substitution_class = _process_substitution_compactable_class(
            substitution_command,
            sample_l,
            text_l,
        )
        if substitution_class is not None:
            return substitution_class
    if any(_is_apt_command(variant) for variant in command_variants):
        return "apt"
    if any(_is_uv_pytest_resolution_failure(variant, text_l) for variant in command_variants):
        return "python_package"
    if any(_is_python_package_command(variant) for variant in command_variants):
        return "python_package"
    if any(_is_systemctl_show_command(variant) for variant in command_variants):
        return "systemctl_show"
    if (
        any(_is_pytest_command(variant) for variant in command_variants)
        or _contains_process_substitution_pytest(command_l)
        or "=== failures ===" in sample_l
    ):
        return "pytest"
    if any(_is_unittest_command(variant) for variant in command_variants) or re.search(
        r"ran \d+ tests?",
        sample_l,
    ):
        return "unittest"
    if any(_is_docker_log_command(variant) for variant in command_variants):
        return "docker_logs"
    if any(_is_log_stream_command(variant) for variant in command_variants):
        return "log_stream"
    if any(_is_docker_build_command(variant) for variant in command_variants) or (
        _looks_like_docker_build_output(text_l)
        and any(_can_infer_docker_build_from_output(variant) for variant in command_variants)
    ):
        return "docker_build"
    if any(_is_node_command(variant, sample_l) for variant in command_variants):
        return "node"
    return "generic"


def _reduction_command_class(
    command: str | None,
    text: str,
    *,
    tool_name: str | None,
    exit_code: int | None = None,
) -> str:
    command_class = classify_command(command, text, exit_code=exit_code)
    if (
        tool_name == "process"
        and command_class == "generic"
        and _first_pattern_match(text, LOG_STREAM_PATTERNS)
    ):
        return "log_stream"
    return command_class


def _compactable_command_output_class(command: str, text: str) -> str | None:
    return _compactable_output_class_for_tokens(
        _shell_tokens(command),
        text[:2_000].lower(),
        text.lower(),
    )


def _command_has_compactable_intent(command: str) -> bool:
    return any(
        _compactable_class_for_tokens(tokens, "", "") is not None
        for tokens in _command_execution_segments(command)
    )


def _exact_command_output_class(
    command: str,
    text: str,
    *,
    exit_code: int | None = None,
) -> str | None:
    tokens = _shell_tokens(command)
    sample = text[:2_000].lower()
    lowered = text.lower()
    command_class = _exact_class_for_tokens(tokens)
    if command_class is None and _looks_like_file_read_command(
        command,
        sample=sample,
        text=lowered,
        exit_code=exit_code,
    ):
        command_class = "file_read"
    if command_class is None and _is_source_search_command(
        command,
        sample=sample,
        text=lowered,
        exit_code=exit_code,
    ):
        command_class = "source_search"
    if command_class == "source_search" and (
        _text_has_exact_owner_output_against_later_compactable(
            tokens,
            text,
            exit_code=exit_code,
        )
        or (
            not _tokens_source_search_hides_filenames(tokens)
            and _contains_likely_source_search_output(text)
        )
    ):
        return command_class
    if command_class == "file_read" and _text_has_exact_owner_output_against_later_compactable(
        tokens,
        text,
        exit_code=exit_code,
    ):
        return command_class
    return None


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
    preserve_patterns: tuple[re.Pattern[str], ...] = ()
    if command_class in {"pytest", "unittest"}:
        preserve_patterns = _preservation_patterns(text, CRITICAL_PATTERNS)
    elif command_class in {"apt", "python_package"}:
        preserve_patterns = _priority_preservation_patterns(
            text,
            PACKAGE_PATTERNS,
            PACKAGE_HIGH_SIGNAL_PRIORITY_PATTERNS,
        )
    elif command_class == "node":
        preserve_patterns = _preservation_patterns(text, NODE_PATTERNS)
    elif command_class == "docker_build":
        preserve_patterns = _priority_preservation_patterns(
            text,
            DOCKER_BUILD_PRESERVATION_PATTERNS,
            DOCKER_BUILD_HIGH_SIGNAL_PRIORITY_PATTERNS,
        )
    elif command_class in {"docker_logs", "log_stream"}:
        patterns = DOCKER_LOG_PATTERNS if command_class == "docker_logs" else LOG_STREAM_PATTERNS
        preserve_patterns = _preservation_patterns(text, patterns)
    elif command_class == "generic" and _first_pattern_match(text, CRITICAL_PATTERNS):
        preserve_patterns = _preservation_patterns(text, CRITICAL_PATTERNS)

    reducer_patterns = REDUCER_ANCHOR_PATTERNS_BY_COMMAND_CLASS.get(command_class, ())
    base_patterns = preserve_patterns or lcm_patterns
    combined = list(base_patterns)
    seen_pattern_ids = {id(pattern) for pattern in combined}
    added_pattern = False
    source_lines = text.splitlines()
    for pattern in (*lcm_patterns, *reducer_patterns):
        pattern_id = id(pattern)
        if pattern_id in seen_pattern_ids or (
            pattern in reducer_patterns
            and not any(pattern.search(line) for line in source_lines)
        ):
            continue
        seen_pattern_ids.add(pattern_id)
        combined.append(pattern)
        added_pattern = True
    if not added_pattern:
        return base_patterns or None
    return tuple(combined)


def _combine_matching_patterns(
    text: str,
    base_patterns: tuple[re.Pattern[str], ...] | None,
    extra_patterns: tuple[re.Pattern[str], ...],
) -> tuple[re.Pattern[str], ...] | None:
    combined = list(base_patterns or ())
    seen_pattern_ids = {id(pattern) for pattern in combined}
    source_lines = text.splitlines()
    for pattern in extra_patterns:
        if id(pattern) in seen_pattern_ids or not any(
            pattern.search(line) for line in source_lines
        ):
            continue
        seen_pattern_ids.add(id(pattern))
        combined.append(pattern)
    return tuple(combined) or None


def _apply_reducer(
    text: str,
    command_class: str,
    options: NoisegateOptions,
    exit_code: int | None,
    *,
    extra_preserve_patterns: tuple[re.Pattern[str], ...] = (),
) -> tuple[str, str | None]:
    lcm_patterns = _lcm_externalized_patterns_for(text)
    reducer_patterns = REDUCER_ANCHOR_PATTERNS_BY_COMMAND_CLASS.get(command_class, ())
    field_patterns = _combine_matching_patterns(text, None, extra_preserve_patterns) or ()
    field_priority_patterns = (field_patterns,) if field_patterns else ()
    reduction_patterns = reducer_patterns + field_patterns + lcm_patterns
    if options.mode == "head_tail":
        if field_patterns:
            return "generic_diagnostic", _important_lines(
                text,
                options,
                field_patterns + CRITICAL_PATTERNS + lcm_patterns,
                priority_patterns=field_priority_patterns + HIGH_SIGNAL_PRIORITY_PATTERNS,
                exit_code=exit_code,
            )
        if lcm_patterns:
            return "generic_head_tail", _important_lines(
                text,
                options,
                lcm_patterns,
                exit_code=exit_code,
            )
        return "generic_head_tail", _head_tail(text, options)
    if command_class in {"pytest", "unittest"}:
        return command_class, _important_lines(
            text,
            options,
            reduction_patterns,
            priority_patterns=field_priority_patterns + HIGH_SIGNAL_PRIORITY_PATTERNS,
            exit_code=exit_code,
        )
    if command_class in {"apt", "python_package"}:
        return command_class, _important_lines(
            text,
            options,
            reduction_patterns,
            priority_patterns=field_priority_patterns + PACKAGE_HIGH_SIGNAL_PRIORITY_PATTERNS,
            exit_code=exit_code,
        )
    if command_class == "node":
        return "node", _important_lines(
            text,
            options,
            reduction_patterns,
            priority_patterns=field_priority_patterns + NODE_HIGH_SIGNAL_PRIORITY_PATTERNS,
            exit_code=exit_code,
        )
    if command_class == "docker_build":
        return "docker_build", _important_lines(
            text,
            options,
            reduction_patterns,
            priority_patterns=(
                field_priority_patterns + DOCKER_BUILD_HIGH_SIGNAL_PRIORITY_PATTERNS
            ),
            exit_code=exit_code,
        )
    if command_class == "docker_logs":
        return "docker_logs", _important_lines(
            text,
            options,
            reduction_patterns,
            priority_patterns=field_priority_patterns + HIGH_SIGNAL_PRIORITY_PATTERNS,
            exit_code=exit_code,
        )
    if command_class == "log_stream":
        return "log_stream", _important_lines(
            text,
            options,
            reduction_patterns,
            priority_patterns=field_priority_patterns + HIGH_SIGNAL_PRIORITY_PATTERNS,
            exit_code=exit_code,
        )
    if command_class == "git_status":
        return "git_status", _important_lines(
            text,
            options,
            reduction_patterns,
            priority_patterns=field_priority_patterns + HIGH_SIGNAL_PRIORITY_PATTERNS,
            exit_code=exit_code,
        )
    if command_class == "git_log":
        return "git_log", _head_tail(text, replace(options, head_lines=40, tail_lines=8))
    if command_class == "generic" and field_patterns:
        return "generic_diagnostic", _important_lines(
            text,
            options,
            field_patterns + CRITICAL_PATTERNS + lcm_patterns,
            priority_patterns=field_priority_patterns + HIGH_SIGNAL_PRIORITY_PATTERNS,
            exit_code=exit_code,
        )
    if (
        command_class == "generic"
        and exit_code != 0
        and _first_pattern_match(text, CRITICAL_PATTERNS)
    ):
        return "generic_critical", _important_lines(
            text,
            options,
            CRITICAL_PATTERNS + lcm_patterns,
            exit_code=exit_code,
        )
    if lcm_patterns:
        return "generic_head_tail", _important_lines(
            text,
            options,
            lcm_patterns,
            exit_code=exit_code,
        )
    return "generic_head_tail", _head_tail(text, options)


TEST_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"={2,}.*(failures|errors|short test summary|failed|passed)",
        r"^(failed|error)\s+",
        r"\bFAILED\b|\bERROR\b",
        r"assertionerror|traceback|exception",
        r"\b[a-z_]+(?:error|exception)\b(?=:|$)",
        r"^\s*E\s+",
        r"^\s*(?:=+\s*)?(?:\d+\s+(?:failed|passed|errors?)(?:,\s*)?)+\b",
        r"tests?/.*::",
    )
)

DIAGNOSTIC_LOCATION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"^\s*(?:[A-Z]:)?[^\s:\r\n][^:\r\n]*\.[A-Z0-9_+-]+:\d+(?::\d+)?:"
        r"\s*(?:\[[^\]\r\n]+\]\s*)?(?:error|warning|info|hint|[A-Z][A-Z0-9_-]*\d{2,})\b",
        r"^\s*(?:[A-Z]:)?[^\s:\r\n][^:\r\n]*\.[A-Z0-9_+-]+:\d+:\d+\s+-\s+"
        r"(?:error|warning|info|hint):\s+\S",
        r"^\s*(?:[A-Z]:)?[^\s:\r\n][^:\r\n]*\.[A-Z0-9_+-]+\(\d+,\d+\):"
        r"\s*(?:error|warning|info|hint)(?:\s+[A-Z][A-Z0-9_-]*\d+)?\b",
        r"^\s*\d+:\d+\s+(?:error|warning)\s+\S+",
        r"^\s*(?:[A-Z]:)?[^\s:\r\n][^:\r\n]*\.[A-Z0-9_+-]+:\d+:\d+"
        r"\s+\[(?:error|warning|info|hint)\]\s+(?:[A-Z][A-Z0-9_-]*\d+\s*:\s*)?\S+",
        r"^\s*[A-Z][A-Z0-9_-]*\d{2,}\b(?:\s+\[[^\]\r\n]+\])?\s+\S",
        r"^\s*-->\s+(?:[A-Z]:)?[^\s:\r\n][^:\r\n]*\.[A-Z0-9_+-]+:\d+:\d+\s*$",
    )
)

CRITICAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bFAILED\b|\bERROR\b",
        r"^(failed|error)\s+",
        r"^(e:|err:|error:)",
        r"assertionerror|traceback|(?:base)?exceptiongroup|baseexception|\bexception\b\s*:",
        r"valueerror|typeerror|referenceerror|syntaxerror|rangeerror|runtimeerror",
        r"\b[a-z_]+(?:error|exception)\b(?=:|$)",
        r"\bunhandled\s+exception\b",
        r"\bexception\s+in\b",
        r"\btask\s+exception\s+was\s+never\s+retrieved\b",
        r"\bexception\s+was\s+never\s+retrieved\b",
        r"\bduring\s+handling\b.*\banother\s+exception\s+occurred\b",
        r"\bthe\s+above\s+exception\s+was\s+the\s+direct\s+cause\b.*\bfollowing\s+exception\b",
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
        r"no solution found|unsatisfiable|could not build wheels|failed building wheel",
        r"dpkg:\s+error|errors were encountered while processing",
        r"\b(error|failed|failure|fail|conflict|conflicts|conflicting|traceback|exception)\b",
        r"\b[a-z_]+(?:error|exception)\b(?=:|$)",
        r"hash sum mismatch|dependency problems|unmet dependencies|permission denied",
        r"npm err!|pnpm err!|yarn.*error|err_pnpm|elifecycle",
        r"^(warning:|warn:)",
        r"successfully installed|installing collected packages|requirement already satisfied",
        r"\b\d+\s+newly installed\b|^setting up\b",
        r"^(installed|downloaded|resolved|prepared|audited)\s+\d+",
        r"(?:^|\|)\s*\d+%\s+\[[^\]]+\]",
        r"^(?:get|hit|ign):\d+\b|^fetched\s+\d+|^reading package lists\b",
        r"^building dependency tree\b|^reading state information\b",
        r"^up to date\b",
    )
)

NODE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"npm err!|pnpm err!|yarn.*error|err_pnpm|elifecycle",
        r"\berror\b|\bfailed\b|\bfail\b",
        r"\bwarning\b|\bwarn\b",
        r"tests?.*(failed|passed)",
        r"\d+\s+(passed|failed|errors?)\b",
        r"^(added|removed|changed|audited)\s+\d+\s+packages?\b",
        r"^\s*(found\s+0\s+vulnerabilities|\d+\s+vulnerabilities?)\b",
        r"^up to date\b",
    )
)

NODE_PRESERVATION_PATTERNS = (*NODE_PATTERNS, *CRITICAL_PATTERNS)

DOCKER_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\berror\b|\bfailed\b|\bfail\b",
        r"dockerfile|unable to|denied|not found",
        r"(?:^|\|)\s*#\d+\s+.*"
        r"(load build definition|load metadata|load \.dockerignore|"
        r"transferring dockerfile|exporting|writing image|done|cached)",
        r"(?:^|\|)\s*=>\s+.*"
        r"(load build definition|load metadata|load \.dockerignore|"
        r"transferring dockerfile|exporting|writing image|done|cached|dockerfile)",
        r"^#\d+|^=>",
    )
)

DOCKER_BUILD_PRESERVATION_PATTERNS = (*DOCKER_PATTERNS, *CRITICAL_PATTERNS)

DOCKER_LOG_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\berror\b|\bfailed\b|\bfail\b",
        r"traceback|exception|fatal|critical|panic|segmentation fault|segfault",
        r"valueerror|typeerror|referenceerror|runtimeerror|assertionerror",
        r"\b[a-z_]+(?:error|exception)\b(?=:|$)",
        r"unable to|denied|refused|timed?\s*out|timeout|unreachable|not found|permission denied",
    )
)

LOG_STREAM_PATTERNS = (
    *DOCKER_LOG_PATTERNS,
    re.compile(
        r"npm err!|pnpm err!|yarn.*error|err_pnpm|elifecycle|"
        r"\bwarn(?:ing)?\b|exit code\s+\d+|exited with",
        re.IGNORECASE,
    ),
)

HIGH_SIGNAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"valueerror|typeerror|referenceerror|runtimeerror|assertionerror",
        r"\b[a-z_]+(?:error|exception)\b(?=:|$)|^\s*exception:|unhandled\s+exception",
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
            r"^\s*(?:\|\s*)*(?:base)?exceptiongroup\b(?=:|$)",
            r"\btask\s+exception\s+was\s+never\s+retrieved\b",
            r"\bexception\s+was\s+never\s+retrieved\b",
            r"\bduring\s+handling\b.*\banother\s+exception\s+occurred\b",
            r"\bthe\s+above\s+exception\s+was\s+the\s+direct\s+cause\b.*\bfollowing\s+exception\b",
            r"valueerror|typeerror|referenceerror|runtimeerror|assertionerror",
            r"\b[a-z_]+(?:error|exception)\b(?=:|$)|^\s*exception:|unhandled\s+exception",
            r"npm err!.*cannot find module",
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
            r"\b(?:refused|timed?\s*out|timeout|unreachable)\b",
        ),
        (
            r"denied|not found",
        ),
    )
)

NODE_HIGH_SIGNAL_PRIORITY_PATTERNS = (
    tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"^\s*error:",
            r"cannot find module",
            r"npm err!.*cannot find module",
            r"valueerror|typeerror|referenceerror|syntaxerror|rangeerror|runtimeerror",
        )
    ),
    *HIGH_SIGNAL_PRIORITY_PATTERNS,
)

PACKAGE_HIGH_SIGNAL_PRIORITY_PATTERNS = tuple(
    tuple(re.compile(pattern, re.IGNORECASE) for pattern in group)
    for group in (
        (
            r"resolutionimpossible",
            r"no solution found|unsatisfiable|failed to resolve dependencies",
            r"because .*conflicts? with",
            r"no matching distribution found",
            r"unable to locate package",
            r"dependency problems|unmet dependencies",
            r"hash sum mismatch",
        ),
        (
            r"traceback \(most recent call last\)",
            r"valueerror|typeerror|referenceerror|runtimeerror|assertionerror",
            r"\b[a-z_]+(?:error|exception)\b(?=:|$)",
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
            r"valueerror|typeerror|referenceerror|runtimeerror|assertionerror",
            r"\b[a-z_]+(?:error|exception)\b(?=:|$)",
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

REDUCER_ANCHOR_PATTERNS_BY_COMMAND_CLASS = {
    "pytest": TEST_PATTERNS,
    "unittest": TEST_PATTERNS,
    "apt": PACKAGE_PATTERNS,
    "python_package": PACKAGE_PATTERNS,
    "node": NODE_PRESERVATION_PATTERNS,
    "docker_build": DOCKER_BUILD_PRESERVATION_PATTERNS,
    "docker_logs": DOCKER_LOG_PATTERNS,
    "log_stream": LOG_STREAM_PATTERNS,
    "git_status": GIT_STATUS_PATTERNS,
}


def _important_lines(
    text: str,
    options: NoisegateOptions,
    patterns: tuple[re.Pattern[str], ...],
    *,
    priority_patterns: tuple[tuple[re.Pattern[str], ...], ...] = HIGH_SIGNAL_PRIORITY_PATTERNS,
    exit_code: int | None = None,
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
        if any(pattern.search(line) for pattern in patterns) or OMISSION_NOTICE_PATTERN.fullmatch(
            line
        ):
            important.append(index)

    if len(important) > options.max_important_lines:
        priority = _important_priority_indices(lines, important, options, priority_patterns)
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
                exit_code=exit_code,
            )
            if budgeted is not None:
                return budgeted
            return _head_tail(text, options)
        if len(text) > options.max_chars:
            budgeted = _line_budgeted_important_excerpt(
                lines,
                important,
                options,
                patterns,
                priority_patterns,
                exit_code=exit_code,
            )
            if budgeted is not None:
                return budgeted
            return _char_head_tail_preserving_patterns(
                text,
                options,
                _priority_preservation_patterns(text, patterns, priority_patterns),
            )
        return _char_head_tail(text, options)
    selected_indices = sorted(keep)
    if _matching_indices(lines, selected_indices, LCM_EXTERNALIZED_PATTERNS):
        selected = _marked_excerpt_for_line_indices(lines, selected_indices)
        if selected is None:
            return None
    else:
        selected = _lines_with_markers(lines, selected_indices)
    if _line_count(selected) > options.max_lines:
        budgeted = _line_budgeted_important_excerpt(
            lines,
            important,
            options,
            patterns,
            priority_patterns,
            exit_code=exit_code,
        )
        if budgeted is None:
            return None
        selected = budgeted
    if len(selected) > options.max_chars:
        budgeted = _line_budgeted_important_excerpt(
            lines,
            important,
            options,
            patterns,
            priority_patterns,
            exit_code=exit_code,
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
            pattern
            for pattern in base_patterns
            if id(pattern) not in LCM_EXTERNALIZED_PATTERN_IDS
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
    *,
    exit_code: int | None = None,
) -> str | None:
    if not important:
        return None
    lcm_priority = _matching_indices(lines, important, LCM_EXTERNALIZED_PATTERNS)
    omission_priority = [
        index
        for index in important
        if OMISSION_NOTICE_PATTERN.fullmatch(lines[index])
    ]
    recovery_priority = lcm_priority + [
        index for index in omission_priority if index not in lcm_priority
    ]
    signal_priority = _priority_indices(lines, important, priority_patterns)
    critical_priority = _matching_indices(lines, important, CRITICAL_PATTERNS)
    ranked_priority = sorted(
        signal_priority or critical_priority or important,
        key=lambda index: _failure_detail_sort_key(lines[index], index),
    )
    fallback_priority = sorted(
        [
            index
            for index in critical_priority
            if index not in signal_priority and index not in ranked_priority
        ],
        key=lambda index: _failure_detail_sort_key(lines[index], index),
    )
    priority = recovery_priority + [
        index
        for index in ranked_priority + fallback_priority
        if index not in recovery_priority
    ]
    best_rank = min(_failure_detail_rank(lines[index]) for index in ranked_priority)
    max_context = max(0, options.important_context_lines)

    # Externalized payload refs and existing omission notices are recovery
    # evidence, not just interesting log lines. Try one ranked diagnostic at a
    # time with all evidence so an overlong primary failure does not hide a
    # shorter active-reducer anchor. If none fit, preserve all evidence alone or
    # fail open.
    if recovery_priority:
        all_signal_priority = _all_priority_indices(lines, important, priority_patterns)
        lcm_diagnostic_priority: list[int] = []
        for index in [
            *all_signal_priority,
            *sorted(
                critical_priority,
                key=lambda item: _failure_detail_sort_key(lines[item], item),
            ),
        ]:
            if index not in recovery_priority and index not in lcm_diagnostic_priority:
                lcm_diagnostic_priority.append(index)

        for diagnostic_anchor in lcm_diagnostic_priority:
            for context in range(max_context, -1, -1):
                keep: set[int] = set(recovery_priority)
                start = max(0, diagnostic_anchor - context)
                end = min(len(lines), diagnostic_anchor + context + 1)
                keep.update(range(start, end))
                sorted_keep = sorted(keep)
                marked_candidate = _marked_excerpt_for_line_indices(lines, sorted_keep)
                if marked_candidate is not None and _fits_budget(marked_candidate, options):
                    return marked_candidate

        marked_refs = _marked_excerpt_for_line_indices(lines, recovery_priority)
        if marked_refs is not None and _fits_budget(marked_refs, options):
            return marked_refs
        return None

    best_detail_key = min(
        (
            _failure_detail_rank(lines[index]),
            _failure_detail_subrank(lines[index]),
        )
        for index in ranked_priority
    )
    best_ranked_priority = [
        index
        for index in ranked_priority
        if (
            _failure_detail_rank(lines[index]),
            _failure_detail_subrank(lines[index]),
        )
        == best_detail_key
    ][:3]
    multi_anchor_priority = ranked_priority[:3]
    if options.max_lines <= 5:
        concrete_tight_priority = [
            index
            for index in ranked_priority + fallback_priority
            if _failure_detail_rank(lines[index]) == best_rank
        ][:3]
        has_concrete_failure = any(
            re.search(
                r"\b[a-z0-9_]+error\b(?::|$)|"
                r"^\s*(?:error|exception):\s*(?!.*\b(?:generic|transient|noise)\b)|"
                r"no solution found|unsatisfiable|failed to resolve dependencies",
                lines[index],
                re.IGNORECASE,
            )
            for index in concrete_tight_priority
        )
        failed_test_id_pattern = r"\bFAILED\b.*tests?/.*::|tests?/.*::.*\bFAILED\b"
        has_failed_test_id = any(
            re.search(failed_test_id_pattern, lines[index], re.IGNORECASE)
            for index in concrete_tight_priority
        )
        if has_concrete_failure and (has_failed_test_id or best_ranked_priority):
            tight_indices = sorted(
                concrete_tight_priority if has_failed_test_id else best_ranked_priority
            )
            tight_anchor_candidate = _marked_excerpt_for_line_indices(lines, tight_indices)
            exit_notice = (
                f"[noisegate: exit_code={exit_code}]"
                if isinstance(exit_code, int) and exit_code != 0
                else None
            )
            tight_options = replace(
                options,
                max_chars=max(
                    0,
                    options.max_chars - (len(exit_notice) + 1 if exit_notice else 0),
                ),
                max_lines=max(0, options.max_lines - (1 if exit_notice else 0)),
            )
            if (
                tight_anchor_candidate is not None
                and _fits_budget(tight_anchor_candidate, tight_options)
                and len(tight_anchor_candidate) < len("\n".join(lines))
            ):
                return tight_anchor_candidate
            single_tight_priority = (
                concrete_tight_priority if has_failed_test_id else best_ranked_priority
            )
            if has_failed_test_id:
                single_tight_priority = sorted(
                    single_tight_priority,
                    key=lambda index: not re.search(
                        failed_test_id_pattern,
                        lines[index],
                        re.IGNORECASE,
                    ),
                )
            for index in single_tight_priority:
                marked_line = _marked_excerpt_for_line_indices(lines, [index])
                if (
                    marked_line is not None
                    and _fits_budget(marked_line, tight_options)
                    and len(marked_line) < len("\n".join(lines))
                ):
                    return marked_line
    if len(multi_anchor_priority) > 1:
        notice = (
            f"[noisegate: exit_code={exit_code}]"
            if isinstance(exit_code, int) and exit_code != 0
            else None
        )
        reserved_options = replace(
            options,
            max_chars=max(0, options.max_chars - (len(notice) + 1 if notice else 0)),
            max_lines=max(0, options.max_lines - (1 if notice else 0)),
        )
        concrete_candidate = _concrete_failure_excerpt_for_notices(
            "\n".join(lines),
            reserved_options,
        )
        if (
            concrete_candidate is not None
            and _represented_line_coverage(concrete_candidate) != len(lines)
        ):
            concrete_candidate = None
        if (
            reserved_options.max_lines <= 5
            and concrete_candidate is not None
            and re.search(
                r"\bFAILED\b.*tests?/.*::|tests?/.*::.*\bFAILED\b",
                concrete_candidate,
                re.IGNORECASE,
            )
        ):
            return concrete_candidate

        for context in range(max_context, -1, -1):
            keep: set[int] = set()
            for anchor in multi_anchor_priority:
                start = max(0, anchor - context)
                end = min(len(lines), anchor + context + 1)
                keep.update(range(start, end))
            candidate = _marked_excerpt_for_line_indices(lines, sorted(keep))
            if candidate is not None and _fits_budget(candidate, reserved_options):
                return candidate

        best_candidate = _marked_excerpt_for_line_indices(lines, best_ranked_priority)
        if best_candidate is not None and _fits_budget(best_candidate, reserved_options):
            return best_candidate

        if concrete_candidate is not None:
            return concrete_candidate

        for anchor in priority:
            if (
                _would_hide_better_failure_anchor(
                    best_rank,
                    _failure_detail_rank(lines[anchor]),
                )
                and not re.search(r"tests?/.*::.*\bFAILED\b", lines[anchor], re.IGNORECASE)
            ):
                return None
            for context in range(max_context, -1, -1):
                start = max(0, anchor - context)
                end = min(len(lines), anchor + context + 1)
                candidate = _lines_with_surrounding_omission_markers(
                    lines,
                    start,
                    end - 1,
                )
                if _fits_budget(candidate, reserved_options):
                    return candidate
        return None

    for anchor in priority:
        if (
            anchor not in lcm_priority
            and _would_hide_better_failure_anchor(
                best_rank,
                _failure_detail_rank(lines[anchor]),
            )
            and not re.search(r"tests?/.*::.*\bFAILED\b", lines[anchor], re.IGNORECASE)
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
                recovery_suffix_reserve = len("\n[noisegate: exit_code=1]")
                omitted_before = anchor
                omitted_after = len(lines) - anchor - 1
                marked_anchor = "\n".join(
                    part
                    for part in (
                        f"[noisegate: omitted {omitted_before} lines]" if omitted_before else "",
                        lines[anchor],
                        f"[noisegate: omitted {omitted_after} lines]" if omitted_after else "",
                    )
                    if part
                )
                if (
                    len(marked_anchor) + recovery_suffix_reserve <= options.max_chars
                    and _fits_budget(marked_anchor, options)
                ):
                    return marked_anchor
                if (
                    len(lines[anchor]) + recovery_suffix_reserve <= options.max_chars
                    and _fits_budget(lines[anchor], options)
                ):
                    return lines[anchor]
                continue
    return None


def _failure_detail_rank(line: str) -> int:
    """Prefer diagnostic detail over progress/status lines when budgets are tight."""
    if re.search(r"resolutionimpossible|because .*conflicts? with", line, re.IGNORECASE):
        return 0
    if re.search(r"^\s*error:\s+.*\b(?:generic|transient|noise)\b", line, re.IGNORECASE):
        return 6
    if re.fullmatch(r"\s*unhandled\s+exception\s*", line, re.IGNORECASE):
        return 2
    if re.search(r"failed to solve|did not complete successfully", line, re.IGNORECASE):
        return 2
    if _is_diagnostic_detail_line(line):
        return 0
    if re.search(r"npm err!|pnpm err!|err_pnpm|elifecycle|yarn.*error", line, re.IGNORECASE):
        return 0
    if re.search(r"\btraceback\b", line, re.IGNORECASE):
        return 1
    if re.search(r"tests?/.*::.*\bFAILED\b\s*\[\s*\d+%\]", line, re.IGNORECASE):
        return 2
    if re.search(r"\bFAILED\b.*tests?/.*::|tests?/.*::.*\bFAILED\b", line, re.IGNORECASE):
        return 0
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


def _failure_detail_sort_key(line: str, index: int) -> tuple[int, int, int]:
    return (_failure_detail_rank(line), _failure_detail_subrank(line), index)


def _failure_detail_subrank(line: str) -> int:
    if re.search(
        r"^\s*(?:\|\s*)*(?:base)?exceptiongroup\b(?=:|$)"
        r"|\btask\s+exception\s+was\s+never\s+retrieved\b"
        r"|\bexception\s+was\s+never\s+retrieved\b"
        r"|\bduring\s+handling\b.*\banother\s+exception\s+occurred\b"
        r"|\bthe\s+above\s+exception\s+was\s+the\s+direct\s+cause\b.*\bfollowing\s+exception\b",
        line,
        re.IGNORECASE,
    ):
        return 0
    return 1


def _is_diagnostic_detail_line(line: str) -> bool:
    if re.search(r"^\s*E\s+", line):
        return True
    if re.search(r"^\s*(?:e:|err:|error:)", line, re.IGNORECASE):
        return True
    if _is_incidental_exception_line(line):
        return False
    return bool(
        re.search(
            r"\b(assertionerror|[a-z0-9_]+error)\b(?::|$)"
            r"|^\s*(?:\|\s*)*(?:base)?exceptiongroup\b(?=:|$)"
            r"|^\s*(?:\|\s*)*baseexception\b(?=:|$)"
            r"|\bexception\b\s*:"
            r"|\bunhandled\s+exception\b"
            r"|\bexception\s+in\b"
            r"|\btask\s+exception\s+was\s+never\s+retrieved\b"
            r"|\bexception\s+was\s+never\s+retrieved\b"
            r"|\bduring\s+handling\b.*\banother\s+exception\s+occurred\b"
            r"|\bthe\s+above\s+exception\s+was\s+the\s+direct\s+cause\b.*\bfollowing\s+exception\b",
            line,
            re.IGNORECASE,
        )
    )


def _is_incidental_exception_line(line: str) -> bool:
    return bool(re.search(r"\bexception ignored\b", line, re.IGNORECASE))


def _important_priority_indices(
    lines: list[str],
    indices: list[int],
    options: NoisegateOptions,
    priority_patterns: tuple[tuple[re.Pattern[str], ...], ...] = HIGH_SIGNAL_PRIORITY_PATTERNS,
) -> list[int]:
    lcm_priority = _matching_indices(lines, indices, LCM_EXTERNALIZED_PATTERNS)
    omission_priority = [
        index for index in indices if OMISSION_NOTICE_PATTERN.fullmatch(lines[index])
    ]
    recovery_priority = lcm_priority + [
        index for index in omission_priority if index not in lcm_priority
    ]
    signal_priority = _all_priority_indices(lines, indices, priority_patterns)
    critical_priority = sorted(
        [
            index
            for index in _matching_indices(lines, indices, CRITICAL_PATTERNS)
            if index not in signal_priority
        ],
        key=lambda index: _failure_detail_sort_key(lines[index], index),
    )

    def might_fit_with_refs(index: int) -> bool:
        candidate_indices = sorted({*recovery_priority, index})
        if recovery_priority:
            candidate = _marked_excerpt_for_line_indices(lines, candidate_indices)
            return candidate is not None and _fits_budget(candidate, options)
        return _fits_budget(lines[index], options)

    fitting_signals = [index for index in signal_priority if might_fit_with_refs(index)]
    fitting_critical = next(
        (index for index in critical_priority if might_fit_with_refs(index)),
        critical_priority[0] if critical_priority else None,
    )
    fallback_aware_priority = [*fitting_signals]
    if not fitting_signals and fitting_critical is not None:
        fallback_aware_priority.append(fitting_critical)
    fallback_aware_priority.extend(
        index for index in signal_priority if index not in fitting_signals
    )
    fallback_aware_priority.extend(critical_priority)
    return recovery_priority + [
        index for index in fallback_aware_priority if index not in recovery_priority
    ]


def _all_priority_indices(
    lines: list[str],
    indices: list[int],
    priority_patterns: tuple[tuple[re.Pattern[str], ...], ...],
) -> list[int]:
    priority: list[int] = []
    for pattern_group in priority_patterns:
        group_matches = sorted(
            [
                index
                for index in indices
                if index not in priority
                and any(pattern.search(lines[index]) for pattern in pattern_group)
            ],
            key=lambda index: _failure_detail_sort_key(lines[index], index),
        )
        priority.extend(group_matches)
    return priority


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
        has_preserved_match = (
            preserve_patterns is not None
            and _first_pattern_match(compacted, preserve_patterns) is not None
        )
        if has_preserved_match:
            line_capped = _important_lines(compacted, options, preserve_patterns)
        else:
            line_capped = _head_tail(compacted, options)
        if line_capped is not None and len(line_capped) < len(compacted):
            line_capped = _remark_budget_rewrite_with_line_coverage(
                before=compacted,
                after=line_capped,
                options=options,
                allow_boundary_fallback=not has_preserved_match,
            )
            if line_capped is None:
                return None
            if _budget_rewrite_loses_recovery_evidence(
                before=compacted,
                after=line_capped,
            ):
                return None
            compacted = line_capped
    if len(compacted) > options.max_chars:
        has_preserved_match = (
            preserve_patterns is not None
            and _first_pattern_match(compacted, preserve_patterns) is not None
        )
        if has_preserved_match:
            char_capped = _char_head_tail_preserving_patterns(
                compacted,
                options,
                preserve_patterns,
            )
        else:
            char_capped = _char_head_tail(compacted, options)
        if char_capped is not None and len(char_capped) < len(compacted):
            char_capped = _remark_budget_rewrite_with_line_coverage(
                before=compacted,
                after=char_capped,
                options=options,
                allow_boundary_fallback=not has_preserved_match,
            )
            if char_capped is None:
                return None
            if _budget_rewrite_loses_recovery_evidence(
                before=compacted,
                after=char_capped,
            ):
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
    multi_anchor_excerpt = _lcm_and_diagnostic_excerpt(layout, options, matches)
    if multi_anchor_excerpt is not None:
        return multi_anchor_excerpt
    best_rank = min(_rank_for_span_match(match, layout) for match in matches)
    for match in matches:
        if (
            not _span_match_line_has_lcm_ref(match, layout)
            and _would_hide_better_failure_anchor(best_rank, _rank_for_span_match(match, layout))
        ):
            return None
        line_excerpt = _line_centered_excerpt(text, options, match, layout=layout)
        if line_excerpt is not None:
            return line_excerpt
    return None


def _lcm_and_diagnostic_excerpt(
    layout: _LineLayout,
    options: NoisegateOptions,
    matches: list[_SpanMatch],
) -> str | None:
    lcm_lines: list[int] = []
    for index, line in enumerate(layout.lines):
        if any(pattern.search(line) for pattern in LCM_EXTERNALIZED_PATTERNS):
            lcm_lines.append(index)

    diagnostic_lines: list[int] = []
    for match in matches:
        index = match.line_index
        if (
            index is not None
            and index not in lcm_lines
            and index not in diagnostic_lines
        ):
            diagnostic_lines.append(index)
    if not lcm_lines or not diagnostic_lines:
        return None

    for diagnostic_line in diagnostic_lines:
        candidate = _marked_excerpt_for_line_indices(
            layout.lines,
            sorted({*lcm_lines, diagnostic_line}),
        )
        if candidate is not None and _fits_budget(candidate, options):
            return candidate
    return None


def _rank_for_span_match(match: _SpanMatch, layout: _LineLayout) -> int:
    if match.detail_rank is not None:
        return match.detail_rank
    for index, (start, end) in enumerate(layout.offsets):
        if start <= match.start() <= end:
            return _failure_detail_rank(layout.lines[index])
    return 5


def _span_match_line_has_lcm_ref(match: _SpanMatch, layout: _LineLayout) -> bool:
    line_index = match.line_index
    if line_index is None or line_index < 0 or line_index >= len(layout.lines):
        return False
    return any(pattern.search(layout.lines[line_index]) for pattern in LCM_EXTERNALIZED_PATTERNS)


def _would_hide_better_failure_anchor(best_rank: int, candidate_rank: int) -> bool:
    return best_rank <= 1 and candidate_rank > 1


def _dropped_lcm_externalized_match(*, before: str, after: str) -> bool:
    before_matches = Counter(_lcm_externalized_matches(before))
    after_matches = Counter(_lcm_externalized_matches(after))
    return any(after_matches[match] < count for match, count in before_matches.items())


def _budget_rewrite_loses_recovery_evidence(*, before: str, after: str) -> bool:
    if _dropped_lcm_externalized_match(before=before, after=after):
        return True
    return _dropped_omission_notice(before=before, after=after)


def _remark_budget_rewrite_with_line_coverage(
    *,
    before: str,
    after: str,
    options: NoisegateOptions,
    allow_boundary_fallback: bool,
) -> str | None:
    if not _omission_notices(before):
        return after
    remarked = _remark_excerpt_with_line_coverage(before, after)
    if remarked == after:
        return after
    if remarked is not None and _fits_budget(remarked, options):
        return remarked
    if allow_boundary_fallback:
        source_lines = before.splitlines()
        boundary_indices = [
            index
            for index, line in enumerate(source_lines)
            if not OMISSION_NOTICE_PATTERN.fullmatch(line)
        ]
        boundary_groups: list[tuple[int, ...]] = []
        if boundary_indices:
            boundary_groups.extend(
                [
                    (boundary_indices[0], boundary_indices[-1]),
                    (boundary_indices[0],),
                    (boundary_indices[-1],),
                ]
            )
        for group in dict.fromkeys(boundary_groups):
            candidate = _marked_excerpt_for_line_indices(source_lines, list(group))
            if candidate is None:
                continue
            boundary_excerpt = _remark_excerpt_with_line_coverage(before, candidate)
            if boundary_excerpt is not None and _fits_budget(boundary_excerpt, options):
                return boundary_excerpt
    return remarked


def _lcm_externalized_matches(text: str) -> list[str]:
    matches: list[str] = []
    for pattern in LCM_EXTERNALIZED_PATTERNS:
        for match in pattern.finditer(text):
            matches.append(match.group(0))
    return matches


def _omission_notices(text: str) -> list[str]:
    return [line for line in text.splitlines() if OMISSION_NOTICE_PATTERN.fullmatch(line)]


def _dropped_omission_notice(*, before: str, after: str) -> bool:
    if not _omission_notices(before):
        return False
    return _remark_excerpt_with_line_coverage(before, after) != after


def _represented_line_coverage(text: str) -> int:
    coverage = 0
    for line in text.splitlines():
        match = LINE_OMISSION_NOTICE_PATTERN.fullmatch(line)
        coverage += int(match.group(1)) if match is not None else 1
    return coverage


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
        if (
            any(pattern.search(line) for pattern in preserve_patterns)
            and line not in after
            and _failure_detail_rank(line) <= 1
            and not re.match(r"\s*(?:e|err|error):\s", line, re.IGNORECASE)
        ):
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
    candidates: list[tuple[tuple[int, int, int, int, int, int, int], _SpanMatch]] = []
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
            detail_subrank = _failure_detail_subrank(stripped_line)
            recovery_rank = 0 if id(pattern) in LCM_EXTERNALIZED_PATTERN_IDS else 1
            if pattern_order_first:
                rank = (
                    recovery_rank,
                    pattern_index,
                    detail_rank,
                    detail_subrank,
                    line_index,
                    offset + match.start(),
                    offset + match.end(),
                )
            else:
                rank = (
                    recovery_rank,
                    detail_rank,
                    detail_subrank,
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
    diagnostic_patterns = tuple(
        pattern for pattern in patterns if id(pattern) not in LCM_EXTERNALIZED_PATTERN_IDS
    )
    return any(
        patterns is priority_set
        or patterns == priority_set
        or (
            len(diagnostic_patterns) > len(priority_set)
            and diagnostic_patterns[: len(priority_set)] == priority_set
        )
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


def _refresh_compacted_metrics(
    metadata: dict[str, JsonValue],
    *,
    original: str,
    compacted: str,
) -> None:
    metadata["omitted_chars"] = max(0, len(original) - len(compacted))
    metadata["omitted_lines"] = max(0, _line_count(original) - _line_count(compacted))


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
    if _looks_secret_bearing_text(text):
        return {"stored": False, "reason": "secret_detected", "size_bytes": len(data)}
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
    data = text.encode("utf-8")
    if len(data) > options.artifact_size_cap:
        return {
            "stored": False,
            "reason": "too_large",
            "size_bytes": len(data),
            "size_cap": options.artifact_size_cap,
        }
    if _looks_secret_bearing_text(text):
        return {
            "stored": False,
            "reason": "secret_detected",
            "size_bytes": len(data),
        }
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


def _concrete_failure_excerpt_for_notices(
    text: str,
    options: NoisegateOptions,
    *,
    preserve_patterns: tuple[re.Pattern[str], ...] | None = None,
) -> str | None:
    lines = text.splitlines()
    if options.max_lines < 2:
        return None

    def fully_represented(candidate: str | None) -> str | None:
        if candidate is None:
            return None
        if _omission_notices(text):
            candidate = _remark_excerpt_with_line_coverage(text, candidate)
        if (
            candidate is None
            or _represented_line_coverage(candidate) != _represented_line_coverage(text)
        ):
            return None
        return candidate

    concrete_indices = [
        index
        for index, line in enumerate(lines)
        if re.search(
            r"\b[a-z0-9_]+error\b(?::|$)|^\s*(?:error|exception):\s*(?!.*\b(?:generic|transient|noise)\b)",
            line,
            re.IGNORECASE,
        )
    ]
    failed_indices = [
        index
        for index, line in enumerate(lines)
        if re.search(r"\bFAILED\b.*tests?/.*::|tests?/.*::.*\bFAILED\b", line, re.IGNORECASE)
    ]
    if not concrete_indices:
        return None

    rich_keep: set[int] = set()
    if preserve_patterns is not None:
        rich_keep.update(_ranked_diagnostic_line_indices(text, preserve_patterns)[:2])
    for index in concrete_indices[:2]:
        if index > 0 and re.search(
            r"\btraceback\b|\bunhandled\s+exception\b|Externalized tool output",
            lines[index - 1],
            re.IGNORECASE,
        ):
            rich_keep.add(index - 1)
        elif index > 1 and re.search(r"\btraceback\b", lines[index - 2], re.IGNORECASE):
            rich_keep.add(index - 2)
        rich_keep.add(index)
    if failed_indices:
        rich_keep.add(failed_indices[0])
    for index, line in enumerate(lines):
        if any(pattern.search(line) for pattern in LCM_EXTERNALIZED_PATTERNS):
            rich_keep.add(index)
    if len(rich_keep) > 1:
        rich_indices = sorted(rich_keep)
        marked_rich_candidate = _marked_excerpt_for_line_indices(lines, rich_indices)
        marked_rich_candidate = fully_represented(marked_rich_candidate)
        if marked_rich_candidate is not None and _fits_budget(marked_rich_candidate, options):
            return marked_rich_candidate

    keep = [concrete_indices[0]]
    if failed_indices:
        keep.append(failed_indices[0])
    keep = sorted(set(keep))
    if len(keep) == 1:
        index = keep[0]
        marked_candidate = "\n".join(
            part
            for part in (
                f"[noisegate: omitted {index} lines]" if index else "",
                lines[index],
                (
                    f"[noisegate: omitted {len(lines) - index - 1} lines]"
                    if index < len(lines) - 1
                    else ""
                ),
            )
            if part
        )
        marked_candidate = fully_represented(marked_candidate)
        if marked_candidate is not None and _fits_budget(marked_candidate, options):
            return marked_candidate
    if len(keep) > 1:
        marked_candidate = _marked_excerpt_for_line_indices(lines, keep)
        marked_candidate = fully_represented(marked_candidate)
        if marked_candidate is not None and _fits_budget(marked_candidate, options):
            return marked_candidate
    return None


def _marked_excerpt_for_line_indices(lines: list[str], indices: list[int]) -> str | None:
    if not lines or not indices:
        return None
    sorted_indices = sorted(set(index for index in indices if 0 <= index < len(lines)))
    if not sorted_indices:
        return None

    parts: list[str] = []
    first_index = sorted_indices[0]
    if first_index:
        parts.append(f"[noisegate: omitted {first_index} lines]")

    previous_index: int | None = None
    for index in sorted_indices:
        if previous_index is not None and index > previous_index + 1:
            parts.append(f"[noisegate: omitted {index - previous_index - 1} lines]")
        parts.append(lines[index])
        previous_index = index

    last_index = sorted_indices[-1]
    if last_index < len(lines) - 1:
        parts.append(f"[noisegate: omitted {len(lines) - last_index - 1} lines]")
    return "\n".join(parts)


def _ranked_diagnostic_line_indices(
    text: str,
    preserve_patterns: tuple[re.Pattern[str], ...] | None,
) -> list[int]:
    if preserve_patterns is None:
        return []

    lines = text.splitlines()
    indices: list[int] = []
    for match in _ranked_pattern_line_matches(text, preserve_patterns):
        index = match.line_index
        if (
            index is None
            or index < 0
            or index >= len(lines)
            or index in indices
            or OMISSION_NOTICE_PATTERN.fullmatch(lines[index])
            or any(pattern.search(lines[index]) for pattern in LCM_EXTERNALIZED_PATTERNS)
        ):
            continue
        indices.append(index)
    return indices


def _line_coverage_remap_dropped_ranked_diagnostic(
    *,
    before: str,
    after: str,
    options: NoisegateOptions,
    preserve_patterns: tuple[re.Pattern[str], ...] | None,
    required_notices: list[str] | None = None,
) -> bool:
    if not _omission_notices(before):
        return False
    lines = before.splitlines()
    diagnostic_indices = _ranked_diagnostic_line_indices(before, preserve_patterns)
    if not diagnostic_indices:
        return False
    best = _best_ranked_diagnostic_excerpt(
        before=before,
        options=options,
        preserve_patterns=preserve_patterns,
        required_notices=required_notices,
    )
    if best is None:
        return after != before
    best_index, _ = best
    body = after
    notices = required_notices or []
    after_lines = after.splitlines()
    if notices and after_lines[-len(notices) :] == notices:
        body = "\n".join(after_lines[: -len(notices)])
    selected_indices = _source_line_indices_for_excerpt(
        lines,
        body,
        _source_key=before,
    )
    return selected_indices is None or best_index not in selected_indices


def _ensure_ranked_diagnostic_after_line_coverage_remap(
    *,
    before: str,
    shortened: str | None,
    options: NoisegateOptions,
    preserve_patterns: tuple[re.Pattern[str], ...] | None,
    required_notices: list[str] | None = None,
) -> str | None:
    if shortened is None or not _omission_notices(before):
        return shortened

    best = _best_ranked_diagnostic_excerpt(
        before=before,
        options=options,
        preserve_patterns=preserve_patterns,
        required_notices=required_notices,
    )
    diagnostic_indices = _ranked_diagnostic_line_indices(before, preserve_patterns)
    if not diagnostic_indices:
        return shortened
    if best is None:
        return None

    best_index, best_excerpt = best
    body_options = _options_reserving_notices(options, required_notices)
    if body_options is None:
        return None
    selected_indices = _source_line_indices_for_excerpt(
        before.splitlines(),
        shortened,
        _source_key=before,
    )
    if (
        selected_indices is not None
        and best_index in selected_indices
        and _fits_budget(shortened, body_options)
    ):
        return shortened
    return best_excerpt


def _best_ranked_diagnostic_excerpt(
    *,
    before: str,
    options: NoisegateOptions,
    preserve_patterns: tuple[re.Pattern[str], ...] | None,
    required_notices: list[str] | None = None,
) -> tuple[int, str] | None:
    token = None
    try:
        if _SOURCE_ALIGNMENT_WORK_BUDGET.get() is None:
            token = _SOURCE_ALIGNMENT_WORK_BUDGET.set(
                _new_source_alignment_work_budget()
            )
        return _best_ranked_diagnostic_excerpt_with_budget(
            before=before,
            options=options,
            preserve_patterns=preserve_patterns,
            required_notices=required_notices,
        )
    except _SourceAlignmentWorkExhausted:
        if token is None:
            raise
        return None
    finally:
        if token is not None:
            _SOURCE_ALIGNMENT_WORK_BUDGET.reset(token)


def _best_ranked_diagnostic_excerpt_with_budget(
    *,
    before: str,
    options: NoisegateOptions,
    preserve_patterns: tuple[re.Pattern[str], ...] | None,
    required_notices: list[str] | None,
) -> tuple[int, str] | None:
    body_options = _options_reserving_notices(options, required_notices)
    if body_options is None:
        return None

    lines = before.splitlines()
    lcm_indices = [
        index
        for index, line in enumerate(lines)
        if any(pattern.search(line) for pattern in LCM_EXTERNALIZED_PATTERNS)
    ]
    for diagnostic_index in _ranked_diagnostic_line_indices(before, preserve_patterns):
        candidate = _marked_excerpt_for_line_indices(
            lines,
            [*lcm_indices, diagnostic_index],
        )
        if candidate is None:
            continue
        candidate = _remark_excerpt_with_line_coverage_with_budget(
            before,
            candidate,
            source_lines=lines,
        )
        if candidate is not None and _fits_budget(candidate, body_options):
            return diagnostic_index, candidate
    return None


def _options_reserving_notices(
    options: NoisegateOptions,
    required_notices: list[str] | None,
) -> NoisegateOptions | None:
    notices = required_notices or []
    if not notices:
        return options
    suffix = "\n" + "\n".join(notices)
    max_chars = options.max_chars - len(suffix)
    max_lines = options.max_lines - len(notices)
    if max_chars <= 0 or max_lines <= 0:
        return None
    return replace(options, max_chars=max_chars, max_lines=max_lines)


def _remark_excerpt_with_line_coverage(
    before: str,
    shortened: str,
) -> str | None:
    token = None
    try:
        if _SOURCE_ALIGNMENT_WORK_BUDGET.get() is None:
            token = _SOURCE_ALIGNMENT_WORK_BUDGET.set(
                _new_source_alignment_work_budget()
            )
        return _remark_excerpt_with_line_coverage_with_budget(before, shortened)
    except _SourceAlignmentWorkExhausted:
        if token is None:
            raise
        return None
    finally:
        if token is not None:
            _SOURCE_ALIGNMENT_WORK_BUDGET.reset(token)


def _remark_excerpt_with_line_coverage_with_budget(
    before: str,
    shortened: str,
    *,
    source_lines: list[str] | None = None,
) -> str | None:
    budget = _SOURCE_ALIGNMENT_WORK_BUDGET.get()
    if budget is None:
        raise RuntimeError("source alignment budget is not active")
    cache_key = (before, shortened)
    if cache_key in budget._remark_cache:
        return budget._remark_cache[cache_key]

    source_lines = before.splitlines() if source_lines is None else source_lines
    selected_indices = _source_line_indices_for_excerpt(
        source_lines,
        shortened,
        _source_key=before,
    )
    if selected_indices is None:
        budget._remark_cache[cache_key] = None
        return None

    # Character notices cannot be represented by a line-coverage marker. Keep
    # every source occurrence at its source position; generated lookalikes in
    # ``shortened`` are deliberately ignored above.
    selected_set = set(selected_indices)
    source_state = budget._source_cache.get(before)
    if source_state is None:
        budget._remark_cache[cache_key] = None
        return None
    selected_set.update(source_state.char_omission_indices)
    ordered_indices = sorted(selected_set)
    budget.spend(1 + len(ordered_indices))

    parts: list[str] = []
    source_cursor = 0
    for index in ordered_indices:
        omitted_coverage = (
            source_state.represented_prefix[index]
            - source_state.represented_prefix[source_cursor]
        )
        if omitted_coverage:
            parts.append(f"[noisegate: omitted {omitted_coverage} lines]")
        parts.append(source_lines[index])
        source_cursor = index + 1
    omitted_coverage = (
        source_state.represented_prefix[-1]
        - source_state.represented_prefix[source_cursor]
    )
    if omitted_coverage:
        parts.append(f"[noisegate: omitted {omitted_coverage} lines]")
    remarked = "\n".join(parts) or None
    budget._remark_cache[cache_key] = remarked
    return remarked


def _source_line_indices_for_excerpt(
    source_lines: list[str],
    excerpt: str,
    *,
    _work_limit: int | None = None,
    _source_key: str | None = None,
) -> list[int] | None:
    """Return the unique source occurrences selected by a marked excerpt.

    Omission notices in an excerpt have two possible provenances: immutable
    notices copied from the source, or markers generated for a skipped source
    interval. Generated line markers can describe either physical lines (the
    first reducer pass) or represented coverage (a later remap pass). Keeping
    both interpretations until adjacent anchors disambiguate them preserves
    occurrence identity without trusting marker text alone. Alignment search
    is explicitly bounded; exhausting the bound returns ambiguous so callers
    fail open.
    """
    excerpt_lines = excerpt.splitlines()
    work_limit = _SOURCE_ALIGNMENT_WORK_LIMIT if _work_limit is None else _work_limit
    budget = _SOURCE_ALIGNMENT_WORK_BUDGET.get()
    owns_budget = budget is None
    if owns_budget:
        if work_limit < 0:
            return None
        budget = _SourceAlignmentWorkBudget(work_limit)
    elif _work_limit is not None and _work_limit < 0:
        budget.exhausted = True
        raise _SourceAlignmentWorkExhausted
    if budget.exhausted:
        if owns_budget:
            return None
        raise _SourceAlignmentWorkExhausted

    budget.alignment_calls += 1
    source_key: _SourceAlignmentSourceKey = (
        tuple(source_lines) if _source_key is None else _source_key
    )
    operation_cache_key = (source_key, excerpt)
    if operation_cache_key in budget._alignment_cache:
        cached = budget._alignment_cache[operation_cache_key]
        return None if cached is None else list(cached)
    if len(excerpt_lines) > _SOURCE_ALIGNMENT_MAX_DEPTH:
        budget._alignment_cache[operation_cache_key] = None
        return None

    call_work_limit = _work_limit if not owns_budget else None
    call_work = 0
    result_limit = 2

    def spend_work(amount: int = 1) -> None:
        nonlocal call_work
        if call_work_limit is not None and amount > call_work_limit - call_work:
            budget.exhausted = True
            raise _SourceAlignmentWorkExhausted
        budget.spend(amount)
        call_work += amount

    def add_unique(
        results: list[tuple[int, ...]],
        candidates: tuple[tuple[int, ...], ...],
    ) -> None:
        for candidate in candidates:
            # Tuple construction and equality are linear in the selected-anchor
            # count, so charge that work as well as the state transition.
            spend_work(1 + (len(candidate) * len(results)))
            if candidate not in results:
                results.append(candidate)
                if len(results) >= result_limit:
                    return

    if source_key in budget._source_cache:
        source_state = budget._source_cache[source_key]
    else:
        evidence_indices_building: dict[str, list[int]] = {}
        represented_prefix_building = [0]
        represented_ends_building: dict[int, list[int]] = {}
        source_is_omission_building: list[bool] = []
        char_omission_indices_building: list[int] = []
        try:
            # Source indices are immutable within an operation. Build and charge
            # them once, then share them across ranked candidates and later passes.
            spend_work(len(source_lines))
            for index, source_line in enumerate(source_lines):
                source_is_omission_building.append(
                    OMISSION_NOTICE_PATTERN.fullmatch(source_line) is not None
                )
                if source_is_omission_building[-1]:
                    evidence_indices_building.setdefault(source_line, []).append(index)
                line_omission = LINE_OMISSION_NOTICE_PATTERN.fullmatch(source_line)
                if source_is_omission_building[-1] and line_omission is None:
                    char_omission_indices_building.append(index)
                coverage = int(line_omission.group(1)) if line_omission is not None else 1
                represented = represented_prefix_building[-1] + coverage
                represented_prefix_building.append(represented)
                represented_ends_building.setdefault(represented, []).append(index + 1)
        except ValueError:
            budget._source_cache[source_key] = None
            budget._alignment_cache[operation_cache_key] = None
            return None
        except _SourceAlignmentWorkExhausted:
            if owns_budget:
                return None
            raise
        source_state = _SourceAlignmentSourceState(
            evidence_indices={
                line: tuple(indices)
                for line, indices in evidence_indices_building.items()
            },
            represented_prefix=tuple(represented_prefix_building),
            represented_ends={
                coverage: tuple(indices)
                for coverage, indices in represented_ends_building.items()
            },
            source_is_omission=tuple(source_is_omission_building),
            char_omission_indices=tuple(char_omission_indices_building),
        )
        budget._source_cache[source_key] = source_state
    if source_state is None:
        budget._alignment_cache[operation_cache_key] = None
        return None

    evidence_indices = source_state.evidence_indices
    represented_prefix = source_state.represented_prefix
    represented_ends = source_state.represented_ends
    source_is_omission = source_state.source_is_omission
    excerpt_is_omission: list[bool] = []
    excerpt_line_coverage: list[int | None] = []
    try:
        spend_work(len(excerpt_lines))
        for excerpt_line in excerpt_lines:
            excerpt_is_omission.append(
                OMISSION_NOTICE_PATTERN.fullmatch(excerpt_line) is not None
            )
            line_omission = LINE_OMISSION_NOTICE_PATTERN.fullmatch(excerpt_line)
            excerpt_line_coverage.append(
                int(line_omission.group(1)) if line_omission is not None else None
            )
    except ValueError:
        budget._alignment_cache[operation_cache_key] = None
        return None
    except _SourceAlignmentWorkExhausted:
        if owns_budget:
            return None
        raise

    suffix_has_ordinary_line = [False] * (len(excerpt_lines) + 1)
    for index in range(len(excerpt_lines) - 1, -1, -1):
        suffix_has_ordinary_line[index] = (
            not excerpt_is_omission[index] or suffix_has_ordinary_line[index + 1]
        )

    def represented_gap_end(start: int, coverage: int) -> int | None:
        ends = represented_ends.get(represented_prefix[start] + coverage, ())
        spend_work(1 + len(ends).bit_length())
        position = bisect_right(ends, start)
        return ends[position] if position < len(ends) else None

    cache: dict[tuple[int, int], tuple[tuple[int, ...], ...]] = {}

    def align(excerpt_index: int, source_index: int) -> tuple[tuple[int, ...], ...]:
        cache_key = (excerpt_index, source_index)
        if cache_key in cache:
            return cache[cache_key]
        spend_work()
        if excerpt_index == len(excerpt_lines):
            aligned = ((),)
            cache[cache_key] = aligned
            return aligned

        excerpt_line = excerpt_lines[excerpt_index]
        results: list[tuple[int, ...]] = []
        if not excerpt_is_omission[excerpt_index]:
            if (
                source_index < len(source_lines)
                and source_lines[source_index] == excerpt_line
                and not source_is_omission[source_index]
            ):
                suffixes = align(excerpt_index + 1, source_index + 1)
                prefixed: list[tuple[int, ...]] = []
                for suffix in suffixes:
                    spend_work(1 + len(suffix))
                    prefixed.append((source_index, *suffix))
                add_unique(
                    results,
                    tuple(prefixed),
                )
        else:
            # A source notice is immutable evidence rather than kept content.
            # Reducers may rank it independently of nearby ordinary lines, so
            # let it match a later source occurrence. Any resulting kept-line
            # ambiguity is still rejected below.
            occurrences = evidence_indices.get(excerpt_line, [])
            spend_work(1 + len(occurrences).bit_length())
            occurrence_index = bisect_left(occurrences, source_index)
            while occurrence_index < len(occurrences):
                spend_work()
                evidence_index = occurrences[occurrence_index]
                add_unique(results, align(excerpt_index + 1, evidence_index + 1))
                # With no ordinary line left, every successful provenance path
                # has the same empty mapping. One success therefore exhausts
                # the distinct result space without exploring quadratic marker
                # combinations.
                if results and not suffix_has_ordinary_line[excerpt_index]:
                    aligned = (results[0],)
                    cache[cache_key] = aligned
                    return aligned
                if len(results) >= result_limit:
                    break
                occurrence_index += 1

            omitted = excerpt_line_coverage[excerpt_index]
            if omitted is not None:
                generated_ends: set[int] = set()
                physical_end = source_index + omitted
                if omitted > 0 and physical_end <= len(source_lines):
                    generated_ends.add(physical_end)
                represented_end = (
                    represented_gap_end(source_index, omitted) if omitted > 0 else None
                )
                if omitted > 0 and represented_end is not None:
                    generated_ends.add(represented_end)

                for generated_end in sorted(generated_ends):
                    spend_work()
                    if excerpt_index == len(excerpt_lines) - 1:
                        suffixes = ((),) if generated_end == len(source_lines) else ()
                    else:
                        suffixes = align(excerpt_index + 1, generated_end)
                    add_unique(results, suffixes)
                    if results and not suffix_has_ordinary_line[excerpt_index]:
                        aligned = (results[0],)
                        cache[cache_key] = aligned
                        return aligned
                    if len(results) >= result_limit:
                        break

        aligned = tuple(results)
        cache[cache_key] = aligned
        return aligned

    try:
        mappings = align(0, 0)
    except _SourceAlignmentWorkExhausted:
        if owns_budget:
            return None
        raise
    if len(mappings) != 1:
        budget._alignment_cache[operation_cache_key] = None
        return None
    mapping = mappings[0]
    budget._alignment_cache[operation_cache_key] = mapping
    return list(mapping)


def _append_recovery_notices(
    text: str,
    metadata: dict[str, JsonValue],
    *,
    artifact_dir: Path | None = None,
    options: NoisegateOptions | None = None,
    preserve_patterns: tuple[re.Pattern[str], ...] | None = None,
    fail_open_text: str | None = None,
) -> str:
    fallback = text if fail_open_text is None else fail_open_text
    notices = _recovery_notices_for_text(
        text,
        metadata,
        artifact_dir=artifact_dir,
        preserve_patterns=preserve_patterns,
    )
    if not notices:
        return text
    requires_exit_notice = any(
        notice.startswith("[noisegate: exit_code=") for notice in notices
    )
    suffix = "\n" + "\n".join(notices)
    if options is None:
        return f"{text}{suffix}"

    budget = max(0, options.max_chars)
    if budget == 0:
        return fallback if requires_exit_notice else ""
    if len(text) + len(suffix) <= budget:
        candidate = f"{text}{suffix}"
        if _fits_budget(candidate, options):
            return candidate
    # Notices already present in the original output are immutable recovery
    # evidence. The reducer has already canonicalized their coverage/position;
    # if a recovery suffix still requires another shortening pass, fail open.
    if fail_open_text is not None and _omission_notices(fail_open_text):
        return fallback
    if (
        fail_open_text is not None
        and not _omission_notices(fallback)
        and any(
            CHAR_OMISSION_NOTICE_PATTERN.fullmatch(line)
            for line in text.splitlines()
        )
    ):
        # Recompute generated char coverage from the original output when room
        # must be reserved for an exit/artifact notice. The intermediate char
        # marker only describes the first rewrite and cannot cover a second.
        text = fallback
    if len(suffix) >= budget:
        if requires_exit_notice:
            return fallback
        return _enforce_final_budget(
            text,
            options,
            preserve_patterns=preserve_patterns,
        ) or text

    text_budget = budget - len(suffix)
    reserved_options = replace(
        options,
        max_chars=text_budget,
        max_lines=max(1, options.max_lines - len(notices)),
    )
    reserved_tight_excerpt = _concrete_failure_excerpt_for_notices(
        text,
        reserved_options,
        preserve_patterns=preserve_patterns,
    )
    if reserved_tight_excerpt is None and reserved_options.max_lines < 2 and re.search(
        r"\bunhandled\s+exception\b",
        text,
        re.IGNORECASE,
    ):
        return fallback if requires_exit_notice else text
    used_reserved_tight_excerpt = reserved_tight_excerpt is not None
    if used_reserved_tight_excerpt:
        shortened = reserved_tight_excerpt
    else:
        shortened = _enforce_final_budget(
            text,
            reserved_options,
            preserve_patterns=preserve_patterns,
        )
    if shortened is not None and not used_reserved_tight_excerpt:
        marked_shortened = _single_preserved_line_excerpt(
            text,
            reserved_options,
            preserve_patterns,
        )
        if (
            marked_shortened is not None
            and "[noisegate: omitted" in marked_shortened
            and "[noisegate: omitted" not in shortened
            and _fits_budget(f"{marked_shortened}{suffix}", options)
        ):
            shortened = marked_shortened
    if shortened is not None and not used_reserved_tight_excerpt and (
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
        shortened = _single_preserved_line_excerpt(
            text,
            reserved_options,
            preserve_patterns,
        )
        if shortened is None:
            return fallback if requires_exit_notice else text
    if shortened is None:
        shortened = _single_preserved_line_excerpt(
            text,
            reserved_options,
            preserve_patterns,
        )
    if shortened is None:
        notice_only = suffix.lstrip("\n")
        if (
            (preserve_patterns is None or _first_pattern_match(text, preserve_patterns) is None)
            and _has_recoverable_artifact(metadata)
            and _fits_budget(notice_only, options)
        ):
            return notice_only
        if requires_exit_notice:
            return fallback
        return text
    source_has_omission_marker = fail_open_text is not None and bool(
        _omission_notices(fallback)
    )
    has_line_omission_marker = any(
        LINE_OMISSION_NOTICE_PATTERN.fullmatch(line)
        for line in text.splitlines()
    )
    evidence_source = fallback if source_has_omission_marker else text
    if (
        source_has_omission_marker
        or has_line_omission_marker
        or _lcm_externalized_matches(text)
    ):
        remarked = _remark_excerpt_with_line_coverage(evidence_source, shortened)
        if remarked is None:
            return fallback
        shortened = remarked
        if (
            _dropped_omission_notice(before=evidence_source, after=shortened)
            or (
                (source_has_omission_marker or has_line_omission_marker)
                and _represented_line_coverage(evidence_source)
                != _represented_line_coverage(shortened)
            )
        ):
            return fallback
    if _dropped_lcm_externalized_match(before=text, after=shortened):
        return fallback
    if (
        (source_has_omission_marker or has_line_omission_marker)
        and _dropped_omission_notice(before=evidence_source, after=shortened)
    ):
        return fallback
    candidate = f"{shortened}{suffix}"
    if not _fits_budget(candidate, options):
        if requires_exit_notice:
            return fallback
        return text
    return candidate


def _recovery_notices_for_text(
    text: str,
    metadata: dict[str, JsonValue],
    *,
    artifact_dir: Path | None,
    preserve_patterns: tuple[re.Pattern[str], ...] | None,
) -> list[str]:
    notice_metadata = metadata
    if (
        preserve_patterns is not None
        and _first_pattern_match(text, preserve_patterns) is not None
        and _has_nonrecoverable_artifact(metadata)
    ):
        notice_metadata = dict(metadata)
        notice_metadata.pop("artifact", None)
    return _recovery_notices(notice_metadata, artifact_dir=artifact_dir)


def _single_preserved_line_excerpt(
    text: str,
    options: NoisegateOptions,
    preserve_patterns: tuple[re.Pattern[str], ...] | None,
) -> str | None:
    if preserve_patterns is None:
        return None
    matches = _ranked_pattern_line_matches(text, preserve_patterns)
    if not matches:
        return None
    layout = _line_layout(text)
    for match in matches:
        line_index = match.line_index
        if line_index is None or line_index < 0 or line_index >= len(layout.lines):
            continue
        line = layout.lines[line_index]
        excerpt_lines = [line]
        omitted_before = line_index
        if (
            line_index > 0
            and re.search(r"\btraceback\b", layout.lines[line_index - 1], re.IGNORECASE)
            and re.search(r"\b[a-z0-9_]+error\b(?::|$)", line, re.IGNORECASE)
        ):
            excerpt_lines = [layout.lines[line_index - 1], line]
            omitted_before = line_index - 1
        omitted_after = len(layout.lines) - line_index - 1
        if omitted_before or omitted_after:
            marked_line = "\n".join(
                part
                for part in (
                    f"[noisegate: omitted {omitted_before} lines]" if omitted_before else "",
                    *excerpt_lines,
                    f"[noisegate: omitted {omitted_after} lines]" if omitted_after else "",
                )
                if part
            )
            if _fits_budget(marked_line, options):
                return marked_line
        excerpt = _line_centered_excerpt(text, options, match, layout=layout)
        if excerpt is not None:
            return excerpt
        if len(layout.lines) == 1 and _fits_budget(line, options):
            return line
    return None


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


def _strip_terminal_escape_sequences(
    text: str,
    *,
    preserve_osc_payload: bool = True,
) -> str:
    output: list[str] = []
    index = 0

    def consume_csi(start: int) -> int:
        cursor = start
        while cursor < len(text):
            final = text[cursor]
            cursor += 1
            if "@" <= final <= "~":
                break
        return cursor

    def consume_control_string(start: int, *, allow_bel: bool) -> tuple[int, int]:
        cursor = start
        while cursor < len(text):
            if text[cursor] == "\x9c" or (allow_bel and text[cursor] == "\x07"):
                return cursor + 1, cursor
            if text[cursor] == "\x1b" and cursor + 1 < len(text) and text[cursor + 1] == "\\":
                return cursor + 2, cursor
            cursor += 1
        return len(text), len(text)

    while index < len(text):
        char = text[index]
        if char in {"\x07", "\x9c"}:
            index += 1
            continue
        if char == "\x9b":
            index = consume_csi(index + 1)
            continue
        if char in {"\x90", "\x98", "\x9d", "\x9e", "\x9f"}:
            payload_start = index + 1
            index, payload_end = consume_control_string(
                payload_start,
                allow_bel=char == "\x9d",
            )
            if preserve_osc_payload:
                output.append(text[payload_start:payload_end])
            continue
        if char != "\x1b":
            if not "\x80" <= char <= "\x9f":
                output.append(char)
            index += 1
            continue
        if index + 1 >= len(text):
            break
        marker = text[index + 1]
        if marker == "[":
            index = consume_csi(index + 2)
            continue
        if marker in {"]", "P", "X", "^", "_"}:
            payload_start = index + 2
            index, payload_end = consume_control_string(
                payload_start,
                allow_bel=marker == "]",
            )
            if preserve_osc_payload:
                output.append(text[payload_start:payload_end])
            continue
        index += 2
    return "".join(output)


def _looks_credential_uri(text: str) -> bool:
    search_from = 0
    while True:
        separator = text.find("://", search_from)
        if separator < 0:
            return False
        scheme_start = separator
        while scheme_start > 0 and (
            text[scheme_start - 1].isalnum() or text[scheme_start - 1] in "+.-"
        ):
            scheme_start -= 1
        scheme = text[scheme_start:separator]
        if scheme and scheme[0].isalpha():
            authority_start = separator + 3
            authority_end = authority_start
            while authority_end < len(text) and text[authority_end] not in "\t\r\n /?#":
                authority_end += 1
            at = text.find("@", authority_start, authority_end)
            if at >= authority_start and text[authority_start:at]:
                return True
        search_from = separator + 3


def _looks_secret_env_assignment(text: str) -> bool:
    for match in SECRET_ENV_ASSIGNMENT_KEY.finditer(text):
        key = match.group(0).upper()
        if any(marker in key for marker in SECRET_ENV_KEY_MARKERS) or key.endswith(
            SECRET_ENV_KEY_SUFFIXES
        ):
            return True
    return False


def _looks_secret_bearing_text(text: str) -> bool:
    if any(
        (ord(char) < 32 and char not in {"\t", "\n", "\r"}) or char == "\x7f"
        for char in text
    ):
        return True
    normalized = _strip_terminal_escape_sequences(text)
    visible = _strip_terminal_escape_sequences(text, preserve_osc_payload=False)
    return any(
        _looks_credential_uri(candidate)
        or _looks_secret_env_assignment(candidate)
        or any(pattern.search(candidate) for pattern in SECRET_ARTIFACT_PATTERNS)
        for candidate in (text, normalized, visible)
    )


def _contains_command(command: str, names: tuple[str, ...]) -> bool:
    tokens = re.split(r"[\s;&|()]+", command)
    return any(name in tokens for name in names)


def _looks_like_memory_retrieval_command(command: str) -> bool:
    token_groups = _command_segments_after_wrappers(command)
    substitutions = _memory_retrieval_substitutions(command)
    if substitutions is None:
        return True
    for _kind, body in substitutions:
        substitution_tokens = _proven_reachable_substitution_tokens(body)
        token_groups.extend(
            substitution_tokens
            if substitution_tokens is not None
            else _command_segments_after_wrappers(body)
        )

    for tokens in token_groups:
        tokens = _memory_retrieval_execution_tokens(tokens)
        if not tokens:
            continue
        executable = Path(tokens[0]).name.lower()
        if executable in MEMORY_RETRIEVAL_HELPERS:
            return True
        if executable != "hermes":
            continue

        hermes_command = _hermes_top_level_command_tokens(tokens)
        if hermes_command is None or len(hermes_command) < 2:
            continue
        group = hermes_command[0].lower()
        action = hermes_command[1].lower()
        if group == "lcm":
            if action in {"grep", "load-session", "describe", "expand", "expand-query"}:
                return True
            if len(hermes_command) >= 3 and (action, hermes_command[2].lower()) in {
                ("load", "session"),
                ("expand", "query"),
            }:
                return True
        if (
            (group == "hindsight" and action in {"recall", "reflect"})
            or (
                group == "memory"
                and action
                in {"search", "recall", "reflect", "get", "read", "show", "list"}
            )
            or (group == "session" and action == "search")
        ):
            return True
    return False


def _hermes_top_level_command_tokens(tokens: list[str]) -> list[str] | None:
    """Return the Hermes command after the narrow set of recognized global options."""

    if not tokens or Path(tokens[0]).name.lower() != "hermes":
        return None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            return tokens[index:] or None
        if token in HERMES_VALUELESS_GLOBAL_OPTIONS:
            index += 1
            continue
        if token in {"--profile", "-p"}:
            if (
                index + 1 >= len(tokens)
                or HERMES_PROFILE_ID_PATTERN.fullmatch(tokens[index + 1]) is None
            ):
                return None
            index += 2
            continue
        if token.startswith("--profile="):
            if (
                HERMES_PROFILE_ID_PATTERN.fullmatch(token.removeprefix("--profile="))
                is None
            ):
                return None
            index += 1
            continue
        if token.startswith("-p") and token != "-p":
            if HERMES_PROFILE_ID_PATTERN.fullmatch(token[2:]) is None:
                return None
            index += 1
            continue
        if token in HERMES_REQUIRED_VALUE_GLOBAL_OPTIONS:
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("-"):
                return None
            index += 2
            continue
        long_value_option = next(
            (
                option
                for option in HERMES_REQUIRED_VALUE_GLOBAL_OPTIONS
                if option.startswith("--") and token.startswith(f"{option}=")
            ),
            None,
        )
        if long_value_option is not None:
            if not token.removeprefix(f"{long_value_option}="):
                return None
            index += 1
            continue
        attached_short_option = next(
            (
                option
                for option in HERMES_ATTACHED_SHORT_VALUE_OPTIONS
                if token.startswith(option) and token != option
            ),
            None,
        )
        if attached_short_option is not None:
            index += 1
            continue
        if token in HERMES_OPTIONAL_VALUE_GLOBAL_OPTIONS:
            index += 1
            if index < len(tokens) and not tokens[index].startswith("-"):
                index += 1
            continue
        if token.startswith("--continue="):
            if not token.removeprefix("--continue="):
                return None
            index += 1
            continue
        if token.startswith("-c") and token != "-c":
            index += 1
            continue
        if token.startswith("-"):
            return None
        return tokens[index:]
    return None


def _memory_retrieval_substitutions(command: str) -> list[tuple[str, str]] | None:
    """Collect executable nested substitutions within a bounded, deduplicated walk."""

    pending = [command]
    seen: set[str] = set()
    substitutions: list[tuple[str, str]] = []
    while pending:
        current = pending.pop()
        for kind, body in _command_substitutions(current):
            if kind == "arithmetic" or body in seen:
                continue
            if len(seen) >= _MEMORY_RETRIEVAL_SUBSTITUTION_LIMIT:
                return None
            seen.add(body)
            substitutions.append((kind, body))
            pending.append(body)
    return substitutions


def _memory_retrieval_redirection(
    tokens: list[str],
    index: int,
) -> tuple[int, bool] | None:
    """Extend simple redirects with an unquoted here-string for retrieval only."""

    if index < len(tokens):
        token = tokens[index]
        if not getattr(token, "redirection_operator_was_quoted", False):
            match = re.fullmatch(r"[0-9]*<<<(.*)", token)
            if match is not None:
                target = match.group(1)
                if target:
                    return 1, False
                if index + 1 >= len(tokens) or _is_unquoted_shell_separator(
                    tokens[index + 1]
                ):
                    return 0, False
                return 2, False
    return _simple_shell_redirection(tokens, index)


def _memory_retrieval_execution_tokens(tokens: list[str]) -> list[str]:
    normalized: list[str] = []
    index = 0
    while index < len(tokens):
        redirection = _memory_retrieval_redirection(tokens, index)
        if redirection is None:
            normalized.append(tokens[index])
            index += 1
            continue
        consumed, _supplies_stdin = redirection
        if not consumed:
            return []
        index += consumed

    normalized = _strip_command_wrappers(normalized)
    if not normalized or normalized[0] != "exec":
        return normalized
    index = 1
    while index < len(normalized):
        token = normalized[index]
        if token == "--":
            index += 1
            break
        if not token.startswith("-") or token == "-":
            break
        cluster = token[1:]
        argv0_index = cluster.find("a")
        option_cluster = cluster if argv0_index < 0 else cluster[:argv0_index]
        if not cluster or any(option not in {"c", "l"} for option in option_cluster):
            return []
        if argv0_index >= 0 and not cluster[argv0_index + 1 :]:
            index += 1
            if index >= len(normalized):
                return []
        index += 1
    return _strip_command_wrappers(normalized[index:])


def _is_pytest_command(command: str) -> bool:
    if _starts_command_name(command, {"pytest", "py.test"}):
        return True
    for tokens in _command_token_segments(command):
        stripped = _strip_command_wrappers(tokens)
        python_module = _python_module_invocation(stripped)
        if python_module is not None and python_module[0] in {"pytest", "py.test"}:
            return True
    return False


def _is_uv_pytest_resolution_failure(command: str, text: str) -> bool:
    uv_pytest = False
    for tokens in _command_segments_after_wrappers(command):
        if not tokens or Path(tokens[0]).name != "uv":
            continue
        payload = _command_runner_payload(tokens)
        if payload is not None and _is_pytest_command(shlex.join(payload)):
            uv_pytest = True
            break
    if not uv_pytest:
        return False
    if re.search(
        r"(?im)^={2,}.*(?:failures|errors|short test summary)|"
        r"^(?:FAILED|ERROR)\s+\S+|^\S+::.*\b(?:PASSED|FAILED|ERROR)\b|"
        r"^\s*\d+\s+(?:passed|failed|errors?)\b",
        text,
    ):
        return False
    return bool(
        re.search(
            r"^\s*(?:(?:\u00d7|error:)\s+)?(?:no solution found when resolving "
            r"(?:tool )?dependencies|failed to resolve dependencies)\b",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
    )


def _command_substitutions(command: str) -> list[tuple[str, str]]:
    substitutions: list[tuple[str, str]] = []
    quote: str | None = None
    escaped = False
    word_start = True
    index = 0
    while index < len(command) - 1:
        char = command[index]
        if escaped:
            escaped = False
            if char not in "\n\r":
                word_start = False
            index += 1
            continue
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if quote == '"':
            if char == '"':
                quote = None
                index += 1
                continue
        elif char in {"'", '"'}:
            quote = char
            word_start = False
            index += 1
            continue
        if quote is None and char == "#" and word_start:
            newline = command.find("\n", index)
            if newline < 0:
                break
            index = newline + 1
            word_start = True
            continue
        if char == "`":
            scan = index + 1
            body_chars: list[str] = []
            inner_escaped = False
            while scan < len(command):
                current = command[scan]
                if inner_escaped:
                    body_chars.append(current)
                    inner_escaped = False
                    scan += 1
                    continue
                if current == "\\":
                    if scan + 1 < len(command) and command[scan + 1] == "`":
                        body_chars.append("`")
                        scan += 2
                        continue
                    body_chars.append(current)
                    inner_escaped = True
                    scan += 1
                    continue
                if current == "`":
                    substitutions.append(("command", "".join(body_chars)))
                    index = scan
                    break
                body_chars.append(current)
                scan += 1
            word_start = False
        if (
            char == "$" or (quote is None and char in {"<", ">"})
        ) and command[index + 1] == "(":
            depth = 1
            body_start = index + 2
            scan = body_start
            inner_quote: str | None = None
            inner_escaped = False
            inner_comment = False
            inner_word_start = True
            while scan < len(command):
                current = command[scan]
                if inner_comment:
                    if current not in "\n\r":
                        scan += 1
                        continue
                    inner_comment = False
                    inner_word_start = True
                if inner_escaped:
                    inner_escaped = False
                    if current not in "\n\r":
                        inner_word_start = False
                    scan += 1
                    continue
                if current == "\\":
                    inner_escaped = True
                    scan += 1
                    continue
                if inner_quote:
                    if current == inner_quote:
                        inner_quote = None
                    scan += 1
                    continue
                if current in {"'", '"'}:
                    inner_quote = current
                    inner_word_start = False
                    scan += 1
                    continue
                if current == "#" and inner_word_start:
                    inner_comment = True
                    scan += 1
                    continue
                if current == "(":
                    depth += 1
                elif current == ")":
                    depth -= 1
                    if depth == 0:
                        kind = "command"
                        if char in {"<", ">"}:
                            kind = "process"
                        elif command[index + 2 : index + 3] == "(":
                            kind = "arithmetic"
                        substitutions.append((kind, command[body_start:scan]))
                        index = scan
                        break
                inner_word_start = current.isspace() or current in ";&|()<>"
                scan += 1
            word_start = False
        elif quote is None:
            word_start = char.isspace() or char in ";&|()<>"
        index += 1
    return substitutions


def _unquoted_shell_lines(command: str) -> list[str]:
    lines: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and quote != "'":
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            current.append(char)
            quote = char
            continue
        if char in "\n\r":
            line = "".join(current).strip()
            if line:
                lines.append(line)
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        lines.append(tail)
    return lines


def _proven_reachable_substitution_tokens(body: str) -> list[list[str]] | None:
    """Evaluate only simple top-level true/false AND-OR lists."""
    if not body.strip() or any(char in body for char in "'\"`\\#{}"):
        return None
    substitutions = _command_substitutions(body)
    masked_body = body
    markers: list[str] = []
    for index, (kind, nested) in enumerate(substitutions):
        if kind == "arithmetic":
            return None
        candidates = (
            [f"$({nested})"]
            if kind == "command"
            else [f"<({nested})", f">({nested})"]
        )
        matches = [
            (masked_body.find(candidate), candidate)
            for candidate in candidates
            if masked_body.find(candidate) >= 0
        ]
        if not matches:
            return None
        start, matched = min(matches)
        marker = f"__noisegate_nested_{index}__"
        masked_body = (
            f"{masked_body[:start]}{marker}{masked_body[start + len(matched):]}"
        )
        markers.append(marker)
    if "$" in masked_body or any(char in masked_body for char in "()"):
        return None
    if any(
        re.search(r"(?:&&|\|\||[;|&<>(){}\r\n])", nested)
        for _kind, nested in substitutions
    ):
        return None

    logical_lines: list[str] = []
    pending = ""
    for line in masked_body.splitlines():
        pending = f"{pending} {line.strip()}".strip()
        if re.search(r"(?:&&|\|\|)\s*$", pending):
            continue
        if pending:
            logical_lines.append(pending)
        pending = ""
    if pending or not logical_lines:
        return None
    normalized = " ; ".join(logical_lines)
    if re.search(r"(?:&&|\|\||[;&])\s*$", normalized):
        return None
    tokens = _shell_tokens(normalized)
    if any(token in {"|", "|&", "&", "{", "}"} for token in tokens):
        return None
    for index, token in enumerate(tokens):
        if "<" not in token and ">" not in token:
            continue
        if token in {"<", ">"} and index + 1 < len(tokens) and tokens[index + 1] == "(":
            continue
        return None

    reachable: list[list[str]] = []
    statuses: set[bool] = set()
    for separator, command_tokens in _background_segments(normalized):
        if not command_tokens:
            return None
        if command_tokens == ["true"] or command_tokens == [":"]:
            command_statuses = {True}
        elif command_tokens == ["false"]:
            command_statuses = {False}
        else:
            command_statuses = {True, False}

        if separator in {None, ";"}:
            runs = True
            statuses = command_statuses
        elif separator == "&&":
            runs = True in statuses
            statuses = ({False} if False in statuses else set()) | (
                command_statuses if runs else set()
            )
        elif separator == "||":
            runs = False in statuses
            statuses = ({True} if True in statuses else set()) | (
                command_statuses if runs else set()
            )
        else:
            return None
        if not runs:
            continue
        if any(token in {"(", ")"} for token in command_tokens):
            return None
        reachable.append(command_tokens)
    if any(marker in token for command in reachable for token in command for marker in markers):
        return None
    return reachable


def _process_substitution_compactable_class(command: str, sample: str, text: str) -> str | None:
    fallback_class: str | None = None
    for kind, body in _command_substitutions(command):
        token_groups = (
            _proven_reachable_substitution_tokens(body) if kind == "command" else None
        )
        if token_groups is None:
            nested_class = _process_substitution_compactable_class(body, sample, text)
            if nested_class is not None:
                if nested_class != "log_stream":
                    return nested_class
                fallback_class = nested_class
            token_groups = [_shell_tokens(body)]
            token_groups.extend(
                tokens
                for line in _unquoted_shell_lines(body)
                for tokens in (_shell_tokens(line), *_command_token_segments(line))
            )
        for tokens in token_groups:
            command_class = _compactable_output_class_for_tokens(tokens, sample, text)
            if command_class is not None:
                if command_class != "log_stream":
                    return command_class
                fallback_class = command_class
            command_class = _compactable_class_for_tokens(tokens, sample, text)
            if command_class is not None:
                if command_class != "log_stream":
                    return command_class
                fallback_class = command_class
    return fallback_class


def _substitutions_are_ordinary(command: str) -> bool:
    substitutions = _command_substitutions(command)
    if not substitutions:
        return False
    for kind, body in substitutions:
        if kind != "command" or not body.strip():
            return False
        if _proven_reachable_substitution_tokens(body) is not None:
            continue
        if _command_substitutions(body) and not _substitutions_are_ordinary(body):
            return False
    return True


def _command_has_process_substitution(command: str) -> bool:
    for kind, body in _command_substitutions(command):
        if kind == "process":
            return True
        if kind == "command" and _proven_reachable_substitution_tokens(body) is not None:
            continue
        if _command_has_process_substitution(body):
            return True
    return False


def _has_active_process_substitution(command: str) -> bool:
    return any(_command_has_process_substitution(v) for v in _command_intent_variants(command))


def _direct_noncompactable_substitution_tokens(
    command: str,
    sample: str,
    text: str,
) -> list[str] | None:
    segments = _background_segments(command)
    if len(segments) != 1 or segments[0][0] is not None:
        return None
    tokens = segments[0][1]
    if (
        not tokens
        or Path(tokens[0]).name not in SOURCE_READ_COMMANDS | SOURCE_SEARCH_COMMANDS
        or any(token in {"|", "|&", "||", "&&", ";", "&", "{", "}"} for token in tokens)
        or _tokens_redirect_stdout(tokens)
        or not _substitutions_are_ordinary(command)
        or _process_substitution_compactable_class(command, sample, text) is not None
    ):
        return None
    return tokens


def _tokens_show_command_substitution(tokens: list[str]) -> bool:
    return any("$(" in token or "`" in token for token in tokens) or any(
        token == "$" and index + 1 < len(tokens) and tokens[index + 1] == "("
        for index, token in enumerate(tokens)
    )


def _contains_process_substitution_pytest(command: str) -> bool:
    return _process_substitution_compactable_class(command, "", "") == "pytest"


def _is_unittest_command(command: str) -> bool:
    return "unittest" in command or _starts_command_name(command, {"unittest"})


def _starts_command_name(command: str, names: set[str]) -> bool:
    if not command:
        return False
    for tokens in _command_token_segments(command):
        stripped = _strip_command_wrappers(tokens)
        if not stripped:
            continue
        command_name = Path(stripped[0]).name
        if command_name in names:
            return True
    return False


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


def _leading_exact_output_owns_before_only_or_fallbacks(
    segments: list[tuple[str | None, list[str]]],
    text: str,
    *,
    exit_code: int | None,
    expected_class: str,
) -> bool:
    if exit_code != 0:
        return False
    meaningful_indices = [
        index
        for index, (_separator, tokens) in enumerate(segments)
        if tokens and not _is_setup_segment(tokens)
    ]
    if len(meaningful_indices) < 2:
        return False
    first_index = meaningful_indices[0]
    first_tokens = segments[first_index][1]
    if _exact_class_for_tokens(first_tokens) != expected_class:
        return False

    group_depth = first_tokens.count("{") + first_tokens.count("(")
    group_depth = max(0, group_depth - first_tokens.count("}") - first_tokens.count(")"))
    for separator, tokens in segments[first_index + 1 :]:
        if group_depth == 0 and separator != "||":
            return False
        group_depth += tokens.count("{") + tokens.count("(")
        group_depth = max(0, group_depth - tokens.count("}") - tokens.count(")"))
    if group_depth != 0:
        return False
    return _text_has_exact_owner_output_against_later_compactable(
        first_tokens,
        text,
        exit_code=exit_code,
    )


def _earlier_compactable_output_blocks_first_file_read(
    segments: list[tuple[str | None, list[str]]],
    sample: str,
    text: str,
    *,
    exit_code: int | None,
) -> bool:
    earlier_compactable_output = False
    for index, (_separator, tokens) in enumerate(segments):
        if not tokens or _is_setup_segment(tokens):
            continue
        if _tokens_start_file_read(tokens) or _tokens_pipe_to_file_read(tokens):
            return earlier_compactable_output and not (
                _text_has_exact_owner_output_against_later_compactable(
                    tokens,
                    text,
                    exit_code=exit_code,
                )
            )
        effective_tokens = _segment_tokens_with_enclosing_group_redirects(segments, index)
        if (
            _compactable_output_dominates_for_tokens(tokens, sample, text)
            and _compactable_segment_can_contribute_output(effective_tokens, text)
        ):
            earlier_compactable_output = True
    return False


def _looks_like_file_read_command(
    command: str,
    *,
    sample: str = "",
    text: str = "",
    exit_code: int | None = None,
) -> bool:
    if not command or _has_suspicious_shell_quote_escape(command):
        return False
    if _has_active_process_substitution(command):
        return False
    segments = _background_segments(command)
    if _leading_exact_output_owns_before_only_or_fallbacks(
        segments,
        text,
        exit_code=exit_code,
        expected_class="file_read",
    ):
        return True
    if _earlier_compactable_output_blocks_first_file_read(
        segments,
        sample,
        text,
        exit_code=exit_code,
    ):
        return False
    for substitution_command in (command, *_command_intent_variants(command)):
        if _process_substitution_compactable_class(substitution_command, sample, text) is not None:
            return False
    dynamic_tokens = _direct_noncompactable_substitution_tokens(command, sample, text)
    if dynamic_tokens is not None and _tokens_start_file_read(dynamic_tokens):
        return True
    has_command_substitution = bool(_command_substitutions(command))
    if (
        _has_unsafe_shell_expansion(command)
        and not _has_only_safe_file_read_redirection(command)
        and len(segments) <= 1
    ):
        if not _has_unquoted_command_or_process_substitution(command):
            return False
        if not (
            _contains_likely_file_read_output(text) or _starts_like_file_read_output(text)
        ):
            return False
    if _pipeline_compactable_class(command, sample, text) is not None:
        return False
    if _pipeline_xargs_compactable_class(command, sample, text) is not None:
        return False
    prior_compactable = False
    prior_compactable_output = False
    prior_compactable_signal = False
    for index, (separator, tokens) in enumerate(segments):
        if _is_setup_segment(tokens):
            continue
        stripped_tokens = _strip_command_wrappers(tokens)
        if stripped_tokens and Path(stripped_tokens[0]).name in {"bash", "sh", "zsh"}:
            shell_command = _shell_c_argument(stripped_tokens[1:])
            if shell_command is not None:
                if _looks_like_file_read_command(
                    shell_command,
                    sample=sample,
                    text=text,
                    exit_code=exit_code,
                ):
                    if separator == "&&" and prior_compactable and exit_code not in {None, 0}:
                        continue
                    if _prior_compactable_output_blocks_exact_tail(
                        separator,
                        prior_compactable,
                        prior_compactable_output,
                        prior_compactable_signal,
                        exit_code,
                    ) and not _text_has_exact_owner_output_against_later_compactable(
                        tokens,
                        text,
                    ):
                        continue
                    if separator == "||" and prior_compactable:
                        if exit_code not in {None, 0}:
                            continue
                        if prior_compactable_output or prior_compactable_signal:
                            continue
                    raw_later_segments = segments[index + 1 :]
                    later_segments = [
                        (later_separator, segment)
                        for later_separator, segment in raw_later_segments
                        if segment and not _is_setup_segment(segment)
                    ]
                    if raw_later_segments:
                        if _shell_starts_fd_attached_file_read(
                            shell_command
                        ) and _fd_later_compactable_output_ran(
                            raw_later_segments,
                            sample,
                            text,
                            exit_code,
                            initial_statuses=_shell_fd_owner_statuses(shell_command),
                        ):
                            return False
                        if not later_segments:
                            return True
                        if (
                            exit_code == 0
                            and later_segments[0][0] == "||"
                            and (
                                _or_fallback_exact_left_succeeded(tokens, text)
                                or _or_fallback_branch_terminates_current_shell(later_segments)
                            )
                            and not _or_tail_has_unconditional_compactable_after_fallback(
                                later_segments,
                                sample,
                                text,
                            )
                        ):
                            return True
                        if _or_later_exact_fallback_succeeded(
                            later_segments,
                            sample,
                            text,
                            exit_code,
                        ):
                            return True
                        if _later_compactable_output_dominates(
                            later_segments,
                            sample,
                            text,
                            exit_code,
                        ) and not _text_has_exact_owner_output_against_later_compactable(
                            tokens,
                            text,
                        ):
                            return False
                        return not any(
                            (later_separator == "&" or prior_compactable)
                            and _compactable_class_for_tokens(segment, sample, text) is not None
                            for later_separator, segment in later_segments
                        )
                    return True
                if _compactable_class_for_tokens(tokens, sample, text) is not None:
                    prior_compactable = True
                    if _compactable_output_class_for_tokens(tokens, sample, text) is not None:
                        prior_compactable_signal = True
                    if _compactable_output_dominates_for_tokens(tokens, sample, text):
                        prior_compactable_output = True
                continue
        if _tokens_have_unsafe_shell_expansion(
            tokens
        ) and _has_unquoted_command_or_process_substitution(command):
            if _compactable_class_for_tokens(tokens, sample, text) is not None:
                prior_compactable = True
                if _compactable_output_class_for_tokens(tokens, sample, text) is not None:
                    prior_compactable_signal = True
                if _compactable_output_dominates_for_tokens(tokens, sample, text):
                    prior_compactable_output = True
                continue
            if not (
                _contains_likely_file_read_output(text)
                or _starts_like_file_read_output(text)
                or _contains_likely_source_search_output(text)
                or _contains_multiple_likely_source_search_lines(text)
            ):
                continue
        if (
            len(segments) > 1
            and has_command_substitution
            and _tokens_show_command_substitution(tokens)
            and not (
                _contains_likely_file_read_output(text)
                or _starts_like_file_read_output(text)
                or _contains_likely_source_search_output(text)
                or _contains_multiple_likely_source_search_lines(text)
            )
        ):
            continue
        if not _tokens_start_file_read(tokens):
            if _compactable_class_for_tokens(tokens, sample, text) is not None:
                prior_compactable = True
                if _compactable_output_class_for_tokens(tokens, sample, text) is not None:
                    prior_compactable_signal = True
                if _compactable_output_dominates_for_tokens(tokens, sample, text):
                    prior_compactable_output = True
            continue
        if separator == "&&" and prior_compactable and exit_code not in {None, 0}:
            continue
        if separator == "&" and prior_compactable_signal:
            continue
        if _prior_compactable_output_blocks_exact_tail(
            separator,
            prior_compactable,
            prior_compactable_output,
            prior_compactable_signal,
            exit_code,
        ) and not _text_has_exact_owner_output_for_tokens(tokens, text):
            continue
        if separator == "||" and prior_compactable:
            if exit_code not in {None, 0}:
                continue
            if prior_compactable_output and not _text_has_exact_owner_output_for_tokens(
                tokens,
                text,
            ):
                continue
        raw_later_segments = segments[index + 1 :]
        later_segments = [
            (later_separator, segment)
            for later_separator, segment in raw_later_segments
            if segment and not _is_setup_segment(segment)
        ]
        if raw_later_segments and _tokens_start_fd_attached_file_read(
            tokens
        ) and _fd_later_compactable_output_ran(
            raw_later_segments,
            sample,
            text,
            exit_code,
        ):
            return False
        if (
            later_segments
            and any(later_separator == "||" for later_separator, _ in later_segments)
            and _contains_file_read_error(text)
        ):
            for fallback_index, (later_separator, segment) in enumerate(later_segments):
                if not (
                    later_separator == "||"
                    and (_tokens_start_file_read(segment) or _tokens_pipe_to_file_read(segment))
                    and not _contains_file_read_error_for_tokens(segment, text)
                    and _contains_likely_file_read_output(text)
                ):
                    continue
                if exit_code == 0 and all(
                    following_separator == "||"
                    for following_separator, _ in later_segments[fallback_index + 1 :]
                ):
                    return True
                if _later_compactable_output_dominates(
                    later_segments[fallback_index + 1 :],
                    sample,
                    text,
                    exit_code,
                ):
                    continue
                return True
            for later_separator, segment in later_segments:
                if later_separator not in {";", "&&"}:
                    continue
                if later_separator == "&&" and exit_code not in {None, 0}:
                    continue
                if not (_tokens_start_file_read(segment) or _tokens_pipe_to_file_read(segment)):
                    continue
                if _contains_file_read_error_for_tokens(segment, text):
                    continue
                if _contains_likely_file_read_output(text) or _starts_like_file_read_output(text):
                    return True
            if _later_compactable_output_dominates(
                later_segments,
                sample,
                text,
                exit_code,
            ):
                return False
            return any(
                later_separator == "||"
                and _exact_fallback_segment_owns_output(segment, text)
                for later_separator, segment in later_segments
            )
        if later_segments:
            if (
                exit_code == 0
                and later_segments[0][0] == "||"
                and (
                    _or_fallback_exact_left_succeeded(tokens, text)
                    or _or_fallback_branch_terminates_current_shell(later_segments)
                )
                and not _or_tail_has_unconditional_compactable_after_fallback(
                    later_segments,
                    sample,
                    text,
                )
            ):
                return True
            if _hidden_compactable_tail_preserves_exact_output(
                tokens,
                later_segments,
                sample,
                text,
            ):
                return True
            if _visible_failed_test_tail_owns_output(
                later_segments,
                sample,
                text,
                exit_code,
            ):
                return False
            if _later_compactable_output_dominates(
                later_segments,
                sample,
                text,
                exit_code,
            ) and not _text_has_exact_owner_output_against_later_compactable(
                tokens,
                text,
                exit_code=exit_code,
            ):
                return False
            return not any(
                (later_separator == "&" or prior_compactable)
                and _compactable_class_for_tokens(segment, sample, text) is not None
                for later_separator, segment in later_segments
            )
        return True
    return False


def _has_unsafe_shell_expansion(command: str) -> bool:
    quote: str | None = None
    escaped = False
    comment = False
    word_start = True
    for index, char in enumerate(command):
        if comment:
            if char not in "\n\r":
                continue
            comment = False
            word_start = True
        if escaped:
            escaped = False
            if char not in "\n\r":
                word_start = False
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
            word_start = False
            continue
        if char == "#" and word_start:
            comment = True
            continue
        if char in "><`\n\r" or (char == "$" and command[index + 1 : index + 2] == "("):
            return True
        word_start = char.isspace() or char in ";&|()<>"
    return quote is not None


def _has_unquoted_command_or_process_substitution(command: str) -> bool:
    quote: str | None = None
    escaped = False
    comment = False
    word_start = True
    for index, char in enumerate(command):
        if comment:
            if char not in "\n\r":
                continue
            comment = False
            word_start = True
        if escaped:
            escaped = False
            if char not in "\n\r":
                word_start = False
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
            word_start = False
            continue
        if char == "#" and word_start:
            comment = True
            continue
        if char == "`" or (char in {"$", "<", ">"} and command[index + 1 : index + 2] == "("):
            return True
        word_start = char.isspace() or char in ";&|()<>"
    return quote is not None


def _has_only_safe_file_read_redirection(command: str) -> bool:
    """Allow valid redirections only when captured stdout remains visible."""

    if (
        _has_unquoted_command_or_process_substitution(command)
        or "\n" in command
        or "\r" in command
    ):
        return False
    tokens = _shell_tokens(command)
    stdout_visible, _stderr_visible = _redirected_stream_visibility(tokens)
    if not stdout_visible:
        return False
    index = 0
    saw_safe_redirection = False
    while index < len(tokens):
        redirection = _simple_shell_redirection(tokens, index)
        if redirection is None:
            index += 1
            continue
        consumed, _supplies_stdin_path = redirection
        if not consumed:
            return False
        saw_safe_redirection = True
        index += consumed
    return saw_safe_redirection


def _has_suspicious_shell_quote_escape(command: str) -> bool:
    return bool(re.search(r"['\"][^'\"]*\\['\"]\s*[;&|]", command))


def _looks_like_sed_search_script(token: str) -> bool:
    return bool(re.fullmatch(r"/.+/p", token.strip()))


def _simple_shell_redirection(tokens: list[str], index: int) -> tuple[int, bool] | None:
    """Return (tokens consumed, supplies stdin path) for a simple shell redirect."""

    if index >= len(tokens):
        return None
    token = tokens[index]
    if getattr(token, "redirection_operator_was_quoted", False):
        return None
    if re.fullmatch(r"[0-9]*[<>]&(?:[0-9]+|-)", token):
        return 1, False
    for operator in ("&>>", "&>"):
        if not token.startswith(operator):
            continue
        if token[len(operator) :]:
            return 1, False
        if index + 1 >= len(tokens) or _is_unquoted_shell_separator(tokens[index + 1]):
            return 0, False
        return 2, False

    match = re.fullmatch(r"([0-9]*)(<>|>>?|>\||<)(.*)", token)
    if match is None:
        return None
    descriptor, operator, target = match.groups()
    if target.startswith(("&", "<", ">")):
        return 0, False
    consumed = 1
    target_path = target
    if not target:
        if index + 1 >= len(tokens) or _is_unquoted_shell_separator(tokens[index + 1]):
            return 0, False
        consumed = 2
        target_path = tokens[index + 1]
    supplies_stdin_path = (
        operator in {"<", "<>"}
        and descriptor in {"", "0"}
        and target_path not in {"/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"}
    )
    return consumed, supplies_stdin_path


def _sed_tokens_have_file_input(tokens: list[str]) -> bool:
    stdin_aliases = {"-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"}
    index = 0
    while index < len(tokens):
        redirection = _simple_shell_redirection(tokens, index)
        if redirection is not None:
            consumed, supplies_stdin_path = redirection
            if not consumed:
                return False
            if supplies_stdin_path:
                return True
            index += consumed
            continue
        if _is_unquoted_shell_separator(tokens[index]):
            return False
        if tokens[index] not in stdin_aliases:
            return True
        index += 1
    return False


def _shell_operand_after_redirections(
    tokens: list[str],
    index: int,
) -> tuple[int, bool] | None:
    supplies_stdin_path = False
    while index < len(tokens):
        redirection = _simple_shell_redirection(tokens, index)
        if redirection is None:
            break
        consumed, redirect_supplies_stdin = redirection
        if not consumed:
            return None
        supplies_stdin_path |= redirect_supplies_stdin
        index += consumed
    if index >= len(tokens) or _is_unquoted_shell_separator(tokens[index]):
        return None
    return index, supplies_stdin_path


def _looks_like_sed_file_read_tokens(tokens: list[str]) -> bool:
    if not tokens or Path(tokens[0]).name != "sed":
        return False
    saw_script = False
    has_file_arg = False
    index = 1
    valueless_long_options = {
        "--follow-symlinks",
        "--null-data",
        "--posix",
        "--quiet",
        "--regexp-extended",
        "--sandbox",
        "--separate",
        "--silent",
        "--unbuffered",
    }
    while index < len(tokens):
        token = tokens[index]
        redirection = _simple_shell_redirection(tokens, index)
        if redirection is not None:
            consumed, supplies_stdin_path = redirection
            if not consumed:
                return False
            has_file_arg |= supplies_stdin_path
            index += consumed
            continue
        if token == "--":
            remaining = tokens[index + 1 :]
            if not saw_script:
                if not remaining or _looks_like_sed_search_script(remaining[0]):
                    return False
                saw_script = True
                remaining = remaining[1:]
            has_file_arg |= _sed_tokens_have_file_input(remaining)
            break
        long_option, separator, attached_value = token.partition("=")
        if long_option.startswith("--i") and "--in-place".startswith(long_option):
            return False
        if long_option.startswith("--e") and "--expression".startswith(long_option):
            if separator:
                script = attached_value
                consumed = 1
            elif index + 1 < len(tokens):
                operand = _shell_operand_after_redirections(tokens, index + 1)
                if operand is None:
                    return False
                operand_index, supplies_stdin_path = operand
                script = tokens[operand_index]
                has_file_arg |= supplies_stdin_path
                consumed = operand_index - index + 1
            else:
                return False
            if _looks_like_sed_search_script(script):
                return False
            saw_script = True
            index += consumed
            continue
        if long_option.startswith("--l") and "--line-length".startswith(long_option):
            if separator:
                line_length = attached_value
                index += 1
            elif index + 1 < len(tokens):
                operand = _shell_operand_after_redirections(tokens, index + 1)
                if operand is None:
                    return False
                operand_index, supplies_stdin_path = operand
                line_length = tokens[operand_index]
                has_file_arg |= supplies_stdin_path
                index = operand_index + 1
            else:
                return False
            if re.fullmatch(r"[+-]?\d+", line_length.strip()) is None:
                return False
            continue
        if long_option.startswith("--fi") and "--file".startswith(long_option):
            if separator:
                if not attached_value:
                    return False
                saw_script = True
                index += 1
                continue
            if index + 1 >= len(tokens):
                return False
            operand = _shell_operand_after_redirections(tokens, index + 1)
            if operand is None:
                return False
            operand_index, supplies_stdin_path = operand
            has_file_arg |= supplies_stdin_path
            saw_script = True
            index = operand_index + 1
            continue
        if token in {"--help", "--version"}:
            return False
        if token in valueless_long_options:
            index += 1
            continue
        if token.startswith("-") and not token.startswith("--"):
            short_options = token[1:]
            next_index = index + 1
            option_index = 0
            while option_index < len(short_options):
                option = short_options[option_index]
                if option in {"i", "I"}:
                    return False
                if option == "l":
                    operand = short_options[option_index + 1 :]
                    if re.fullmatch(r"[+-]?\d+", operand):
                        break
                    if not operand and index + 1 < len(tokens):
                        shell_operand = _shell_operand_after_redirections(tokens, index + 1)
                        if shell_operand is not None:
                            operand_index, supplies_stdin_path = shell_operand
                            candidate = tokens[operand_index]
                            if re.fullmatch(r"[+-]?\d+", candidate.strip()):
                                has_file_arg |= supplies_stdin_path
                                next_index = operand_index + 1
                                break
                    option_index += 1
                    continue
                if option in {"e", "f"}:
                    operand = short_options[option_index + 1 :]
                    if not operand:
                        if index + 1 >= len(tokens):
                            return False
                        shell_operand = _shell_operand_after_redirections(tokens, index + 1)
                        if shell_operand is None:
                            return False
                        operand_index, supplies_stdin_path = shell_operand
                        operand = tokens[operand_index]
                        has_file_arg |= supplies_stdin_path
                        next_index = operand_index + 1
                    if option == "e" and _looks_like_sed_search_script(operand):
                        return False
                    if option == "e" or operand:
                        saw_script = True
                    break
                if option not in {"E", "H", "a", "b", "n", "r", "s", "u", "z"}:
                    return False
                option_index += 1
            index = next_index
            continue
        if token.startswith("--"):
            return False
        if not saw_script:
            if _looks_like_sed_search_script(token):
                return False
            saw_script = True
        else:
            has_file_arg |= _sed_tokens_have_file_input([token])
        index += 1
    return saw_script and has_file_arg


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
        rest = _skip_option_tokens(
            tokens[1:],
            valueless_options={"-f", "--fix-broken"},
        )
        if rest and rest[0] in {
            "update",
            "install",
            "upgrade",
            "dist-upgrade",
            "full-upgrade",
        }:
            return True
    return False


def _is_patch_command(command: str) -> bool:
    return any(
        tokens and Path(tokens[0]).name in {"apply_patch", "patch"}
        for tokens in _command_segments_after_wrappers(command)
    )


def _tail_follows(tokens: list[str]) -> bool:
    for token in tokens:
        if token == "--":
            return False
        if token == "--follow" or token.startswith("--follow="):
            return True
        if token.startswith("-") and not token.startswith("--") and "f" in token[1:].lower():
            return True
    return False


def _tail_operand_tokens(tokens: list[str]) -> list[str] | None:
    value_options = {
        "-c",
        "--bytes",
        "-n",
        "--lines",
        "--max-unchanged-stats",
        "--pid",
        "-s",
        "--sleep-interval",
    }
    flag_options = {"-f", "-F", "-q", "-v", "-z", "--follow", "--retry", "--zero-terminated"}
    operands: list[str] = []
    index = 0
    options_done = False
    while index < len(tokens):
        token = tokens[index]
        if _is_unquoted_shell_separator(token):
            return None
        redirection = _simple_shell_redirection(tokens, index)
        if redirection is not None:
            consumed, _ = redirection
            if not consumed:
                return None
            index += consumed
            continue
        if options_done or not token.startswith("-") or token == "-":
            operands.append(token)
            index += 1
            continue
        if token == "--":
            options_done = True
            index += 1
            continue
        option, separator, value = token.partition("=")
        if option in value_options and separator:
            if not value:
                return None
            index += 1
            continue
        if token in value_options:
            if index + 1 >= len(tokens) or not tokens[index + 1]:
                return None
            index += 2
            continue
        if token in flag_options or (token.startswith("--follow=") and token != "--follow="):
            index += 1
            continue
        if token.startswith("--"):
            return None
        position = 1
        consumes_next = False
        while position < len(token):
            short_option = token[position]
            if short_option in {"f", "F", "q", "v", "z"}:
                position += 1
                continue
            if short_option in {"c", "n", "s"}:
                consumes_next = position + 1 == len(token)
                break
            return None
        if consumes_next:
            if index + 1 >= len(tokens) or not tokens[index + 1]:
                return None
            index += 2
        else:
            index += 1
    return operands


def _tail_follow_targets_are_log_like(tokens: list[str]) -> bool:
    operands = _tail_operand_tokens(tokens)
    if operands is None:
        return False
    if not operands:
        return True
    known_log_names = {"syslog", "messages"}
    known_log_suffixes = {".err", ".log", ".out"}
    for operand in operands:
        if operand == "-":
            continue
        normalized = operand.replace("\\", "/").lower()
        path = Path(normalized)
        if (
            normalized.startswith("/var/log/")
            or "/var/log/" in normalized
            or path.name in known_log_names
            or path.suffix in known_log_suffixes
        ):
            continue
        return False
    return True


def _is_systemctl_show_command(command: str) -> bool:
    for tokens in _command_segments_after_wrappers(command):
        if not tokens or Path(tokens[0]).name.lower() != "systemctl":
            continue
        action = _skip_option_tokens(tokens[1:])
        if action and action[0].lower() == "show":
            return True
    return False


def _is_log_stream_command(command: str) -> bool:
    if any(
        token in {"|", "|&"} and not getattr(token, "was_quoted", False)
        for token in _shell_tokens(command)
    ):
        return False
    for tokens in _command_segments_after_wrappers(command):
        if not tokens:
            continue
        command_name = Path(tokens[0]).name.lower()
        rest = tokens[1:]
        if command_name == "process":
            action = _skip_option_tokens(rest)
            if action and action[0].lower() in {"log", "poll", "wait"}:
                return True
            continue
        if command_name in {"dmesg", "journalctl"}:
            return True
        if command_name == "systemctl":
            action = _skip_option_tokens(rest)
            if action and action[0].lower() == "status":
                return True
            continue
        if (
            command_name == "tail"
            and _tail_follows(rest)
            and _tail_follow_targets_are_log_like(rest)
        ):
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
        if rest[0] == "service":
            service_rest = _skip_option_tokens(rest[1:])
            if service_rest and service_rest[0] == "logs":
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
        or re.search(r"(?im)(?:^|\|)\s*#\d+\s+.*\bdockerfile\b", sample)
        or re.search(r"(?im)(?:^|\|)\s*=>\s+.*\bdockerfile\b", sample)
        or re.search(
            r"(?im)(?:^|\|)\s*(?:#\d+|=>)\s+.*\b"
            r"(?:load metadata|load \.dockerignore|exporting|writing image)\b",
            sample,
        )
        or re.search(r"(?im)(?:^|\|)\s*(?:#\d+|=>)\s+(?:done|cached)\b", sample)
        or re.search(r"(?im)(?:^|\|)\s*(?:#\d+|=>)\s+.*\b(?:done|cached)\b", sample)
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
    value_options = {"-c", "-w", "-x"}
    while index < len(tokens):
        token = tokens[index]
        token_l = token.lower()
        if token_l == "-m" and index + 1 < len(tokens):
            return tokens[index + 1].lower(), tokens[index + 2 :]
        if token_l.startswith("-m") and len(token) > 2:
            return token[2:].lower(), tokens[index + 1 :]
        if token in SHELL_SEPARATORS or not token.startswith("-"):
            return None
        option_name = token_l.split("=", 1)[0]
        if option_name == "-c" or token_l.startswith("-c"):
            return None
        if (
            option_name in value_options
            or any(token_l.startswith(option) and token_l != option for option in value_options)
        ):
            index += 1
            if (
                option_name in value_options
                and "=" not in token
                and token_l == option_name
                and index < len(tokens)
            ):
                index += 1
            continue
        if token.startswith("-") and not token.startswith("--") and "m" in token_l[1:]:
            module_suffix = token_l.split("m", 1)[1]
            if module_suffix:
                return module_suffix, tokens[index + 1 :]
            if index + 1 < len(tokens):
                return tokens[index + 1].lower(), tokens[index + 2 :]
            return None
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


def _is_source_search_command(
    command: str,
    *,
    sample: str = "",
    text: str = "",
    exit_code: int | None = None,
) -> bool:
    if not command:
        return False
    if _has_active_process_substitution(command):
        return False
    segments = _background_segments(command)
    if _leading_exact_output_owns_before_only_or_fallbacks(
        segments,
        text,
        exit_code=exit_code,
        expected_class="source_search",
    ):
        return True
    dynamic_tokens = _direct_noncompactable_substitution_tokens(command, sample, text)
    if (
        dynamic_tokens is not None
        and _tokens_start_source_search(dynamic_tokens)
        and _tokens_source_search_hides_filenames(dynamic_tokens)
    ):
        return True
    has_command_substitution = bool(_command_substitutions(command))
    if _has_unsafe_shell_expansion(command) and not _has_only_safe_file_read_redirection(command):
        if _process_substitution_compactable_class(command, sample, text) is not None:
            return False
        if len(segments) <= 1:
            if not _has_unquoted_command_or_process_substitution(command):
                return False
            if not (
                _contains_likely_source_search_output(text)
                or _contains_multiple_likely_source_search_lines(text)
            ):
                return False
    if _pipeline_compactable_class(command, sample, text) is not None:
        return False
    if _pipeline_xargs_compactable_class(command, sample, text) is not None:
        return False
    prior_compactable = False
    prior_compactable_output = False
    prior_compactable_signal = False
    for index, (separator, tokens) in enumerate(segments):
        if _is_setup_segment(tokens):
            continue
        stripped_tokens = _strip_command_wrappers(tokens)
        if stripped_tokens and Path(stripped_tokens[0]).name in {"bash", "sh", "zsh"}:
            shell_command = _shell_c_argument(stripped_tokens[1:])
            if shell_command is not None:
                if _is_source_search_command(
                    shell_command,
                    sample=sample,
                    text=text,
                    exit_code=exit_code,
                ):
                    if (
                        separator == "&&"
                        and prior_compactable
                        and (prior_compactable_output or prior_compactable_signal)
                        and not (
                            _tokens_source_search_hides_filenames(tokens)
                            or _contains_likely_source_search_output(text)
                        )
                    ):
                        continue
                    if separator == "&&" and prior_compactable and exit_code not in {None, 0}:
                        continue
                    if _prior_compactable_output_blocks_exact_tail(
                        separator,
                        prior_compactable,
                        prior_compactable_output,
                        prior_compactable_signal,
                        exit_code,
                    ) and not _text_has_exact_owner_output_against_later_compactable(
                        tokens,
                        text,
                    ):
                        continue
                    if separator == "||" and prior_compactable:
                        if exit_code not in {None, 0}:
                            continue
                        if prior_compactable_output or prior_compactable_signal:
                            continue
                    later_segments = [
                        (later_separator, segment)
                        for later_separator, segment in segments[index + 1 :]
                        if segment and not _is_setup_segment(segment)
                    ]
                    if later_segments:
                        if (
                            exit_code == 0
                            and later_segments[0][0] == "||"
                            and (
                                _or_fallback_exact_left_succeeded(tokens, text)
                                or _or_fallback_branch_terminates_current_shell(later_segments)
                            )
                            and not _or_tail_has_unconditional_compactable_after_fallback(
                                later_segments,
                                sample,
                                text,
                            )
                        ):
                            return True
                        if _or_later_exact_fallback_succeeded(
                            later_segments,
                            sample,
                            text,
                            exit_code,
                        ):
                            return True
                        if _later_compactable_output_dominates(
                            later_segments,
                            sample,
                            text,
                            exit_code,
                        ) and not _text_has_exact_owner_output_against_later_compactable(
                            tokens,
                            text,
                        ):
                            return False
                        return not any(
                            (later_separator == "&" or prior_compactable)
                            and _compactable_class_for_tokens(segment, sample, text) is not None
                            for later_separator, segment in later_segments
                        )
                    return True
                if _compactable_class_for_tokens(tokens, sample, text) is not None:
                    prior_compactable = True
                    if _compactable_output_class_for_tokens(tokens, sample, text) is not None:
                        prior_compactable_signal = True
                    if _compactable_output_dominates_for_tokens(tokens, sample, text):
                        prior_compactable_output = True
                continue
        if (
            _tokens_have_unsafe_shell_expansion(tokens)
            or (
                _has_unsafe_shell_expansion(command)
                and any("$(" in token or "`" in token for token in tokens)
            )
        ) and _has_unquoted_command_or_process_substitution(command):
            if _compactable_class_for_tokens(tokens, sample, text) is not None:
                prior_compactable = True
                if _compactable_output_class_for_tokens(tokens, sample, text) is not None:
                    prior_compactable_signal = True
                if _compactable_output_dominates_for_tokens(tokens, sample, text):
                    prior_compactable_output = True
                continue
            if not (
                _contains_likely_file_read_output(text)
                or _starts_like_file_read_output(text)
                or _contains_likely_source_search_output(text)
                or _contains_multiple_likely_source_search_lines(text)
            ):
                continue
            if (
                _process_substitution_compactable_class(shlex.join(tokens), sample, text)
                is not None
            ):
                continue
        if (
            len(segments) > 1
            and has_command_substitution
            and _tokens_show_command_substitution(tokens)
            and not (
                _contains_likely_source_search_output(text)
                or _contains_multiple_likely_source_search_lines(text)
            )
        ):
            continue
        if _tokens_start_source_search(tokens) or _tokens_pipe_to_source_search(tokens):
            if (
                separator == "&&"
                and prior_compactable
                and (prior_compactable_output or prior_compactable_signal)
                and not (
                    _tokens_source_search_hides_filenames(tokens)
                    or _contains_likely_source_search_output(text)
                )
            ):
                continue
            if separator == "&&" and prior_compactable and exit_code not in {None, 0}:
                continue
            if (
                separator == "&"
                and prior_compactable_signal
                and not _text_has_exact_owner_output_for_tokens(tokens, text)
            ):
                continue
            if _prior_compactable_output_blocks_exact_tail(
                separator,
                prior_compactable,
                prior_compactable_output,
                prior_compactable_signal,
                exit_code,
            ) and not _text_has_exact_owner_output_for_tokens(tokens, text):
                continue
            if separator == "||" and prior_compactable:
                if exit_code not in {None, 0}:
                    continue
                if prior_compactable_output or prior_compactable_signal:
                    continue
                if not _text_has_exact_output_for_tokens(tokens, text):
                    continue
            semantic_later_segments = [
                (later_separator, segment)
                for later_separator, segment in segments[index + 1 :]
                if segment
            ]
            later_segments = [
                (later_separator, segment)
                for later_separator, segment in semantic_later_segments
                if not _is_setup_segment(segment)
            ]
            if semantic_later_segments:
                if (
                    exit_code == 0
                    and semantic_later_segments[0][0] == "||"
                    and not _or_tail_has_unconditional_compactable_after_fallback(
                        semantic_later_segments,
                        sample,
                        text,
                    )
                    and (
                        (
                            not _or_tail_has_any_compactable_fallback(
                                semantic_later_segments,
                                sample,
                                text,
                            )
                            and _or_fallback_exact_left_succeeded(tokens, text)
                        )
                        or (
                            _or_fallback_tail_forces_zero(semantic_later_segments)
                            and not _or_tail_has_any_compactable_fallback(
                                semantic_later_segments,
                                sample,
                                text,
                            )
                            and _text_has_exact_output_for_tokens(tokens, text)
                        )
                        or (
                            _or_fallback_tail_forces_nonzero(semantic_later_segments)
                            and not _or_tail_has_following_compactable_fallback(
                                semantic_later_segments,
                                sample,
                                text,
                            )
                            and _text_has_exact_output_for_tokens(tokens, text)
                        )
                    )
                ):
                    return True
                if (
                    semantic_later_segments[0][0] == "||"
                    and _or_tail_has_reachable_compactable_output(
                        semantic_later_segments,
                        sample,
                        text,
                    )
                    and not _text_has_exact_owner_output_against_later_compactable(
                        tokens,
                        text,
                    )
                ):
                    return False
                if _or_later_exact_fallback_succeeded(
                    later_segments,
                    sample,
                    text,
                    exit_code,
                ):
                    return True
                if _hidden_compactable_tail_preserves_exact_output(
                    tokens,
                    later_segments,
                    sample,
                    text,
                ):
                    return True
                if _later_compactable_output_dominates(
                    later_segments,
                    sample,
                    text,
                    exit_code,
                ) and not _text_has_exact_owner_output_against_later_compactable(
                    tokens,
                    text,
                ):
                    return False
                return not any(
                    (later_separator == "&" or prior_compactable)
                    and _compactable_class_for_tokens(segment, sample, text) is not None
                    for later_separator, segment in later_segments
                )
            return True
        if _compactable_class_for_tokens(tokens, sample, text) is not None:
            prior_compactable = True
            if _compactable_output_class_for_tokens(tokens, sample, text) is not None:
                prior_compactable_signal = True
            if _compactable_output_dominates_for_tokens(tokens, sample, text):
                prior_compactable_output = True
    return False


def _source_consumer_command_class(
    command: str,
    sample: str,
    text: str,
    *,
    exit_code: int | None = None,
) -> str | None:
    substitution_class = _process_substitution_compactable_class(command, sample, text)
    has_process_substitution = _has_active_process_substitution(command)
    if has_process_substitution and substitution_class is not None:
        return substitution_class
    if substitution_class is not None:
        segments = _background_segments(command)
        substitution_index = next(
            (
                index
                for index, (_separator, tokens) in enumerate(segments)
                if any("$(" in token or "`" in token for token in tokens)
            ),
            -1,
        )
        if substitution_index > 0:
            substitution_tokens = _segment_tokens_with_enclosing_group_redirects(
                segments,
                substitution_index,
            )
            substitution_stdout_hidden = _tokens_hide_stdout_from_capture(
                substitution_tokens
            )
            substitution_stderr_hidden = _tokens_hide_stderr_from_capture(
                substitution_tokens
            )
            if (
                substitution_stdout_hidden
                and (
                    substitution_stderr_hidden
                    or not _contains_redirected_compactable_stderr(text)
                )
                and segments[substitution_index][0] in {"&&", "||", ";", "&"}
            ):
                previous_exact_class = (
                    _previous_exact_output_class_when_later_output_is_hidden(
                        segments[:substitution_index],
                        text,
                    )
                )
                if previous_exact_class is not None:
                    return previous_exact_class
        if substitution_index > 0 and segments[substitution_index][0] in {"&&", ";", "&"}:
            previous_exact_class = _previous_exact_output_class(
                segments[:substitution_index],
                text,
                exit_code=exit_code,
            )
            if previous_exact_class is not None:
                return previous_exact_class
        later_segments = segments[substitution_index + 1 :]
        if exit_code == 0 and (
            _or_fallback_branch_terminates_current_shell(later_segments)
            or (
                _or_fallback_tail_forces_zero(later_segments)
                and not _or_tail_has_any_compactable_fallback(
                    later_segments,
                    sample,
                    text,
                )
            )
        ):
            return substitution_class
        for _separator, tokens in reversed(later_segments):
            exact_class = _exact_class_for_tokens(tokens)
            if exact_class is not None and _text_has_exact_owner_output_against_later_compactable(
                tokens,
                text,
                exit_code=exit_code,
            ):
                return exact_class
            later_output_class = _compactable_output_class_for_tokens(tokens, sample, text)
            if later_output_class is not None:
                return later_output_class
        return substitution_class
    if not has_process_substitution:
        for variant in _command_intent_variants(command):
            command_class = _background_tail_command_class(
                variant,
                sample,
                text,
                exit_code=exit_code,
            )
            if command_class is not None:
                return command_class
    for variant in _command_intent_variants(command):
        command_class = _pipeline_compactable_class(variant, sample, text)
        if command_class is not None:
            return command_class
    for variant in _command_intent_variants(command):
        command_class = _pipeline_xargs_compactable_class(variant, sample, text)
        if command_class is not None:
            return command_class
    return None


def _previous_exact_output_class(
    segments: list[tuple[str | None, list[str]]],
    text: str,
    *,
    exit_code: int | None = None,
) -> str | None:
    for _separator, tokens in reversed(segments):
        if not tokens or _is_setup_segment(tokens):
            continue
        exact_class = _exact_class_for_tokens(tokens)
        if exact_class is not None:
            return (
                exact_class
                if _text_has_exact_owner_output_against_later_compactable(
                    tokens,
                    text,
                    exit_code=exit_code,
                )
                else None
            )
        if _compactable_output_class_for_tokens(tokens, text[:2_000], text) is not None:
            return None
    return None


def _previous_exact_output_class_when_later_output_is_hidden(
    segments: list[tuple[str | None, list[str]]],
    text: str,
) -> str | None:
    for _separator, tokens in reversed(segments):
        if not tokens or _is_setup_segment(tokens):
            continue
        exact_class = _exact_class_for_tokens(tokens)
        if exact_class == "file_read":
            return None if _contains_file_read_error_for_tokens(tokens, text) else exact_class
        if exact_class == "source_search":
            return exact_class if _text_has_exact_output_for_tokens(tokens, text) else None
        return None
    return None


def _segments_before_unconditional_current_shell_exit(
    segments: list[tuple[str | None, list[str]]],
) -> tuple[list[tuple[str | None, list[str]]], bool]:
    reachable: list[tuple[str | None, list[str]]] = []
    paren_depth = 0
    first_meaningful = True
    previous_forces_zero = False
    previous_forces_nonzero = False
    for separator, tokens in segments:
        reachable.append((separator, tokens))
        command_paren_depth = paren_depth + tokens.count("(")
        normalized = _strip_grouping_tokens(tokens)
        if (
            normalized
            and Path(normalized[0]).name.lower() == "exit"
            and command_paren_depth == 0
            and (
                first_meaningful
                or separator == ";"
                or (separator == "&&" and previous_forces_zero)
                or (separator == "||" and previous_forces_nonzero)
            )
        ):
            return reachable, True
        paren_depth = max(
            0,
            command_paren_depth - tokens.count(")"),
        )
        if normalized:
            command_name = Path(normalized[0]).name.lower()
            previous_forces_zero = command_name in {":", "echo", "printf", "true"}
            previous_forces_nonzero = command_name == "false"
            first_meaningful = False
    return reachable, False


def _segment_tokens_with_enclosing_group_redirects(
    segments: list[tuple[str | None, list[str]]],
    index: int,
) -> list[str]:
    """Include redirects applied to any shell group enclosing one segment."""
    effective_tokens = list(segments[index][1])
    group_depth = 0
    for _separator, tokens in segments[: index + 1]:
        group_depth += tokens.count("{") + tokens.count("(")
        group_depth = max(0, group_depth - tokens.count("}") - tokens.count(")"))
    if group_depth == 0:
        return effective_tokens

    enclosing_depth = group_depth
    for _separator, tokens in segments[index + 1 :]:
        next_depth = group_depth + tokens.count("{") + tokens.count("(")
        next_depth = max(0, next_depth - tokens.count("}") - tokens.count(")"))
        if next_depth < enclosing_depth:
            effective_tokens.extend(tokens)
            enclosing_depth = next_depth
        group_depth = next_depth
        if enclosing_depth == 0:
            break
    return effective_tokens


def _compactable_segment_can_contribute_output(tokens: list[str], text: str) -> bool:
    stdout_visible, stderr_visible = _redirected_stream_visibility(tokens)
    return stdout_visible or (
        stderr_visible and _contains_redirected_compactable_stderr(text)
    )


def _contains_redirected_compactable_stderr(text: str) -> bool:
    return bool(
        re.search(
            r"(?im)^[^\S\r\n]*(?:npm|pnpm)\s+"
            r"(?:err!|error|warn(?:ing)?)(?:\s|$)|"
            r"^[^\S\r\n]*err_pnpm(?:_|\b)|^[^\S\r\n]*warn\s+|"
            r"^[^\S\r\n]*yarn\b.*\berror\b|"
            r"^[^\S\r\n]*traceback\b|"
            r"^[^\S\r\n]*[a-z_][a-z0-9_]*(?:error|exception):|"
            r"^[^\S\r\n]*(?:e:|err:|error:)\s+\S|"
            r"\bfailed to (?:solve|build|install)\b",
            text,
        )
    )


def _hidden_compactable_tail_preserves_exact_output(
    exact_tokens: list[str],
    later_segments: list[tuple[str | None, list[str]]],
    sample: str,
    text: str,
) -> bool:
    if not _text_has_exact_output_for_tokens(exact_tokens, text):
        return False
    later_segments, _terminated = _segments_before_unconditional_current_shell_exit(
        later_segments
    )
    saw_hidden_stdout = False
    for index, (_separator, segment) in enumerate(later_segments):
        if _compactable_class_for_tokens(segment, sample, text) is None:
            continue
        effective_tokens = _segment_tokens_with_enclosing_group_redirects(
            later_segments,
            index,
        )
        stdout_hidden = _tokens_hide_stdout_from_capture(effective_tokens)
        stderr_hidden = _tokens_hide_stderr_from_capture(effective_tokens)
        if not stdout_hidden:
            if _separator != "||":
                return False
            if saw_hidden_stdout and _contains_redirected_compactable_stderr(text):
                return False
            continue
        saw_hidden_stdout = True
        if stderr_hidden:
            continue
        if _contains_redirected_compactable_stderr(text):
            return False
    return saw_hidden_stdout


def _visible_failed_test_tail_owns_output(
    segments: list[tuple[str | None, list[str]]],
    sample: str,
    text: str,
    exit_code: int | None,
) -> bool:
    if exit_code in {None, 0}:
        return False
    for index, (separator, tokens) in enumerate(segments):
        if separator != "&&":
            continue
        effective_tokens = _segment_tokens_with_enclosing_group_redirects(segments, index)
        stdout_visible, stderr_visible = _redirected_stream_visibility(effective_tokens)
        if not stdout_visible and not stderr_visible:
            continue
        command_class = _compactable_class_for_tokens(tokens, sample, text)
        stripped = _strip_command_wrappers(tokens)
        if (
            command_class == "node"
            and len(stripped) >= 2
            and Path(stripped[0]).name == "npm"
            and stripped[1] == "test"
            and stderr_visible
            and re.search(r"(?im)^\s*error:\s+cannot find module\b", text)
        ):
            return True
        if command_class == "pytest" and stdout_visible and re.search(
            r"(?im)^\s*failed\s+\S+\.py::",
            text,
        ):
            return True
    return False


def _background_tail_command_class(
    command: str,
    sample: str,
    text: str,
    *,
    exit_code: int | None = None,
) -> str | None:
    segments = _background_segments(command)
    for index, (separator, tokens) in enumerate(segments):
        if separator != "&" or not tokens:
            continue
        exact_class = _exact_class_for_tokens(tokens)
        if exact_class is not None:
            for _previous_separator, previous_tokens in reversed(segments[:index]):
                if not previous_tokens or _is_setup_segment(previous_tokens):
                    continue
                previous_output_class = _compactable_output_class_for_tokens(
                    previous_tokens,
                    sample,
                    text,
                )
                if previous_output_class is not None and (
                    exact_class == "file_read"
                    or not _text_has_exact_owner_output_for_tokens(tokens, text)
                ):
                    return previous_output_class
                break
            later_compactable_class = _later_compactable_class(
                [
                    (later_separator, segment)
                    for later_separator, segment in segments[index + 1 :]
                    if not _tokens_hide_stdout_from_capture(segment)
                    or (
                        exit_code not in {None, 0}
                        and _compactable_output_class_for_tokens(segment, sample, text) is not None
                        and not _text_has_exact_output_for_tokens(tokens, text)
                    )
                ],
                sample,
                text,
            )
            return later_compactable_class or exact_class
        compactable_class = _compactable_class_for_tokens(tokens, sample, text)
        if compactable_class is not None:
            if (
                index > 0
                and _compactable_output_class_for_tokens(tokens, sample, text) is None
            ):
                previous_exact_class = _previous_exact_output_class(
                    segments[:index],
                    text,
                    exit_code=exit_code,
                )
                if previous_exact_class is not None:
                    return previous_exact_class
            if _tokens_hide_stdout_from_capture(tokens) and index > 0:
                previous_exact_class = _previous_exact_output_class(
                    segments[:index],
                    text,
                    exit_code=exit_code,
                )
                if previous_exact_class is not None:
                    return previous_exact_class
                if not _contains_redirected_compactable_stderr(text):
                    for _previous_separator, previous_tokens in reversed(segments[:index]):
                        if _is_setup_segment(previous_tokens):
                            continue
                        previous_exact_class = _exact_class_for_tokens(previous_tokens)
                        if previous_exact_class == "file_read" or (
                            previous_exact_class == "source_search"
                            and _text_has_exact_output_for_tokens(previous_tokens, text)
                        ):
                            return previous_exact_class
                        break
            if (
                _tokens_hide_stdout_from_capture(tokens)
                and index > 0
                and _compactable_output_class_for_tokens(tokens, sample, text) is None
            ):
                previous_exact_class = _exact_class_for_tokens(segments[index - 1][1])
                if previous_exact_class is not None:
                    return previous_exact_class
            later_exact_class = _later_exact_tail_class(
                segments[index + 1 :],
                sample,
                text,
                exit_code=exit_code,
            )
            if later_exact_class is not None:
                return later_exact_class
            return compactable_class
    return None


def _background_segments(command: str) -> list[tuple[str | None, list[str]]]:
    segments: list[tuple[str | None, list[str]]] = []
    separator: str | None = None
    current: list[str] = []
    for token in _shell_tokens(command):
        if token in {"&&", "||", ";", "&"} and not getattr(token, "was_quoted", False):
            if current:
                segments.append((separator, current))
                current = []
            separator = token
            continue
        current.append(token)
    if current:
        segments.append((separator, current))
    return segments


def _prior_compactable_output_blocks_exact_tail(
    separator: str | None,
    prior_compactable: bool,
    prior_compactable_output: bool,
    prior_compactable_signal: bool,
    exit_code: int | None,
) -> bool:
    if separator != "&&" or not prior_compactable:
        return False
    if exit_code not in {None, 0}:
        return True
    return exit_code is None and (prior_compactable_output or prior_compactable_signal)


def _later_compactable_class(
    segments: list[tuple[str | None, list[str]]],
    sample: str,
    text: str,
) -> str | None:
    segments, _terminated = _segments_before_unconditional_current_shell_exit(segments)
    for index, (_separator, tokens) in enumerate(segments):
        if not tokens or _is_setup_segment(tokens):
            continue
        effective_tokens = _segment_tokens_with_enclosing_group_redirects(segments, index)
        command_class = _compactable_class_for_tokens(tokens, sample, text)
        if command_class is not None and _compactable_segment_can_contribute_output(
            effective_tokens,
            text,
        ):
            return command_class
    return None


def _later_compactable_output_ran(
    segments: list[tuple[str | None, list[str]]],
    sample: str,
    text: str,
    exit_code: int | None,
) -> bool:
    segments, _terminated = _segments_before_unconditional_current_shell_exit(segments)
    for index, (separator, tokens) in enumerate(segments):
        if not tokens or _is_setup_segment(tokens):
            continue
        if separator == "&":
            continue
        if separator == "||" and exit_code == 0:
            continue
        effective_tokens = _segment_tokens_with_enclosing_group_redirects(segments, index)
        command_class = _compactable_output_class_for_tokens(tokens, sample, text)
        if command_class is not None and _compactable_segment_can_contribute_output(
            effective_tokens,
            text,
        ):
            return True
    return False


def _tokens_without_simple_redirections(tokens: list[str]) -> list[str]:
    normalized: list[str] = []
    index = 0
    while index < len(tokens):
        redirect = _simple_shell_redirection(tokens, index)
        if redirect is not None:
            consumed, _supplies_stdin = redirect
            index += consumed
            continue
        normalized.append(tokens[index])
        index += 1
    return normalized


def _known_command_statuses(tokens: list[str]) -> set[bool]:
    normalized = _tokens_without_simple_redirections(
        _strip_command_wrappers(_strip_grouping_tokens(tokens))
    )
    inverted = False
    while normalized and normalized[0] == "!":
        inverted = not inverted
        normalized = normalized[1:]
    if normalized == ["true"] or normalized == [":"]:
        statuses = {True}
    elif normalized == ["false"]:
        statuses = {False}
    elif normalized and normalized[0] in {"exit", "return"}:
        if len(normalized) == 1:
            statuses = {True}
        elif len(normalized) == 2 and normalized[1].isdigit():
            statuses = {int(normalized[1]) == 0}
        else:
            statuses = {True, False}
    else:
        statuses = {True, False}
    return {not status for status in statuses} if inverted else statuses


def _setup_status_for_fd_reachability(
    tokens: list[str],
    effective_tokens: list[str],
    text: str,
) -> set[bool] | None:
    direct = _tokens_without_simple_redirections(_strip_grouping_tokens(tokens))
    normalized = _tokens_without_simple_redirections(_strip_command_wrappers(direct))
    if not _is_setup_segment(normalized):
        return None
    if direct and Path(direct[0]).name == "env" and direct != normalized:
        return {True, False}
    command_name = normalized[0]
    if command_name == "true":
        return {True}
    if command_name == "cd":
        if len(normalized) != 2:
            return {True, False}
    elif command_name == "export":
        assignments = normalized[1:]
        if not assignments or not all(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:=.*)?", token)
            for token in assignments
        ):
            return {True, False}
    else:
        return {True, False}
    _stdout_visible, stderr_visible = _redirected_stream_visibility(effective_tokens)
    if not stderr_visible:
        return {True, False}
    if re.search(
        r"(?im)^(?:[^\s:]+/)?-?(?:[a-z]+)?sh:(?:(?: line)? \d+:)?|"
        r"^.*:\s*(?:line \d+:\s*)?(?:cd|pushd|popd|source|export|pwd):",
        text,
    ) or _contains_file_read_error(text):
        return {False}
    return {True}


def _shell_list_statuses(command: str) -> set[bool]:
    statuses: set[bool] = set()
    for separator, tokens in _background_segments(command):
        if not tokens:
            continue
        command_statuses = _known_command_statuses(tokens)
        if separator in {None, ";", "&"}:
            runs = True
            statuses = command_statuses
        elif separator == "&&":
            runs = True in statuses
            statuses = ({False} if False in statuses else set()) | (
                command_statuses if runs else set()
            )
        elif separator == "||":
            runs = False in statuses
            statuses = ({True} if True in statuses else set()) | (
                command_statuses if runs else set()
            )
        else:
            return {True, False}
    return statuses or {True, False}


def _shell_fd_owner_statuses(command: str) -> set[bool]:
    statuses = _shell_list_statuses(command)
    semantic_segments = [
        tokens
        for _separator, tokens in _background_segments(command)
        if tokens and not _is_setup_segment(tokens)
    ]
    if (
        statuses == {True, False}
        and len(semantic_segments) == 1
        and _tokens_start_fd_attached_file_read(semantic_segments[0])
    ):
        return {True}
    return statuses


def _fd_later_compactable_output_ran(
    segments: list[tuple[str | None, list[str]]],
    sample: str,
    text: str,
    exit_code: int | None,
    *,
    initial_statuses: set[bool] | None = None,
) -> bool:
    if exit_code in {None, 0}:
        return False

    statuses = set(initial_statuses or {True})
    reachable: list[tuple[int, list[str]]] = []
    terminated_subshell_depth = 0
    for index, (separator, tokens) in enumerate(segments):
        if not tokens:
            continue
        if terminated_subshell_depth:
            terminated_subshell_depth = max(
                0,
                terminated_subshell_depth + tokens.count("(") - tokens.count(")"),
            )
            continue
        normalized = _strip_command_wrappers(_strip_grouping_tokens(tokens))
        if not normalized:
            continue
        if separator in {None, ";"}:
            runs = True
        elif separator == "&&":
            runs = statuses == {True}
        elif separator == "||":
            runs = statuses == {False}
        elif separator == "&":
            runs = True
        else:
            return False
        if not runs:
            continue
        reachable.append((index, tokens))
        effective_tokens = _segment_tokens_with_enclosing_group_redirects(segments, index)
        command_statuses = _setup_status_for_fd_reachability(
            tokens,
            effective_tokens,
            text,
        ) or _known_command_statuses(tokens)
        if normalized and normalized[0] in {"exit", "return"}:
            if tokens[0] != "(":
                break
            terminated_subshell_depth = max(0, tokens.count("(") - tokens.count(")"))
        if separator in {None, ";", "&"}:
            statuses = command_statuses
        elif separator == "&&":
            statuses = ({False} if False in statuses else set()) | command_statuses
        elif separator == "||":
            statuses = ({True} if True in statuses else set()) | command_statuses

    for index, tokens in reachable:
        command_class = _compactable_output_class_for_tokens(tokens, sample, text)
        if command_class is None:
            continue
        effective_tokens = _segment_tokens_with_enclosing_group_redirects(segments, index)
        stdout_visible, stderr_visible = _redirected_stream_visibility(effective_tokens)
        command = shlex.join(tokens)
        if _is_uv_pytest_resolution_failure(command, text):
            if stderr_visible:
                return True
            continue
        if stdout_visible or (
            stderr_visible and _contains_redirected_compactable_stderr(text)
        ):
            return True
    return False


def _later_compactable_output_dominates(
    segments: list[tuple[str | None, list[str]]],
    sample: str,
    text: str,
    exit_code: int | None,
) -> bool:
    segments, _terminated = _segments_before_unconditional_current_shell_exit(segments)
    for index, (separator, tokens) in enumerate(segments):
        if not tokens or _is_setup_segment(tokens):
            continue
        if separator == "&":
            continue
        if _exact_class_for_tokens(tokens) is not None:
            continue
        effective_tokens = _segment_tokens_with_enclosing_group_redirects(segments, index)
        if (
            _tokens_hide_stdout_from_capture(effective_tokens)
            and _tokens_hide_stderr_from_capture(effective_tokens)
        ):
            continue
        if (
            _tokens_hide_stdout_from_capture(effective_tokens)
            and not _tokens_hide_stderr_from_capture(effective_tokens)
            and _contains_redirected_compactable_stderr(text)
            and _compactable_class_for_tokens(tokens, sample, text) is not None
        ):
            return True
        if _compactable_output_dominates_for_tokens(tokens, sample, text):
            return True
    return False


def _or_later_exact_fallback_succeeded(
    segments: list[tuple[str | None, list[str]]],
    sample: str,
    text: str,
    exit_code: int | None,
) -> bool:
    for fallback_index, (separator, tokens) in enumerate(segments):
        if separator != "||" or not tokens or _is_setup_segment(tokens):
            continue
        exact_class = _exact_class_for_tokens(tokens)
        if exact_class is None:
            continue
        if exact_class == "file_read":
            owns_output = (
                not _contains_file_read_error_for_tokens(tokens, text)
                and (_contains_likely_file_read_output(text) or _starts_like_file_read_output(text))
            )
        else:
            owns_output = _text_has_exact_owner_output_against_later_compactable(
                tokens,
                text,
                exit_code=exit_code,
            )
        if not owns_output:
            continue
        following = segments[fallback_index + 1 :]
        if exit_code == 0 and all(
            following_separator == "||" for following_separator, _ in following
        ):
            return True
        if not _later_compactable_output_dominates(following, sample, text, exit_code):
            return True
    return False


def _top_level_or_fallback_branches(
    segments: list[tuple[str | None, list[str]]],
) -> list[list[tuple[str | None, list[str]]]]:
    branches: list[list[tuple[str | None, list[str]]]] = []
    current: list[tuple[str | None, list[str]]] = []
    started = False
    group_depth = 0
    for separator, tokens in segments:
        if not started:
            if separator != "||":
                continue
            started = True
        elif group_depth == 0 and separator == "||":
            if current:
                branches.append(current)
            current = []
        current.append((separator, tokens))
        group_depth += tokens.count("{") + tokens.count("(")
        group_depth = max(0, group_depth - tokens.count("}") - tokens.count(")"))
    if current:
        branches.append(current)
    return branches


def _or_tail_has_any_compactable_fallback(
    segments: list[tuple[str | None, list[str]]],
    sample: str,
    text: str,
) -> bool:
    for branch in _top_level_or_fallback_branches(segments):
        reachable_branch, terminated = _segments_before_unconditional_current_shell_exit(branch)
        if any(
            _compactable_class_for_tokens(tokens, sample, text)
            and _compactable_segment_can_contribute_output(
                _segment_tokens_with_enclosing_group_redirects(reachable_branch, index),
                text,
            )
            for index, (_separator, tokens) in enumerate(reachable_branch)
        ):
            return True
        if terminated:
            return False
        if _or_fallback_branch_terminates_current_shell(branch):
            return False
        if _or_fallback_tail_forces_zero(branch):
            return False
    return False


def _or_tail_has_reachable_compactable_output(
    segments: list[tuple[str | None, list[str]]],
    sample: str,
    text: str,
) -> bool:
    if not _or_tail_has_any_compactable_fallback(segments, sample, text):
        return False
    return any(
        _compactable_output_class_for_tokens(tokens, sample, text) is not None
        and _compactable_segment_can_contribute_output(
            _segment_tokens_with_enclosing_group_redirects(segments, index),
            text,
        )
        for index, (_separator, tokens) in enumerate(segments)
    )


def _or_fallback_branch_terminates_current_shell(
    segments: list[tuple[str | None, list[str]]],
) -> bool:
    saw_fallback = False
    brace_depth = 0
    paren_depth = 0
    previous_forces_zero = False
    previous_forces_nonzero = False
    for separator, tokens in segments:
        first_fallback_segment = not saw_fallback
        if not saw_fallback:
            if separator != "||":
                continue
            saw_fallback = True
        elif brace_depth == 0 and paren_depth == 0:
            break

        brace_depth += tokens.count("{")
        paren_depth += tokens.count("(")
        normalized = _strip_grouping_tokens(tokens)
        is_numeric_exit = bool(
            normalized
            and Path(normalized[0]).name.lower() == "exit"
            and len(normalized) >= 2
            and re.fullmatch(r"[+-]?\d+", normalized[1])
        )
        if is_numeric_exit and (
            (
                first_fallback_segment
                and brace_depth == 0
                and paren_depth == 0
            )
            or (
                int(normalized[1]) % 256 != 0
                and (
                    first_fallback_segment
                    or separator == ";"
                    or (separator == "&&" and previous_forces_zero)
                    or (separator == "||" and previous_forces_nonzero)
                )
                and brace_depth > 0
                and paren_depth == 0
            )
        ):
            return True
        if normalized:
            command_name = Path(normalized[0]).name.lower()
            previous_forces_zero = command_name in {":", "echo", "printf", "true"}
            previous_forces_nonzero = command_name == "false"
        brace_depth = max(0, brace_depth - tokens.count("}"))
        paren_depth = max(0, paren_depth - tokens.count(")"))
        if saw_fallback and brace_depth == 0 and paren_depth == 0:
            break
    return False


def _or_tail_has_following_compactable_fallback(
    segments: list[tuple[str | None, list[str]]],
    sample: str,
    text: str,
) -> bool:
    if _or_fallback_branch_terminates_current_shell(segments):
        return False
    saw_fallback = False
    group_depth = 0
    branch_done = False
    for index, (separator, tokens) in enumerate(segments):
        if not saw_fallback:
            if separator != "||":
                continue
            saw_fallback = True
        elif not branch_done:
            if group_depth == 0:
                branch_done = True
            else:
                group_depth += tokens.count("{") + tokens.count("(")
                group_depth = max(0, group_depth - tokens.count("}") - tokens.count(")"))
                if group_depth == 0:
                    branch_done = True
                continue

        if not branch_done:
            group_depth += tokens.count("{") + tokens.count("(")
            group_depth = max(0, group_depth - tokens.count("}") - tokens.count(")"))
            if group_depth == 0:
                branch_done = True
            continue
        if (
            separator == "||"
            and _compactable_class_for_tokens(tokens, sample, text)
            and _compactable_segment_can_contribute_output(
                _segment_tokens_with_enclosing_group_redirects(segments, index),
                text,
            )
        ):
            return True
    return False


def _or_tail_has_unconditional_compactable_after_fallback(
    segments: list[tuple[str | None, list[str]]],
    sample: str,
    text: str,
) -> bool:
    saw_fallback = False
    group_depth = 0
    for index, (separator, tokens) in enumerate(segments):
        if separator == "||" and not saw_fallback:
            saw_fallback = True
            group_depth += tokens.count("{") + tokens.count("(")
            group_depth = max(0, group_depth - tokens.count("}") - tokens.count(")"))
            continue
        if not saw_fallback:
            continue
        if (
            group_depth == 0
            and separator in {";", "&&", "&"}
            and _compactable_class_for_tokens(tokens, sample, text)
            and _compactable_segment_can_contribute_output(
                _segment_tokens_with_enclosing_group_redirects(segments, index),
                text,
            )
        ):
            return True
        group_depth += tokens.count("{") + tokens.count("(")
        group_depth = max(0, group_depth - tokens.count("}") - tokens.count(")"))
    return False


def _or_fallback_tail_forces_zero(
    segments: list[tuple[str | None, list[str]]],
) -> bool:
    branch_tokens: list[list[str]] = []
    saw_fallback = False
    group_depth = 0
    for separator, tokens in segments:
        if not saw_fallback:
            if separator != "||":
                continue
            saw_fallback = True
        elif group_depth == 0:
            break

        normalized = _strip_grouping_tokens(tokens)
        if normalized:
            branch_tokens.append(normalized)
        group_depth += tokens.count("{") + tokens.count("(")
        group_depth = max(0, group_depth - tokens.count("}") - tokens.count(")"))
        if saw_fallback and group_depth == 0:
            break

    if not branch_tokens:
        return False
    last_command = Path(branch_tokens[-1][0]).name.lower()
    if last_command in {"true", ":", "echo", "printf"}:
        return True
    if last_command != "exit" or len(branch_tokens[-1]) == 1:
        return False
    try:
        return int(branch_tokens[-1][1]) % 256 == 0
    except ValueError:
        return False


def _or_fallback_tail_forces_nonzero(
    segments: list[tuple[str | None, list[str]]],
) -> bool:
    """Return True when the immediate || fallback branch cannot explain exit_code=0."""

    branch_tokens: list[list[str]] = []
    saw_fallback = False
    group_depth = 0
    for separator, tokens in segments:
        if not saw_fallback:
            if separator != "||":
                continue
            saw_fallback = True
        elif group_depth == 0:
            break

        normalized = _strip_grouping_tokens(tokens)
        if normalized:
            branch_tokens.append(normalized)
        group_depth += tokens.count("{") + tokens.count("(")
        group_depth = max(0, group_depth - tokens.count("}") - tokens.count(")"))
        if saw_fallback and group_depth == 0:
            break

    if not branch_tokens:
        return False
    last_command = Path(branch_tokens[-1][0]).name.lower()
    if last_command == "false":
        return True
    if last_command != "exit" or len(branch_tokens[-1]) == 1:
        return False
    try:
        return int(branch_tokens[-1][1]) != 0
    except ValueError:
        return False


def _later_exact_tail_class(
    segments: list[tuple[str | None, list[str]]],
    sample: str,
    text: str,
    *,
    exit_code: int | None = None,
) -> str | None:
    for index, (separator, tokens) in enumerate(segments):
        if not tokens or _is_setup_segment(tokens):
            continue
        exact_class = _exact_class_for_tokens(tokens)
        if exact_class is None:
            continue
        if separator == "&&" and exit_code not in {None, 0}:
            continue
        later_compactable_class = _later_compactable_class(
            segments[index + 1 :],
            sample,
            text,
        )
        if later_compactable_class is None:
            return exact_class
        return None
    return None


def _exact_class_for_tokens(tokens: list[str]) -> str | None:
    if _tokens_have_unsafe_shell_expansion(tokens):
        return None
    if _tokens_start_source_search(tokens) or _tokens_pipe_to_source_search(tokens):
        return "source_search"
    if _tokens_start_file_read(tokens):
        return "file_read"
    return None


def _tokens_redirect_stdout(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if getattr(token, "redirection_operator_was_quoted", False):
            continue
        if token in {">", ">>", "1>", ">|"}:
            return True
        if token.startswith("1<>"):
            return True
        if token.startswith((">", "1>")) and token not in {"2>", "2>>"}:
            return True
        if token == "&>":
            return True
        if token.startswith("&>"):
            return True
        if token in {"2>", "2>>"}:
            continue
        if (
            token == "2"
            and not getattr(token, "was_quoted", False)
            and index + 1 < len(tokens)
            and tokens[index + 1] in {">", ">>"}
            and not getattr(tokens[index + 1], "redirection_operator_was_quoted", False)
        ):
            continue
    return False


def _redirected_stream_visibility(tokens: list[str]) -> tuple[bool, bool]:
    stdout_visible = True
    stderr_visible = True

    def target_visibility(target: str) -> bool:
        if target in {"&1", "/dev/stdout", "/dev/fd/1", "/proc/self/fd/1"}:
            return stdout_visible
        if target in {"&2", "/dev/stderr", "/dev/fd/2", "/proc/self/fd/2"}:
            return stderr_visible
        return False

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if getattr(token, "redirection_operator_was_quoted", False):
            index += 1
            continue
        if token in {"&>", "&>>"} or token.startswith(("&>", "&>>")):
            target = token[2:]
            if token.startswith("&>>"):
                target = token[3:]
            if not target and index + 1 < len(tokens):
                target = tokens[index + 1]
                index += 1
            visibility = target_visibility(target)
            stdout_visible = visibility
            stderr_visible = visibility
            index += 1
            continue

        fd_prefix = ""
        redirect_token = token
        if (
            token in {"1", "2"}
            and not getattr(token, "was_quoted", False)
            and not getattr(token, "numeric_redirection_was_separated", False)
            and index + 1 < len(tokens)
        ):
            next_token = tokens[index + 1]
            if next_token in {
                ">",
                ">>",
                ">|",
                "<>",
                "<",
                "<&",
                "<<",
                "<<<",
            } and not getattr(
                next_token,
                "redirection_operator_was_quoted",
                False,
            ):
                fd_prefix = token
                redirect_token = next_token
                index += 1

        input_match = re.fullmatch(r"([12]?)(<&|<<<|<<|<)(.*)", redirect_token)
        if input_match is not None:
            fd = fd_prefix or input_match.group(1) or "0"
            operator = input_match.group(2)
            target = input_match.group(3)
            if not target and index + 1 < len(tokens):
                target = tokens[index + 1]
                index += 1
            if fd == "1":
                if operator == "<&" and target in {"1", "&1"}:
                    pass
                elif operator == "<&" and target in {"2", "&2"}:
                    stdout_visible = stderr_visible
                else:
                    stdout_visible = False
            index += 1
            continue

        match = re.fullmatch(r"([12]?)(<>|>>?|>\|)(.*)", redirect_token)
        if match is None:
            index += 1
            continue
        operator = match.group(2)
        fd = fd_prefix or match.group(1) or ("0" if operator == "<>" else "1")
        target = match.group(3)
        if not target and index + 1 < len(tokens):
            target = tokens[index + 1]
            index += 1
        visibility = target_visibility(target)
        if fd == "2":
            stderr_visible = visibility
        elif fd == "1":
            stdout_visible = visibility
        index += 1

    return stdout_visible, stderr_visible


def _tokens_hide_stdout_from_capture(tokens: list[str]) -> bool:
    stdout_visible, _stderr_visible = _redirected_stream_visibility(tokens)
    return not stdout_visible


def _tokens_hide_stderr_from_capture(tokens: list[str]) -> bool:
    _stdout_visible, stderr_visible = _redirected_stream_visibility(tokens)
    return not stderr_visible


def _tokens_redirect_stderr(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if getattr(token, "redirection_operator_was_quoted", False):
            continue
        if token in {"2>", "2>>", "&>"}:
            return True
        if token.startswith("2<>"):
            return True
        if token.startswith(("2>", "&>")):
            return True
        if (
            token == "2"
            and not getattr(token, "was_quoted", False)
            and index + 1 < len(tokens)
            and tokens[index + 1] in {">", ">>"}
            and not getattr(tokens[index + 1], "redirection_operator_was_quoted", False)
        ):
            return True
    return False


def _tokens_have_unsafe_shell_expansion(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if token == "<" and index + 1 < len(tokens) and tokens[index + 1] == "(":
            return True
        if token == "$" and index + 1 < len(tokens) and tokens[index + 1] == "(":
            return True
        if token.endswith("$") and index + 1 < len(tokens) and tokens[index + 1] == "(":
            return True
        redirection_is_quoted = getattr(token, "redirection_operator_was_quoted", False)
        if (
            "`" in token
            or (token.startswith(("<(", ">(")) and not redirection_is_quoted)
            or (
                token in {">", ">>", "1>", "&>", ">|"}
                and not redirection_is_quoted
            )
            or (token.startswith((">", "1>", "&>")) and not redirection_is_quoted)
        ):
            return True
    return False


def _tokens_start_exact_output_producer(tokens: list[str]) -> bool:
    if _tokens_start_source_search(tokens) or _tokens_start_file_read(tokens):
        return True
    if _tokens_pipe_to_source_search(tokens):
        return True
    if _tokens_start_find_path_list(tokens):
        return True
    command = shlex.join(tokens)
    return any(
        _tokens_start_source_search(segment)
        or _tokens_start_file_read(segment)
        or _tokens_pipe_to_source_search(segment)
        for segment in _command_execution_segments(command)
        if segment
    )


def _pipeline_compactable_class(command: str, sample: str, text: str) -> str | None:
    token_variants = [_shell_tokens(variant) for variant in _command_intent_variants(command)]
    token_variants.extend(_command_token_segments(command))
    for tokens in token_variants:
        for index, token in enumerate(tokens[:-1]):
            if token not in {"|", "|&"} or getattr(token, "was_quoted", False):
                continue
            upstream = tokens[:index]
            if not _tokens_start_exact_output_producer(upstream):
                continue
            downstream = _strip_command_wrappers(tokens[index + 1 :])
            if _tokens_start_exact_output_producer(downstream):
                continue
            command_class = _compactable_class_for_tokens(downstream, sample, text)
            if command_class is not None:
                return command_class
    return None


def _pipeline_xargs_compactable_class(command: str, sample: str, text: str) -> str | None:
    token_variants = [_shell_tokens(variant) for variant in _command_intent_variants(command)]
    token_variants.extend(_command_token_segments(command))
    for tokens in token_variants:
        for index, token in enumerate(tokens[:-1]):
            if token not in {"|", "|&"} or getattr(token, "was_quoted", False):
                continue
            upstream = tokens[:index]
            if not _tokens_start_exact_output_producer(upstream):
                continue
            downstream = _strip_command_wrappers(tokens[index + 1 :])
            payload = _command_runner_payload(downstream)
            if payload is None:
                continue
            if _tokens_start_exact_output_producer(payload):
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
    for substitution_command in (command, *command_variants):
        substitution_class = _process_substitution_compactable_class(
            substitution_command,
            sample,
            text,
        )
        if substitution_class is not None:
            return substitution_class
    if any(_is_apt_command(variant) for variant in command_variants):
        return "apt"
    if any(_is_uv_pytest_resolution_failure(variant, text) for variant in command_variants):
        return "python_package"
    if any(_is_python_package_command(variant) for variant in command_variants):
        return "python_package"
    if (
        any(_is_pytest_command(variant) for variant in command_variants)
        or _contains_process_substitution_pytest(command)
        or "=== failures ===" in sample
    ):
        return "pytest"
    if any(_is_unittest_command(variant) for variant in command_variants) or re.search(
        r"ran \d+ tests?",
        sample,
    ):
        return "unittest"
    if any(_is_docker_log_command(variant) for variant in command_variants):
        return "docker_logs"
    if any(_is_log_stream_command(variant) for variant in command_variants):
        return "log_stream"
    if any(_is_docker_build_command(variant) for variant in command_variants) or (
        _looks_like_docker_build_output(text)
        and any(_can_infer_docker_build_from_output(variant) for variant in command_variants)
    ):
        return "docker_build"
    if any(_is_node_command(variant, sample) for variant in command_variants):
        return "node"
    return None


def _compactable_output_class_for_tokens(
    tokens: list[str],
    sample: str,
    text: str,
) -> str | None:
    command_class = _compactable_class_for_tokens(tokens, sample, text)
    if command_class is None:
        return None
    output = f"{sample}\n{text}"
    if command_class in {"pytest", "unittest"}:
        if _first_pattern_match(output, TEST_PATTERNS) or _first_pattern_match(
            output,
            CRITICAL_PATTERNS,
        ):
            return command_class
        return None
    if command_class in {"apt", "python_package"}:
        if _first_pattern_match(output, PACKAGE_PATTERNS) or _first_pattern_match(
            output,
            CRITICAL_PATTERNS,
        ):
            return command_class
        return None
    if command_class == "node":
        if _first_pattern_match(output, NODE_PATTERNS) or _first_pattern_match(
            output,
            CRITICAL_PATTERNS,
        ):
            return command_class
        return None
    if command_class == "docker_build":
        if _first_pattern_match(
            output,
            DOCKER_BUILD_PRESERVATION_PATTERNS,
        ) or _looks_like_docker_build_output(output):
            return command_class
        return None
    if command_class in {"docker_logs", "log_stream"}:
        patterns = DOCKER_LOG_PATTERNS if command_class == "docker_logs" else LOG_STREAM_PATTERNS
        if _first_pattern_match(output, patterns) or _first_pattern_match(
            output,
            CRITICAL_PATTERNS,
        ):
            return command_class
        return None
    return command_class


def _compactable_output_dominates_for_tokens(
    tokens: list[str],
    sample: str,
    text: str,
) -> bool:
    command_class = _compactable_class_for_tokens(tokens, sample, text)
    if command_class is None:
        return False
    output = f"{sample}\n{text}"
    if command_class in {"pytest", "unittest"}:
        has_pytest_summary = bool(
            re.search(r"(?im)^={2,}.*(?:failures|errors|short test summary)", output)
            or re.search(r"(?im)^tests?/.*::.*\b(?:passed|failed|error)\b", output)
            or re.search(
                r"(?im)^\s*(?:=+\s*)?(?:\d+\s+(?:passed|failed|errors?)(?:,\s*)?)+\b",
                output,
            )
            or len(re.findall(r"(?im)^pytest\b", output)) >= 3
        )
        has_short_traceback_detail = bool(
            re.search(r"(?im)^[\w./-]+\.py:\d+:\s+in\s+(?:\w+|<[^>]+>)", output)
            and (
                re.search(r"(?im)^\s*e\s+", output)
                or re.search(
                    r"(?i)\b(?:assertionerror|modulenotfounderror|importerror|typeerror|"
                    r"referenceerror|runtimeerror):",
                    output,
                )
            )
        )
        has_traceback_detail = bool(
            re.search(r"(?im)^traceback \(most recent call last\):", output)
            and (
                re.search(r"(?im)^\s*file \".+\", line \d+", output)
                or re.search(r"(?im)^\s*e\s+", output)
                or re.search(
                    r"(?i)\b(?:assertionerror|modulenotfounderror|importerror|typeerror|referenceerror|runtimeerror):",
                    output,
                )
            )
        )
        return has_pytest_summary or has_traceback_detail or has_short_traceback_detail
    if command_class in {"apt", "python_package"}:
        return bool(
            _first_pattern_match(output, PACKAGE_PATTERNS)
            or _first_pattern_match(output, CRITICAL_PATTERNS)
            or re.search(r"(?im)^(?:e:|err:|error:)", output)
            or re.search(
                r"(?i)resolutionimpossible|no matching distribution found|unable to locate package",
                output,
            )
            or re.search(r"(?im)^(?:installed|downloaded|resolved|prepared|audited)\s+\d+", output)
            or re.search(r"(?im)(?:^|\|)\s*\d+%\s+\[[^\]]+\].*\bapt\b", output)
            or re.search(r"(?im)(?:^|\|)\s*(?:get|hit|ign):\d+\b", output)
            or re.search(r"(?im)(?:^|\|)\s*\d+%\s+\[[^\]]+\]", output)
            or re.search(r"(?im)(?:^|\|)\s*reading package lists\b", output)
            or re.search(r"(?im)(?:^|\|)\s*building dependency tree\b", output)
            or re.search(r"(?im)(?:^|\|)\s*reading state information\b", output)
            or re.search(r"(?im)(?:^|\|)\s*fetched\s+\d+", output)
            or re.search(r"(?im)(?:^|\|)\s*setting up\b", output)
            or re.search(
                r"(?im)^(?:get|hit|ign):\d+\b|^\s*fetched\s+\d+|"
                r"^\s*reading package lists\b|^\s*building dependency tree\b|"
                r"^\s*reading state information\b",
                output,
            )
            or re.search(
                r"(?i)successfully installed|installing collected packages|"
                r"requirement already satisfied",
                output,
            )
            or re.search(r"(?im)^\s*(?:up to date|setting up)\b", output)
            or re.search(r"(?im)\b\d+\s+newly installed\b", output)
            or re.search(r"(?im)^\s*[+~\-]\s+\w", output)
        )
    if command_class == "node":
        return bool(
            len(
                re.findall(
                    r"(?im)^\s*(?:(?:npm|pnpm)\s+(?:err!|error|warn(?:ing)?)|"
                    r"yarn.*error|err_pnpm|warn\s+)",
                    output,
                )
            )
            >= 2
            or re.search(r"(?i)\b(?:typeerror|referenceerror|syntaxerror|rangeerror):", output)
            or re.search(r"(?im)^\s*error:\s+\S", output)
            or re.search(r"(?im)^\s+at\s+.+\(.+\)", output)
            or re.search(r"(?im)^\s*error:\s+cannot find module\b", output)
            or re.search(
                r"(?im)^\s*(?:=+\s*)?(?:\d+\s+(?:passed|failed|errors?)(?:,\s*)?)+\b",
                output,
            )
            or re.search(r"(?im)^tests?/.*::.*\b(?:passed|failed|error)\b", output)
            or re.search(r"(?im)^(?:added|removed|changed|audited)\s+\d+\s+packages?\b", output)
            or re.search(
                r"(?im)^\s*(?:found\s+0\s+vulnerabilities|\d+\s+vulnerabilities?)\b",
                output,
            )
            or re.search(r"(?im)^up to date\b", output)
        )
    if command_class == "docker_build":
        return bool(
            re.search(r"(?im)^(?:#\d+\s+error\b|failed to solve\b|error: failed\b)", output)
            or re.search(r"(?im)(?:^|\|)\s*step\s+\d+/\d+\s*:", output)
            or re.search(r"(?im)(?:^|\|)\s*=>\s+.*\bdockerfile\b", output)
            or re.search(
                r"(?im)(?:^|\|)\s*(?:#\d+|=>)\s+.*\b"
                r"(?:load metadata|load \.dockerignore|exporting|writing image)\b",
                output,
            )
            or re.search(r"(?im)(?:^|\|)\s*(?:#\d+|=>)\s+(?:done|cached)\b", output)
            or re.search(r"(?im)(?:^|\|)\s*(?:#\d+|=>)\s+.*\b(?:done|cached)\b", output)
            or _looks_like_docker_build_output(output)
        )
    return _compactable_output_class_for_tokens(tokens, sample, text) is not None


def _is_setup_segment(tokens: list[str]) -> bool:
    setup_commands = {".", "cd", "export", "popd", "pushd", "pwd", "set", "source", "true"}
    return bool(tokens and (tokens[0] == "." or Path(tokens[0]).name in setup_commands))


def _text_has_exact_output_for_tokens(tokens: list[str], text: str) -> bool:
    if _tokens_start_source_search(tokens) or _tokens_pipe_to_source_search(tokens):
        return (
            _tokens_source_search_hides_filenames(tokens)
            or _contains_likely_source_search_output(text)
            or _source_search_pattern_appears_in_text(tokens, text)
        )
    if _tokens_start_file_read(tokens):
        if _contains_file_read_error(text):
            return False
        return _contains_likely_file_read_output(text) or _tokens_read_plain_text_file(tokens)
    return False


def _text_has_exact_owner_output_for_tokens(tokens: list[str], text: str) -> bool:
    if _tokens_start_source_search(tokens) or _tokens_pipe_to_source_search(tokens):
        pattern_owns_text = _source_search_pattern_owns_text(tokens, text)
        fixed_pattern_owns_text = (
            _tokens_source_search_uses_fixed_strings(tokens)
            and _source_search_pattern_appears_in_text(tokens, text)
        )
        if _looks_like_test_failure_output(text) and not (
            pattern_owns_text or fixed_pattern_owns_text
        ):
            return False
        return (
            _contains_likely_source_search_output(text)
            or pattern_owns_text
            or fixed_pattern_owns_text
        )
    if _tokens_start_file_read(tokens):
        if _contains_file_read_error(text):
            return False
        return (
            _contains_likely_dominant_file_read_output(text)
            or _starts_like_file_read_output(text)
            or _tokens_read_plain_text_file(tokens)
        )
    return False


def _text_has_exact_owner_output_against_later_compactable(
    tokens: list[str],
    text: str,
    *,
    exit_code: int | None = None,
) -> bool:
    if _tokens_start_source_search(tokens) or _tokens_pipe_to_source_search(tokens):
        patterns = _source_search_patterns(tokens)
        pattern_owns_text = _source_search_pattern_owns_text(tokens, text)
        if patterns and all(
            _source_search_pattern_is_compactable_signal(pattern) for pattern in patterns
        ):
            if _tokens_source_search_uses_fixed_strings(
                tokens
            ) and _source_search_fixed_pattern_context_owns_text(tokens, text):
                return True
            if _source_search_pattern_context_owns_text(tokens, text):
                return True
            if _source_search_heading_block_owns_text(tokens, text) and any(
                pattern.lower() in {"error", "failed", "traceback"}
                for pattern in patterns
            ):
                return True
            return (
                _contains_multiple_likely_source_search_lines(text)
                and not _contains_compactable_anchor_outside_source_search_lines(text)
            ) or pattern_owns_text
        if _contains_compactable_anchor_outside_source_search_lines(
            text
        ) and not _source_search_pattern_owns_non_source_line(tokens, text):
            return False
        if _source_search_heading_block_owns_text(
            tokens,
            text,
        ):
            return True
        if pattern_owns_text:
            return True
    if _tokens_start_file_read(tokens) or _tokens_pipe_to_file_read(tokens):
        return _file_read_output_owns_against_later_compactable(
            tokens,
            text,
            exit_code=exit_code,
        )
    return _text_has_exact_owner_output_for_tokens(tokens, text)


def _exact_fallback_segment_owns_output(tokens: list[str], text: str) -> bool:
    if _tokens_start_source_search(tokens) or _tokens_pipe_to_source_search(tokens):
        return _contains_likely_source_search_output(text) or _source_search_pattern_owns_text(
            tokens,
            text,
        )
    if _tokens_start_file_read(tokens) or _tokens_pipe_to_file_read(tokens):
        if _contains_file_read_error_for_tokens(tokens, text):
            return False
        return (
            _contains_likely_dominant_file_read_output(text)
            or _starts_like_file_read_output(text)
            or _tokens_read_plain_text_file(tokens)
        )
    return False


def _file_read_output_owns_against_later_compactable(
    tokens: list[str],
    text: str,
    *,
    exit_code: int | None = None,
) -> bool:
    if _contains_file_read_error(text):
        return False
    if _contains_later_command_output_anchor(text, exit_code=exit_code):
        return _file_read_plain_context_owns_compactable_literal(text)
    return (
        _contains_likely_dominant_file_read_output(text)
        or _starts_like_file_read_output(text)
        or _tokens_read_plain_text_file(tokens)
    )


def _contains_later_command_output_anchor(
    text: str,
    *,
    exit_code: int | None = None,
) -> bool:
    failure_exit = exit_code not in {None, 0}
    return bool(
        re.search(
            r"(?im)^[^\S\r\n]*(?:npm|pnpm|yarn|pip|uv|apt(?:-get)?|docker|pytest)\b.*"
            r"(?:noise|progress|err|error|warn|failed|passed|install|sync|build|after|before)",
            text,
        )
        or re.search(r"(?im)^\s*err_pnpm(?:_|\b)|^\s*warn\s+", text)
        or re.search(r"(?im)^(?:added|removed|changed|audited)\s+\d+\s+packages?\b", text)
        or re.search(r"(?im)^\s*found\s+0\s+vulnerabilities\b", text)
        or re.search(r"(?im)^\s*(?:setting up|successfully installed)\b", text)
        or re.search(r"(?im)\b\d+\s+upgraded,\s+\d+\s+newly installed\b", text)
        or re.search(r"(?im)^\s*(?:resolved|audited)\s+\d+\b", text)
        or re.search(
            r"(?im)^(?:get|hit|ign):\d+\b|^\s*fetched\s+\d+|"
            r"^\s*reading package lists\b|^\s*building dependency tree\b|"
            r"^\s*reading state information\b",
            text,
        )
        or re.search(
            r"(?im)(?:^|\|)\s*(?:fetched\s+\d+|reading package lists\b|"
            r"building dependency tree\b|reading state information\b|setting up\b)",
            text,
        )
        or re.search(r"(?im)(?:^|\|)\s*\d+%\s+\[[^\]]+\]", text)
        or re.search(r"(?im)(?:^|\|)\s*(?:get|hit|ign):\d+\b", text)
        or re.search(
            r"(?im)^#\d+\s+.*\b"
            r"(?:done|cached|load build definition|load metadata|load \.dockerignore|"
            r"transferring dockerfile|exporting|writing image|error|"
            r"failed to solve)\b",
            text,
        )
        or re.search(
            r"(?im)(?:^|\|)\s*#\d+\s+.*\b"
            r"(?:done|cached|load build definition|load metadata|load \.dockerignore|"
            r"transferring dockerfile|exporting|writing image|dockerfile|error)\b",
            text,
        )
        or re.search(
            r"(?im)(?:^|\|)\s*=>\s+.*\b"
            r"(?:done|cached|load build definition|load metadata|load \.dockerignore|"
            r"transferring dockerfile|exporting|writing image|dockerfile|error)\b",
            text,
        )
        or re.search(r"(?im)^\s*\d+\s+passed(?:\s+in\s+[\d.]+s)?\s*$", text)
        or re.search(r"(?im)^tests?/.*::.*\bPASSED\b", text)
        or (
            failure_exit
            and bool(
                re.search(r"(?im)^={2,}.*(?:failures|errors|short test summary)", text)
                or re.search(r"(?im)^failed\s+tests?/.*::", text)
                or re.search(
                    r"(?i)\b(?:no matching distribution found|resolutionimpossible|"
                    r"unable to locate package|could not build wheels|failed building wheel)\b",
                    text,
                )
                or re.search(r"(?im)^(?:e:|err:|error:)\s+\S", text)
                or re.search(r"(?i)\b(?:typeerror|referenceerror|syntaxerror|rangeerror):", text)
                or re.search(r"(?im)^\s+at\s+.+\(.+\)", text)
            )
        )
    )


def _or_fallback_exact_left_succeeded(tokens: list[str], text: str) -> bool:
    if _contains_file_read_error(text):
        return False
    if _tokens_start_file_read(tokens) or _tokens_pipe_to_file_read(tokens):
        if _tokens_hide_stderr_from_capture(tokens):
            return _contains_likely_file_read_output(text) or _starts_like_file_read_output(text)
        return True
    if _tokens_start_source_search(tokens) or _tokens_pipe_to_source_search(tokens):
        return _contains_likely_source_search_output(text) or (
            _tokens_source_search_uses_fixed_strings(tokens)
            and (
                _source_search_fixed_pattern_dominates_text(tokens, text)
                or _source_search_heading_block_owns_text(tokens, text)
            )
        )
    return _text_has_exact_owner_output_for_tokens(tokens, text)


def _contains_file_read_error(text: str) -> bool:
    reader_names = "|".join(sorted(SOURCE_READ_COMMANDS))
    prefixed_error = re.compile(
        rf"(?i)\b(?:{reader_names}):\s+.*"
        r"(?:no such file|cannot open|can't read|not a directory|permission denied|"
        r"cannot access)",
    )
    generic_reader_error = re.compile(
        r"(?i)(?:no such file or directory|cannot open .+ for reading|"
        r"can't read|permission denied|cannot access)",
    )
    unprefixed_reader_error = re.compile(
        r"(?i)^\s*[\w./~-]+:\s+.*(?:no such file or directory|cannot open|"
        r"can't read|permission denied|cannot access)"
    )
    path_like = re.compile(
        r"(?i)(?:[\w./~-]+\.(?:py|pyi|js|jsx|ts|tsx|md|txt|toml|ya?ml|json|"
        r"sh|css|html|go|rs|java|c|cc|cpp|cxx|h|hpp|rb|php|sql)|[\w./~-]+/)",
    )
    for line in text.splitlines()[:12]:
        if prefixed_error.search(line) or unprefixed_reader_error.search(line):
            return True
        if generic_reader_error.search(line) and path_like.search(line):
            return True
    return False


def _contains_file_read_error_for_tokens(tokens: list[str], text: str) -> bool:
    tokens = _strip_command_wrappers(tokens)
    runner_tokens = _command_runner_payload(tokens)
    if runner_tokens is not None:
        tokens = runner_tokens
    if not tokens:
        return False
    command_name = Path(tokens[0]).name.lower()
    candidate_paths: set[str] = set()
    skip_next = False
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if _is_unquoted_shell_separator(token) or (
            token in {"<", ">", ">>", "2>", "2>>"}
            and not getattr(token, "redirection_operator_was_quoted", False)
        ):
            continue
        if token.startswith("-"):
            option_name = token.split("=", 1)[0]
            if option_name in OPTIONS_WITH_VALUES and "=" not in token:
                skip_next = True
            continue
        candidate_paths.add(Path(token).name.lower())
        candidate_paths.add(token.lower())
    if not candidate_paths:
        return False
    error_re = re.compile(
        r"(?i)(?:no such file or directory|cannot open|can't read|permission denied|cannot access)"
    )
    for line in text.splitlines():
        if not error_re.search(line):
            continue
        line_l = line.lower()
        if f"{command_name}:" in line_l and any(path in line_l for path in candidate_paths):
            return True
        if any(path in line_l for path in candidate_paths):
            return True
    return False


def _is_likely_source_search_line(line: str) -> bool:
    return bool(
        re.match(r"^[\w./-]+(?::\d+){1,2}:", line)
        or re.match(
            r"^(?:[\w./-]+/)?[\w.-]+(?:\.[\w.-]+|file|makefile|dockerfile)$",
            line,
            re.IGNORECASE,
        )
    )


def _contains_multiple_likely_source_search_lines(text: str) -> bool:
    count = 0
    for line in text.splitlines():
        if _is_likely_source_search_line(line):
            count += 1
            if count >= 3:
                return True
    return False


def _source_search_heading_block_owns_text(tokens: list[str], text: str) -> bool:
    patterns = [
        pattern.strip().strip("'\"").lower()
        for pattern in _source_search_patterns(tokens)
        if len(pattern.strip().strip("'\"")) >= 3
    ]
    if not patterns:
        return False
    owned_lines = 0
    pattern_lines = 0
    context_lines = 0
    allow_hidden_context = _tokens_source_search_hides_filenames(tokens)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _is_likely_source_search_line(stripped):
            owned_lines += 1
            continue
        line_l = stripped.lower()
        if any(pattern in line_l for pattern in patterns):
            owned_lines += 1
            pattern_lines += 1
            continue
        if allow_hidden_context and pattern_lines > 0:
            context_lines += 1
            continue
        return False
    if pattern_lines >= 1 and owned_lines >= 3 and context_lines == 0:
        return True
    return allow_hidden_context and pattern_lines >= 2 and owned_lines + context_lines >= 3


def _contains_compactable_anchor_outside_source_search_lines(text: str) -> bool:
    remainder_lines: list[str] = []
    for line in text.splitlines():
        if _is_likely_source_search_line(line):
            continue
        remainder_lines.append(line)
    remainder = "\n".join(remainder_lines)
    return bool(
        _first_pattern_match(remainder, PACKAGE_PATTERNS)
        or _first_pattern_match(remainder, NODE_PATTERNS)
        or _first_pattern_match(remainder, DOCKER_BUILD_PRESERVATION_PATTERNS)
        or re.search(
            r"(?im)^(?:npm|pip|uv|docker|node|pytest|pnpm|yarn)\b.*(?:progress|err|error|failed|passed)",
            remainder,
        )
        or re.search(r"(?im)^(?:added|removed|changed|audited)\s+\d+\s+packages?\b", remainder)
        or re.search(
            r"(?im)^\s*(?:found\s+0\s+vulnerabilities|\d+\s+vulnerabilities?)\b",
            remainder,
        )
        or re.search(r"(?im)^(?:e:|err:|error:)\s+\S", remainder)
        or re.search(r"(?im)(?:^|\|)\s*\d+%\s+\[[^\]]+\].*\bapt\b", remainder)
        or re.search(r"(?im)(?:^|\|)\s*(?:get|hit|ign):\d+\b", remainder)
        or re.search(r"(?im)(?:^|\|)\s*\d+%\s+\[[^\]]+\]", remainder)
        or re.search(r"(?im)(?:^|\|)\s*reading package lists\b", remainder)
        or re.search(r"(?im)(?:^|\|)\s*building dependency tree\b", remainder)
        or re.search(r"(?im)(?:^|\|)\s*reading state information\b", remainder)
        or re.search(r"(?im)(?:^|\|)\s*fetched\s+\d+", remainder)
        or re.search(r"(?im)(?:^|\|)\s*setting up\b", remainder)
        or re.search(r"(?im)(?:^|\|)\s*step\s+\d+/\d+\s*:", remainder)
        or re.search(
            r"(?im)(?:^|\|)\s*#\d+\s+.*\b"
            r"(?:done|cached|load build definition|load metadata|load \.dockerignore|"
            r"transferring dockerfile|exporting|writing image|dockerfile|error)\b",
            remainder,
        )
        or re.search(
            r"(?im)(?:^|\|)\s*=>\s+.*\b"
            r"(?:done|cached|load build definition|load metadata|load \.dockerignore|"
            r"transferring dockerfile|exporting|writing image|dockerfile|error)\b",
            remainder,
        )
        or re.search(
            r"(?i)\b(?:no matching distribution found|resolutionimpossible|"
            r"unable to locate package|could not build wheels)\b",
            remainder,
        )
        or re.search(r"(?im)^\s+at\s+.+\(.+\)", remainder)
        or re.search(r"(?im)^\s*(?:up to date|setting up)\b", remainder)
        or re.search(r"(?im)\b\d+\s+newly installed\b", remainder)
        or re.search(r"(?im)\b\d+\s+upgraded,\s+\d+\s+newly installed\b", remainder)
        or re.search(
            r"(?im)^(?:installing collected packages|successfully installed|"
            r"resolved\s+\d+|audited\s+\d+)\b",
            remainder,
        )
        or re.search(
            r"(?im)^#\d+\s+.*\b(?:load build definition|error|failed to solve)\b",
            remainder,
        )
        or re.search(r"(?im)failed to solve", remainder)
        or re.search(
            r"(?im)^\s*(?:=+\s*)?(?:\d+\s+(?:passed|failed|errors?)(?:,\s*)?)+\b",
            remainder,
        )
    )


def _looks_like_test_failure_output(text: str) -> bool:
    lowered = text.lower()
    has_short_traceback_detail = bool(
        re.search(r"(?im)^[\w./-]+\.py:\d+:\s+in\s+(?:\w+|<[^>]+>)", text)
        and (
            re.search(r"(?im)^\s*e\s+", text)
            or re.search(
                r"(?i)\b(?:assertionerror|modulenotfounderror|importerror|typeerror|"
                r"referenceerror|runtimeerror):",
                text,
            )
        )
    )
    has_traceback_detail = bool(
        re.search(r"(?im)^traceback \(most recent call last\):", text)
        and (
            re.search(r"(?im)^\s*file \".+\", line \d+", text)
            or has_short_traceback_detail
            or re.search(r"(?im)^\s*e\s+", text)
            or re.search(
                r"(?i)\b(?:assertionerror|modulenotfounderror|importerror|typeerror|"
                r"referenceerror|runtimeerror):",
                text,
            )
        )
    )
    return (
        "=== failures ===" in lowered
        or "failed tests/" in lowered
        or has_traceback_detail
        or has_short_traceback_detail
        or re.search(r"(?im)^tests?/.*::.*\b(?:passed|failed|error)\b", text) is not None
        or re.search(
            r"(?im)^\s*(?:=+\s*)?(?:\d+\s+(?:passed|failed|errors?)(?:,\s*)?)+\b",
            text,
        ) is not None
        or re.search(r"^tests/.+:\d+:\s*(?:assertionerror|error|failed)", text, re.MULTILINE)
        is not None
    )


def _tokens_read_plain_text_file(tokens: list[str]) -> bool:
    tokens = _strip_command_wrappers(tokens)
    runner_tokens = _command_runner_payload(tokens)
    if runner_tokens is not None:
        tokens = runner_tokens
    plain_extensionless_names = {
        "authors",
        "changelog",
        "codeowners",
        "contributing",
        "copying",
        "license",
        "notice",
        "readme",
    }
    for token in tokens[1:]:
        if not token or token.startswith("-"):
            continue
        path = Path(token)
        suffix = path.suffix.lower()
        if suffix in {".md", ".rst", ".txt"}:
            return True
        if not suffix and path.name.lower() in plain_extensionless_names:
            return True
    return False


def _text_has_dominant_exact_output_for_tokens(tokens: list[str], text: str) -> bool:
    if _tokens_start_source_search(tokens) or _tokens_pipe_to_source_search(tokens):
        return (
            _tokens_source_search_hides_filenames(tokens)
            or _contains_likely_source_search_output(text)
            or _source_search_pattern_appears_in_text(tokens, text)
        )
    if _tokens_start_file_read(tokens):
        return _contains_likely_dominant_file_read_output(text)
    return False


def _source_search_options_with_values(tokens: list[str]) -> frozenset[str] | set[str]:
    command_name = Path(tokens[0]).name if tokens else ""
    if command_name == "rg":
        return RG_OPTIONS_WITH_VALUES | {"-e", "--regexp"}
    if command_name == "grep":
        return GREP_OPTIONS_WITH_VALUES
    return SOURCE_SEARCH_OPTIONS_WITH_VALUES | {"-e", "--regexp"}


def _source_search_option_flags(tokens: list[str]) -> list[str]:
    flags: list[str] = []
    value_options = _source_search_options_with_values(tokens)
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            break
        if token in value_options:
            index += 2
            continue
        if token.startswith("--"):
            flags.append(token)
            index += 1
            continue
        if token.startswith("-"):
            short_flags = token[1:]
            for flag_index, flag in enumerate(short_flags):
                flags.append(f"-{flag}")
                if f"-{flag}" in value_options:
                    if flag_index == len(short_flags) - 1 and index + 1 < len(tokens):
                        index += 1
                    break
        index += 1
    return flags


def _tokens_request_ripgrep_help(tokens: list[str]) -> bool:
    flags = _source_search_option_flags(tokens)
    return bool(tokens and Path(tokens[0]).name == "rg" and {"-h", "--help"} & set(flags))


def _tokens_source_search_hides_filenames(tokens: list[str]) -> bool:
    tokens = _strip_command_wrappers(tokens)
    if tokens and Path(tokens[0]).name in {"bash", "sh", "zsh"}:
        shell_command = _shell_c_argument(tokens[1:])
        if shell_command is None:
            return False
        return any(
            _tokens_source_search_hides_filenames(segment)
            for segment in _command_token_segments(shell_command)
            if segment
        )
    runner_tokens = _command_runner_payload(tokens)
    if runner_tokens is not None:
        return _tokens_source_search_hides_filenames(runner_tokens)
    if not tokens:
        return False
    command_name = Path(tokens[0]).name
    if command_name == "git":
        subcommand = _git_subcommand_tokens(tokens)
        return bool(
            subcommand
            and subcommand[0] == "grep"
            and _tokens_source_search_hides_filenames(subcommand)
        )
    flags = _source_search_option_flags(tokens)
    if command_name == "rg":
        hidden_flags = {"-I", "--no-filename"}
        visible_flags = {"-H", "--with-filename"}
    elif command_name == "grep":
        hidden_flags = {"-h", "--no-filename"}
        visible_flags = {"-H", "--with-filename"}
    else:
        hidden_flags = {"-h", "--no-filename"}
        visible_flags = {"--with-filename"}
    hidden: bool | None = None
    for flag in flags:
        if flag in hidden_flags:
            hidden = True
        elif flag in visible_flags:
            hidden = False
    return hidden is True


def _source_search_pattern_is_compactable_signal(pattern: str) -> bool:
    normalized = pattern.strip().strip("'\"").lower()
    if normalized in {
        "added",
        "audited",
        "dockerfile",
        "downloaded",
        "error",
        "errors",
        "exporting",
        "fail",
        "failed",
        "failure",
        "found 0 vulnerabilities",
        "installed",
        "load .dockerignore",
        "load metadata",
        "newly installed",
        "no matching distribution found",
        "passed",
        "prepared",
        "reading package lists",
        "reading state information",
        "resolved",
        "solve",
        "successfully installed",
        "building dependency tree",
        "up to date",
        "writing image",
    }:
        return True
    return bool(
        re.search(r"\b\d+\s+(passed|failed|errors?)\b", normalized)
        or re.search(r"\b(passed|failed)\s+in\b", normalized)
        or re.search(r"\b(added|removed|changed|audited)\s+\d+\s+packages?\b", normalized)
        or re.search(r"\bfound\s+0\s+vulnerabilities\b", normalized)
        or re.search(r"\b(successfully\s+installed|installed\s+\S+)", normalized)
        or re.search(r"\b(resolved|downloaded|prepared)\s+\d+\s+packages?\b", normalized)
        or re.search(r"\b(up\s+to\s+date|failed\s+to\s+solve)\b", normalized)
        or re.search(r"\bnpm\s+err!", normalized)
        or re.search(
            r"\b(?:no matching distribution found|resolutionimpossible|"
            r"unable to locate package|could not build wheels|eresolve)\b",
            normalized,
        )
        or re.search(r"\b\d+\s+upgraded,\s+\d+\s+newly installed\b", normalized)
        or re.search(
            r"\b(load\s+build\s+definition\s+from\s+dockerfile|load\s+metadata|"
            r"load\s+\.dockerignore|exporting|writing\s+image|dockerfile.*error)\b",
            normalized,
        )
        or re.search(
            r"\b(reading\s+package\s+lists|building\s+dependency\s+tree|"
            r"reading\s+state\s+information|setting\s+up)\b",
            normalized,
        )
        or re.search(r"\bnpm\s+err!\b", normalized)
    )


def _source_search_pattern_is_traceback_path(pattern: str) -> bool:
    normalized = pattern.strip().strip("'\"").lower()
    return bool(
        re.fullmatch(r"[\w./-]*[\w-]+\.py(?::\d+)?", normalized)
        or re.fullmatch(r"[\w./-]*[\w-]+\.py:\d+:.*", normalized)
    )


def _source_search_pattern_owns_text(tokens: list[str], text: str) -> bool:
    patterns = _source_search_patterns(tokens)
    if not patterns:
        return False
    text_l = text.lower()
    looks_like_test_failure = _looks_like_test_failure_output(text)
    return any(
        len(pattern) >= 3
        and (
            pattern.lower() in text_l
            or _safe_source_search_regex_matches_text(tokens, pattern, text)
        )
        and not _source_search_pattern_is_compactable_signal(pattern)
        and not (looks_like_test_failure and _source_search_pattern_is_traceback_path(pattern))
        for pattern in patterns
    )


def _safe_source_search_regex_matches_text(
    tokens: list[str],
    pattern: str,
    text: str,
) -> bool:
    if _tokens_source_search_uses_fixed_strings(tokens) or pattern.count(".*") != 1:
        return False
    ignore_case = bool(
        {"-i", "--ignore-case"} & set(_source_search_option_flags(tokens))
    )
    prefix, suffix = pattern.split(".*")
    if ignore_case:
        prefix = prefix.casefold()
        suffix = suffix.casefold()
    if len(prefix) + len(suffix) < 3 or any(
        char in r"\[]^$*.+?(){}|" for char in prefix + suffix
    ):
        return False
    for line in text.splitlines():
        searchable_line = line.casefold() if ignore_case else line
        start = searchable_line.find(prefix)
        if start >= 0 and suffix in searchable_line[start + len(prefix) :]:
            return True
    return False


def _tokens_source_search_uses_fixed_strings(tokens: list[str]) -> bool:
    tokens = _strip_command_wrappers(tokens)
    runner_tokens = _command_runner_payload(tokens)
    if runner_tokens is not None:
        return _tokens_source_search_uses_fixed_strings(runner_tokens)
    return any(
        token in {"-F", "--fixed-strings"}
        or (
            token.startswith("-")
            and not token.startswith("--")
            and "F" in token[1:]
        )
        for token in tokens[1:]
    )


def _source_search_pattern_owns_non_source_line(
    tokens: list[str],
    text: str,
    *,
    allow_compactable_signals: bool = False,
) -> bool:
    patterns = [
        pattern
        for pattern in _source_search_patterns(tokens)
        if len(pattern) >= 3
        and (allow_compactable_signals or not _source_search_pattern_is_compactable_signal(pattern))
    ]
    if not patterns:
        return False
    looks_like_test_failure = _looks_like_test_failure_output(text)
    for line in text.splitlines():
        if _is_likely_source_search_line(line.strip()):
            continue
        line_l = line.lower()
        for pattern in patterns:
            if (
                pattern.lower() in line_l
                or _safe_source_search_regex_matches_text(tokens, pattern, line)
            ) and not (
                looks_like_test_failure and _source_search_pattern_is_traceback_path(pattern)
            ):
                return True
    return False


def _source_search_pattern_owns_repeated_non_source_lines(
    tokens: list[str],
    text: str,
    *,
    allow_compactable_signals: bool = False,
) -> bool:
    patterns = [
        pattern.lower()
        for pattern in _source_search_patterns(tokens)
        if len(pattern) >= 3
        and (allow_compactable_signals or not _source_search_pattern_is_compactable_signal(pattern))
    ]
    if not patterns:
        return False
    looks_like_test_failure = _looks_like_test_failure_output(text)
    counts = {pattern: 0 for pattern in patterns}
    for line in text.splitlines():
        if _is_likely_source_search_line(line.strip()):
            continue
        if allow_compactable_signals and _line_looks_like_compactable_output(line):
            continue
        line_l = line.lower()
        for pattern in patterns:
            if pattern in line_l and not (
                looks_like_test_failure and _source_search_pattern_is_traceback_path(pattern)
            ):
                counts[pattern] += 1
                if counts[pattern] >= 2:
                    return True
    return False


def _source_search_fixed_pattern_dominates_text(tokens: list[str], text: str) -> bool:
    patterns = [pattern.lower() for pattern in _source_search_patterns(tokens) if len(pattern) >= 3]
    if not patterns:
        return False
    checked_lines = [line.strip().lower() for line in text.splitlines() if line.strip()]
    if not checked_lines:
        return False
    for pattern in patterns:
        hits = sum(1 for line in checked_lines if pattern in line)
        if hits >= 2 and hits / len(checked_lines) >= 0.8:
            return True
    return False


def _source_search_fixed_pattern_context_owns_text(tokens: list[str], text: str) -> bool:
    if not _tokens_source_search_uses_fixed_strings(tokens):
        return False
    return _source_search_pattern_context_owns_text(
        tokens,
        text,
        allow_test_summary=True,
    )


def _source_search_pattern_context_owns_text(
    tokens: list[str],
    text: str,
    *,
    allow_test_summary: bool = False,
) -> bool:
    patterns = [pattern.lower() for pattern in _source_search_patterns(tokens) if len(pattern) >= 3]
    if not patterns:
        return False
    if not allow_test_summary and any(
        re.search(r"\b\d+\s+(?:passed|failed|errors?)\b", pattern)
        or re.search(r"\b(?:passed|failed)\s+in\b", pattern)
        for pattern in patterns
    ):
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 3:
        return False
    pattern_lines = 0
    compactable_other_lines = 0
    plain_context_lines = 0
    for line in lines:
        line_l = line.lower()
        if any(pattern in line_l for pattern in patterns):
            pattern_lines += 1
            continue
        if _line_looks_like_compactable_context(line):
            compactable_other_lines += 1
            continue
        plain_context_lines += 1
    return pattern_lines >= 1 and compactable_other_lines == 0 and plain_context_lines >= 2


def _file_read_plain_context_owns_compactable_literal(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 3:
        return False
    compactable_lines = sum(1 for line in lines if _line_looks_like_compactable_context(line))
    plain_lines = len(lines) - compactable_lines
    return compactable_lines <= max(2, len(lines) // 10) and plain_lines >= 2


def _line_looks_like_compactable_context(line: str) -> bool:
    stripped = line.strip()
    return bool(
        _line_looks_like_compactable_output(stripped)
        or re.search(r"(?i)^#\d+\b", stripped)
        or re.search(r"(?i)^=>\s", stripped)
        or re.search(r"(?i)^tests?/.*::.*\b(?:passed|failed|error)\b", stripped)
        or re.search(r"(?i)(?:^|\|)\s*#\d+\b", stripped)
        or re.search(r"(?i)(?:^|\|)\s*=>\s", stripped)
        or re.search(r"(?i)(?:^|\|)\s*\d+%\s+\[[^\]]+\]", stripped)
        or re.search(r"(?i)(?:^|\|)\s*(?:get|hit|ign):\d+\b", stripped)
        or re.search(
            r"(?i)(?:^|\|)\s*(?:reading package lists|building dependency tree|"
            r"reading state information|fetched\s+\d+|setting up|"
            r"successfully installed|audited\s+\d+)\b",
            stripped,
        )
        or re.search(r"(?i)\b\d+\s+upgraded,\s+\d+\s+newly installed\b", stripped)
    )


def _line_looks_like_compactable_output(line: str) -> bool:
    stripped = line.strip()
    return bool(
        re.search(r"(?i)^(?:npm|pnpm|yarn|pip|uv|apt(?:-get)?|docker|pytest)\b", stripped)
        or re.search(r"(?i)^(?:npm err!|pnpm err!|yarn.*error|err_pnpm)\b", stripped)
        or re.search(r"(?i)^warn\s+", stripped)
        or re.search(
            r"(?i)^(?:e:|err:|error:)\s+(?:no matching distribution found|"
            r"could not build wheels|unable to locate package|failed|error|"
            r"cannot find module)\b",
            stripped,
        )
        or re.search(
            r"(?i)^(?:added|removed|changed|audited)\s+\d+\s+packages?"
            r"(?:\s+in\s+[\d.]+s)?$",
            stripped,
        )
        or re.search(r"(?i)^found\s+0\s+vulnerabilities$", stripped)
        or re.search(r"(?i)^up\s+to\s+date$", stripped)
        or re.search(r"(?i)^\d+\s+(?:passed|failed|errors?)(?:\s+in\s+[\d.]+s)?$", stripped)
        or re.search(r"(?i)^failed\s+to\s+solve\b", stripped)
        or re.search(
            r"(?i)^(?:resolved|downloaded|prepared)\s+\d+\s+packages?"
            r"(?:\s+in\s+[\d.]+\w+)?$",
            stripped,
        )
    )


def _source_search_patterns(tokens: list[str]) -> list[str]:
    tokens = _strip_command_wrappers(tokens)
    if tokens and Path(tokens[0]).name in {"bash", "sh", "zsh"}:
        shell_command = _shell_c_argument(tokens[1:])
        if shell_command is None:
            return []
        patterns: list[str] = []
        for segment in _command_token_segments(shell_command):
            patterns.extend(_source_search_patterns(segment))
        return patterns
    runner_tokens = _command_runner_payload(tokens)
    if runner_tokens is not None:
        return _source_search_patterns(runner_tokens)
    if not tokens or Path(tokens[0]).name not in SOURCE_SEARCH_COMMANDS | {"git"}:
        return []
    if Path(tokens[0]).name == "git":
        subcommand = _git_subcommand_tokens(tokens)
        if subcommand and subcommand[0] == "grep":
            return _source_search_patterns(subcommand)
        return []

    if any(
        token in {"--files", "--type-list"}
        or (token.startswith("-") and not token.startswith("--") and "f" in token[1:])
        for token in tokens[1:]
    ):
        return []

    if any(
        (
            token == "-l"
            or (token.startswith("-") and not token.startswith("--") and "l" in token[1:])
            or token in {"--files-with-matches", "--files-without-match"}
        )
        for token in tokens[1:]
    ):
        return []

    explicit_patterns: list[str] = []
    has_pattern_file = False
    for index, token in enumerate(tokens[1:], start=1):
        if token in {"-f", "--file"} and index + 1 < len(tokens):
            has_pattern_file = True
            continue
        if token.startswith("-f") and not token.startswith("--") and len(token) > 2:
            has_pattern_file = True
            continue
        if token.startswith("--file="):
            has_pattern_file = True
            continue
        if token in {"-e", "--regexp"} and index + 1 < len(tokens):
            explicit_patterns.append(tokens[index + 1].strip())
            continue
        if token.startswith("-e") and not token.startswith("--") and len(token) > 2:
            explicit_patterns.append(token[2:].strip())
            continue
        if token.startswith("--regexp="):
            explicit_patterns.append(token.split("=", 1)[1].strip())
            continue
    if explicit_patterns:
        return explicit_patterns
    if has_pattern_file:
        return []

    value_options = _source_search_options_with_values(tokens)
    option_value_next = False
    for token in tokens[1:]:
        if option_value_next:
            option_value_next = False
            continue
        if token == "--":
            continue
        if token in value_options:
            option_value_next = True
            continue
        if token.startswith("--") and "=" in token:
            continue
        if token.startswith("--") and "=" not in token:
            continue
        if token.startswith("-") and not token.startswith("--"):
            continue
        pattern = token.strip()
        if len(pattern) < 3 or pattern in {".", "*"}:
            continue
        return [pattern]
    return []


def _source_search_pattern_appears_in_text(tokens: list[str], text: str) -> bool:
    text_l = text.lower()
    return any(
        len(pattern) >= 3 and pattern.lower() in text_l
        for pattern in _source_search_patterns(tokens)
    )


def _contains_likely_source_search_output(text: str) -> bool:
    path_match = re.compile(
        r"^(?:"
        r"[\w./~-]+\."
        r"(?:py|pyi|js|jsx|ts|tsx|md|txt|toml|ya?ml|json|sh|css|html|go|rs|java|"
        r"c|cc|cpp|cxx|h|hpp|rb|php|kt|kts|swift|scala|cs|sql|mod|sum|lock)"
        r"|[\w./~-]*/[\w.~+-]+"
        r"|[\w.~+-]+:\d+"
        r"|(?:Makefile|Dockerfile|Containerfile|Rakefile|Gemfile|Procfile|BUILD|WORKSPACE)"
        r")(?::\d+)?[:\t ]",
        re.IGNORECASE,
    )
    path_only_match = re.compile(
        r"^(?:"
        r"[\w./~-]+\."
        r"(?:py|pyi|js|jsx|ts|tsx|md|txt|toml|ya?ml|json|sh|css|html|go|rs|java|"
        r"c|cc|cpp|cxx|h|hpp|rb|php|kt|kts|swift|scala|cs|sql|mod|sum|lock)"
        r"|[\w./~-]*/[\w.~+-]+"
        r"|[\w.~+-]+"
        r"|(?:Makefile|Dockerfile|Containerfile|Rakefile|Gemfile|Procfile|BUILD|WORKSPACE)"
        r")$",
        re.IGNORECASE,
    )
    return any(
        path_match.match(line) or path_only_match.match(line.strip())
        for line in text.splitlines()
    )


def _contains_likely_file_read_output(text: str) -> bool:
    code_markers = (
        "#!",
        "# ",
        "// ",
        "/*",
        "def ",
        "class ",
        "import ",
        "from ",
        "export ",
        "const ",
        "let ",
        "var ",
        "function ",
        "package ",
        "module ",
        "[",
        "{",
        "<",
    )
    return any(line.lstrip().startswith(code_markers) for line in text.splitlines())


def _starts_like_file_read_output(text: str) -> bool:
    code_markers = (
        "#!",
        "# ",
        "// ",
        "/*",
        "def ",
        "class ",
        "import ",
        "from ",
        "export ",
        "const ",
        "let ",
        "var ",
        "function ",
        "package ",
        "module ",
        "[",
        "{",
        "<",
    )
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped:
            continue
        return stripped.startswith(code_markers)
    return False


def _contains_likely_dominant_file_read_output(text: str) -> bool:
    code_markers = (
        "#!",
        "# ",
        "// ",
        "/*",
        "def ",
        "class ",
        "import ",
        "from ",
        "export ",
        "const ",
        "let ",
        "var ",
        "function ",
        "package ",
        "module ",
        "[",
        "{",
        "<",
    )
    marker_lines = [
        line for line in text.splitlines() if line.lstrip().startswith(code_markers)
    ]
    return len(marker_lines) >= 3


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
    if _tokens_request_ripgrep_help(tokens):
        return False
    if tokens and Path(tokens[0]).name == "git":
        rest = _git_subcommand_tokens(tokens)
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
    if _tokens_pipe_to_file_read(tokens):
        return True
    if not tokens:
        return False
    token_name = Path(tokens[0]).name
    if token_name in {"fd", "fdfind"}:
        return _fd_attached_exec_reads_files(tokens)
    if token_name == "git":
        return _looks_like_git_show_file_read_tokens(tokens)
    if token_name == "sed":
        return _looks_like_sed_file_read_tokens(tokens)
    if token_name in {"jq", "yq"}:
        return _looks_like_jq_file_read_tokens(tokens)
    if token_name == "tail" and _tail_follows(tokens[1:]):
        return not _tail_follow_targets_are_log_like(tokens[1:])
    return token_name in SOURCE_READ_COMMANDS


def _fd_attached_exec_reads_files(tokens: list[str]) -> bool:
    consumers = []
    for token in tokens[1:]:
        if token == "--":
            break
        if token in {"-x", "-X", "--exec", "--exec-batch"}:
            return False
        option, separator, consumer = token.partition("=")
        if option not in {"--exec", "--exec-batch"} or not separator:
            continue
        consumers.append(consumer)
    return len(consumers) == 1 and consumers[0] in {"cat", "/bin/cat", "/usr/bin/cat"}


def _tokens_start_fd_attached_file_read(tokens: list[str]) -> bool:
    stripped = _strip_command_wrappers(tokens)
    seen: set[tuple[str, ...]] = set()
    while stripped and tuple(stripped) not in seen:
        seen.add(tuple(stripped))
        payload = _command_runner_payload(stripped)
        if payload is not None:
            stripped = _strip_command_wrappers(payload)
            continue
        if stripped and Path(stripped[0]).name in {"bash", "sh", "zsh"}:
            shell_command = _shell_c_argument(stripped[1:])
            if shell_command is not None:
                shell_payload = next(
                    (
                        segment
                        for _separator, segment in _background_segments(shell_command)
                        if segment and not _is_setup_segment(segment)
                    ),
                    [],
                )
                stripped = _strip_command_wrappers(shell_payload)
                continue
        break
    return bool(
        stripped
        and Path(stripped[0]).name in {"fd", "fdfind"}
        and _fd_attached_exec_reads_files(stripped)
    )


def _shell_starts_fd_attached_file_read(command: str) -> bool:
    for _separator, tokens in _background_segments(command):
        if not tokens or _is_setup_segment(tokens):
            continue
        return _tokens_start_fd_attached_file_read(tokens)
    return False


def _looks_like_jq_file_read_tokens(tokens: list[str]) -> bool:
    if not tokens or Path(tokens[0]).name not in {"jq", "yq"}:
        return False
    command_name = Path(tokens[0]).name
    null_input = False
    file_filter = False
    explicit_file_option = False
    argument_mode = False
    positional: list[str] = []
    option_arities = JQ_OPTION_ARITIES | (YQ_OPTION_ARITIES if command_name == "yq" else {})
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            remaining = tokens[index + 1 :]
            if argument_mode and not positional and not file_filter and remaining:
                positional.append(remaining[0])
            elif not argument_mode:
                positional.extend(remaining)
            break
        if token in {"--help", "--version"}:
            return False
        if token.startswith("--null-input="):
            return False
        if token == "--null-input":
            null_input = True
            index += 1
            continue
        if token in {"--args", "--jsonargs"}:
            argument_mode = True
            index += 1
            continue
        if token in option_arities:
            arity = option_arities[token]
            operands = tokens[index + 1 : index + 1 + arity]
            if len(operands) != arity:
                return False
            explicit_file_option |= token in JQ_FILE_OPTIONS
            file_filter |= command_name == "jq" and token in {"-f", "--from-file"}
            index += arity + 1
            continue
        option_name = token.split("=", 1)[0]
        if command_name == "yq" and "=" in token and option_name in option_arities:
            index += 1
            continue
        if command_name == "yq" and token.startswith(("-I", "-f", "-o", "-p")) and len(token) > 2:
            index += 1
            continue
        if token.startswith("-L") and not token.startswith("--") and len(token) > 2:
            index += 1
            continue
        if token.startswith("-") and not token.startswith("--"):
            flags = token[1:]
            flag_index = 0
            while flag_index < len(flags):
                flag = flags[flag_index]
                if flag == "n":
                    null_input = True
                if flag == "L":
                    if flag_index + 1 == len(flags):
                        if index + 1 >= len(tokens):
                            return False
                        index += 1
                    break
                flag_index += 1
            index += 1
            continue
        if token.startswith("--"):
            index += 1
            continue
        if not argument_mode or (not positional and not file_filter):
            positional.append(token)
        index += 1
    yq_subcommand = bool(
        command_name == "yq"
        and positional
        and positional[0] in {"e", "ea", "eval", "eval-all"}
    )
    if yq_subcommand:
        positional = positional[1:]
    if explicit_file_option:
        return True
    if null_input:
        return False
    if file_filter:
        return bool(positional)
    if command_name == "yq" and len(positional) == 1:
        operand = positional[0]
        if operand.startswith(".") and not operand.startswith(("./", "../")):
            return False
        return Path(operand).suffix.lower() in YQ_INPUT_SUFFIXES
    return len(positional) >= 2


def _tokens_start_compactable_command(tokens: list[str]) -> bool:
    tokens = _strip_command_wrappers(tokens)
    if not tokens:
        return False
    if _is_log_stream_command(shlex.join(tokens)):
        return True
    runner_tokens = _command_runner_payload(tokens)
    if runner_tokens is not None:
        return _tokens_start_compactable_command(runner_tokens)
    token_name = Path(tokens[0]).name
    if token_name in {"apt", "apt-get", "docker", "npm", "pnpm", "yarn"}:
        return True
    if token_name in {"uv", "pip", "pip3", "pytest", "py.test", "node"}:
        return True
    python_module = _python_module_invocation(tokens)
    return bool(python_module is not None and python_module[0] in {"pip", "pytest", "py.test"})


def _tokens_start_find_path_list(tokens: list[str]) -> bool:
    tokens = _strip_command_wrappers(tokens)
    if not tokens or Path(tokens[0]).name != "find":
        return False
    return "-exec" not in tokens


def _tokens_pipe_to_file_read(tokens: list[str]) -> bool:
    tokens = _strip_command_wrappers(tokens)
    for index, token in enumerate(tokens[:-1]):
        if token in {"|", "|&"} and not getattr(token, "was_quoted", False):
            downstream = _strip_command_wrappers(tokens[index + 1 :])
            upstream = tokens[:index]
            if _tokens_start_compactable_command(upstream):
                continue
            if not (
                _tokens_start_file_read(upstream)
                or _tokens_start_find_path_list(upstream)
                or _tokens_start_source_search(upstream)
            ):
                continue
            payload = _command_runner_payload(downstream)
            if payload is not None:
                downstream = payload
            if _tokens_start_file_read(downstream):
                return True
        if Path(tokens[0]).name == "find" and token == "-exec":
            exec_tokens = tokens[index + 1 :]
            if _tokens_start_file_read(exec_tokens):
                return True
    return False


def _tokens_start_direct_log_stream(tokens: list[str]) -> bool:
    tokens = _strip_command_wrappers(tokens)
    if not tokens:
        return False
    command_name = Path(tokens[0]).name
    if command_name == "journalctl":
        return True
    if command_name != "kubectl":
        return False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-n", "--namespace"}:
            index += 2
            continue
        if token.startswith("--namespace=") or (
            token.startswith("-n") and len(token) > 2
        ):
            index += 1
            continue
        break
    return index < len(tokens) and tokens[index] == "logs"


def _tokens_pipe_to_source_search(tokens: list[str]) -> bool:
    tokens = _strip_command_wrappers(tokens)
    for index, token in enumerate(tokens[:-1]):
        if token in {"|", "|&"} and not getattr(token, "was_quoted", False):
            downstream = tokens[index + 1 :]
            if _tokens_start_source_search(downstream):
                upstream = tokens[:index]
                if _tokens_start_direct_log_stream(upstream):
                    continue
                if _tokens_start_compactable_command(upstream):
                    continue
                return True
        if Path(tokens[0]).name == "find" and token == "-exec":
            exec_tokens = tokens[index + 1 :]
            if _tokens_start_source_search(exec_tokens):
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
        shell_command = _runner_shell_call_argument(tokens[1:])
        if shell_command is not None:
            return _shell_tokens(shell_command)
        return _skip_option_tokens(tokens[1:])
    if command == "xargs":
        return _xargs_payload_tokens(tokens)
    if command in {"npm", "pnpm", "yarn"}:
        rest = _skip_option_tokens(tokens[1:])
        if command == "yarn" and len(rest) >= 3 and rest[0] == "workspace":
            rest = rest[2:]
        if rest and rest[0] in {"dlx", "exec"}:
            shell_command = _runner_shell_call_argument(rest[1:])
            if shell_command is not None:
                return _shell_tokens(shell_command)
            return _skip_option_tokens(rest[1:])
    return None


def _runner_shell_call_argument(tokens: list[str]) -> str | None:
    value_options = {
        "-p",
        "--package",
        "-w",
        "--workspace",
        "--workspace-root",
        "--prefix",
        "--dir",
        "--filter",
    }
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return None
        if not token.startswith("-"):
            return None
        option_name = token.split("=", 1)[0]
        if token.startswith("--call="):
            return token.split("=", 1)[1]
        if token in {"-c", "--call"} and index + 1 < len(tokens):
            return tokens[index + 1]
        index += 1
        if option_name in value_options and "=" not in token and index < len(tokens):
            index += 1
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
        if _is_unquoted_shell_separator(token):
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


def _strip_grouping_tokens(tokens: list[str]) -> list[str]:
    while tokens and tokens[0] in {"(", "{"}:
        tokens = tokens[1:]
    while tokens and tokens[-1] in {")",
        "}",
    }:
        tokens = tokens[:-1]
    return tokens


def _strip_leading_redirections(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens):
        redirection = _simple_shell_redirection(tokens, index)
        if redirection is None:
            break
        consumed, _supplies_stdin = redirection
        if not consumed:
            return []
        index += consumed
    return tokens[index:]


def _is_gnu_timeout_duration(value: str) -> bool:
    return bool(re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[smhd])?", value, re.IGNORECASE))


def _timeout_short_option_span(tokens: list[str], index: int) -> int:
    token = tokens[index]
    if not token.startswith("-") or token.startswith("--") or len(token) < 3:
        return 0
    position = 1
    while position < len(token):
        option = token[position]
        if option == "v":
            position += 1
            continue
        if option in {"k", "s"}:
            if position + 1 < len(token):
                return 1
            return 2 if index + 1 < len(tokens) and tokens[index + 1] else 0
        return 0
    return 1


def _timeout_child_tokens(tokens: list[str]) -> list[str]:
    index = 0
    value_options = {"-k", "--kill-after", "-s", "--signal"}
    flag_options = {"-v", "--foreground", "--preserve-status", "--verbose"}
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if token in flag_options:
            index += 1
            continue
        option, separator, value = token.partition("=")
        if option in value_options and separator:
            if not value:
                return []
            index += 1
            continue
        if token in value_options:
            if index + 1 >= len(tokens) or not tokens[index + 1]:
                return []
            index += 2
            continue
        short_option_span = _timeout_short_option_span(tokens, index)
        if short_option_span:
            index += short_option_span
            continue
        if token.startswith("-"):
            return []
        break
    if index >= len(tokens) or not _is_gnu_timeout_duration(tokens[index]):
        return []
    return tokens[index + 1 :]


def _strip_command_wrappers(tokens: list[str]) -> list[str]:
    tokens = _strip_assignment_tokens(_strip_grouping_tokens(tokens))
    while tokens:
        tokens = _strip_leading_redirections(tokens)
        if not tokens:
            return []
        command = Path(tokens[0]).name
        command_name = Path(command).name
        if command_name == "sudo":
            tokens = _skip_sudo_options(tokens[1:])
            continue
        if command_name == "env":
            tokens = _strip_env_assignment_tokens(_skip_env_options(tokens[1:]))
            continue
        if command_name == "timeout":
            tokens = _timeout_child_tokens(tokens[1:])
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


def _is_valid_sudo_close_from(value: str) -> bool:
    if re.fullmatch(r"[ \t\n\v\f\r]*[+-]?[0-9]+", value) is None:
        return False
    try:
        parsed = int(value)
    except ValueError:
        return False
    return 3 <= parsed <= 2_147_483_647


def _is_valid_sudo_timeout(value: str) -> bool:
    if not value:
        return False
    suffixes = "dhms"
    suffix_index = 0
    total = 0
    index = 0
    while index < len(value):
        while index < len(value) and value[index] in " \t\n\v\f\r":
            index += 1
        if index >= len(value):
            return False
        number_start = index
        if value[index] in {"+", "-"}:
            index += 1
        digits_start = index
        while index < len(value) and value[index].isascii() and value[index].isdigit():
            index += 1
        if index == digits_start:
            return False
        number = int(value[number_start:index])
        if number < 0:
            return False
        multiplier = 1
        if index < len(value):
            suffix = value[index].lower()
            while suffix_index < len(suffixes) and suffixes[suffix_index] != suffix:
                suffix_index += 1
            if suffix_index == len(suffixes):
                return False
            multiplier = (86_400, 3_600, 60, 1)[suffix_index]
            suffix_index += 1
            index += 1
        else:
            if suffix_index >= len(suffixes):
                return False
            suffix_index = len(suffixes)
        if number > (2_147_483_647 - total) // multiplier:
            return False
        total += number * multiplier
    return True


def _is_valid_sudo_option_value(option: str, value: str) -> bool:
    if option in {"C", "--close-from"}:
        return _is_valid_sudo_close_from(value)
    if option in {"T", "--command-timeout"}:
        return _is_valid_sudo_timeout(value)
    if option in {"h", "--host"}:
        return bool(
            value
            and not value.startswith("-")
            and not _is_sudo_environment_assignment(value)
        )
    return True


def _is_sudo_environment_assignment(token: str) -> bool:
    return bool(token and token[0] not in {"/", "="} and "=" in token)


def _skip_sudo_options(tokens: list[str]) -> list[str]:
    """Return sudo's child only when its option grammar is unambiguous and valid."""

    short_value_options = frozenset(
        {"a", "C", "c", "D", "g", "h", "p", "R", "r", "t", "T", "u"}
    )
    short_value_identities = {
        "a": "auth-type",
        "C": "close-from",
        "c": "login-class",
        "D": "chdir",
        "g": "group",
        "h": "host",
        "p": "prompt",
        "R": "chroot",
        "r": "role",
        "t": "type",
        "T": "command-timeout",
        "u": "user",
    }
    short_flags = frozenset({"A", "B", "b", "E", "H", "i", "k", "N", "n", "P", "S", "s"})
    short_no_child = frozenset({"e", "K", "l", "U", "v", "V"})
    long_values = frozenset(
        {
            "--auth-type",
            "--chdir",
            "--chroot",
            "--close-from",
            "--command-timeout",
            "--group",
            "--host",
            "--login-class",
            "--prompt",
            "--role",
            "--type",
            "--user",
        }
    )
    long_no_child = frozenset(
        {
            "--edit",
            "--help",
            "--list",
            "--other-user",
            "--remove-timestamp",
            "--validate",
            "--version",
        }
    )
    long_flags = frozenset(
        {
            "--askpass",
            "--background",
            "--bell",
            "--login",
            "--no-update",
            "--non-interactive",
            "--preserve-env",
            "--preserve-groups",
            "--reset-timestamp",
            "--set-home",
            "--shell",
            "--stdin",
        }
    )
    supported_long = long_values | long_no_child | long_flags
    empty_values = frozenset({"--prompt"})
    short_flag_identities = {
        "A": "askpass",
        "E": "preserve-all",
        "i": "login",
        "k": "reset-timestamp",
        "N": "no-update",
        "S": "stdin",
        "s": "shell",
    }
    long_flag_identities = {
        "--askpass": "askpass",
        "--login": "login",
        "--no-update": "no-update",
        "--reset-timestamp": "reset-timestamp",
        "--shell": "shell",
        "--stdin": "stdin",
    }
    conflicts = (
        frozenset({"askpass", "stdin"}),
        frozenset({"login", "preserve-all"}),
        frozenset({"login", "shell"}),
        frozenset({"no-update", "reset-timestamp"}),
    )
    seen_values: set[str] = set()
    seen_flags: set[str] = set()
    index = 0

    def record_flag(identity: str | None) -> bool:
        if identity is None:
            return True
        seen_flags.add(identity)
        return not any(conflict <= seen_flags for conflict in conflicts)

    while index < len(tokens):
        token = tokens[index]
        if _is_unquoted_shell_separator(token):
            return []
        redirection = _simple_shell_redirection(tokens, index)
        if redirection is not None:
            consumed, _supplies_stdin = redirection
            if not consumed:
                return []
            index += consumed
            continue
        if token == "--":
            operand = _shell_operand_after_redirections(tokens, index + 1)
            return tokens[operand[0] :] if operand is not None else []
        if not token.startswith("-") or token == "-":
            if _is_sudo_environment_assignment(token):
                index += 1
                continue
            break
        index += 1
        if token.startswith("--"):
            option_name = token.split("=", 1)[0]
            if option_name in supported_long:
                normalized = option_name
            else:
                matches = tuple(
                    option for option in supported_long if option.startswith(option_name)
                )
                if len(matches) != 1:
                    return []
                normalized = matches[0]
            if normalized in long_no_child:
                return []
            if normalized in long_values:
                identity = normalized.removeprefix("--")
                if identity in seen_values:
                    return []
                seen_values.add(identity)
                if "=" in token:
                    value = token.partition("=")[2]
                    if not value and normalized not in empty_values:
                        return []
                else:
                    operand = _shell_operand_after_redirections(tokens, index)
                    if operand is None:
                        return []
                    index = operand[0] + 1
                    value = tokens[operand[0]]
                    if not value and normalized not in empty_values:
                        return []
                if not _is_valid_sudo_option_value(normalized, value):
                    return []
                continue
            if normalized == "--preserve-env" and "=" in token:
                if "=" in token.partition("=")[2]:
                    return []
                continue
            if "=" in token or not record_flag(long_flag_identities.get(normalized)):
                return []
            if normalized == "--preserve-env" and not record_flag("preserve-all"):
                return []
            continue
        for option_index, option in enumerate(token[1:]):
            if option in short_no_child:
                return []
            if option in short_flags:
                if not record_flag(short_flag_identities.get(option)):
                    return []
                continue
            if option not in short_value_options:
                return []
            identity = short_value_identities[option]
            if identity in seen_values:
                return []
            seen_values.add(identity)
            if option_index < len(token) - 2:
                value = token[option_index + 2 :]
            else:
                operand = _shell_operand_after_redirections(tokens, index)
                if operand is None:
                    return []
                index = operand[0] + 1
                value = tokens[operand[0]]
                if not value and option != "p":
                    return []
            if not _is_valid_sudo_option_value(option, value):
                return []
            break
    return tokens[index:]


def _safe_env_split_tokens(value: str) -> list[str]:
    """Parse env split-string argv without treating its contents as outer shell syntax."""

    split_tokens = _shell_tokens(value)
    if not split_tokens:
        return []
    for index, token in enumerate(split_tokens):
        if _is_unquoted_shell_separator(token):
            return []
        if _simple_shell_redirection(split_tokens, index) is not None:
            return []
    return split_tokens


def _skip_env_options(tokens: list[str]) -> list[str]:
    value_options = {
        "-C",
        "-a",
        "-f",
        "-u",
        "--argv0",
        "--chdir",
        "--file",
        "--unset",
    }
    flag_options = {
        "-0",
        "-i",
        "-v",
        "--block-signal",
        "--debug",
        "--default-signal",
        "--ignore-environment",
        "--ignore-signal",
        "--list-signal-handling",
        "--null",
    }
    no_child_options = {"-h", "-V", "--help", "--version"}
    short_value_options = {"C", "a", "f", "u"}
    short_flags = {"0", "i", "v"}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _is_unquoted_shell_separator(token):
            return []
        redirection = _simple_shell_redirection(tokens, index)
        if redirection is not None:
            consumed, _supplies_stdin = redirection
            if not consumed:
                return []
            index += consumed
            continue
        if token == "-":
            index += 1
            continue
        if token == "--":
            return tokens[index + 1 :]
        if token in no_child_options:
            return []
        if token.startswith("--split-string="):
            value = token.partition("=")[2]
            if not value:
                return tokens[index + 1 :]
            split_tokens = _safe_env_split_tokens(value)
            return split_tokens + tokens[index + 1 :] if split_tokens else []
        if token == "--split-string" or token == "-S":
            operand = _shell_operand_after_redirections(tokens, index + 1)
            if operand is None:
                return []
            value_index, _supplies_stdin = operand
            value = tokens[value_index]
            split_tokens = _safe_env_split_tokens(value)
            return split_tokens + tokens[value_index + 1 :] if split_tokens else []
        if token.startswith("-S") and not token.startswith("--"):
            value = token[2:]
            split_tokens = _safe_env_split_tokens(value)
            return split_tokens + tokens[index + 1 :] if split_tokens else []
        if token.startswith("--"):
            option_name, separator, attached = token.partition("=")
            if option_name in value_options:
                if separator:
                    if not attached and option_name != "--argv0":
                        return []
                    index += 1
                    continue
                operand = _shell_operand_after_redirections(tokens, index + 1)
                if operand is None:
                    return []
                value_index = operand[0]
                if not tokens[value_index] and option_name != "--argv0":
                    return []
                index = value_index + 1
                continue
            if option_name in flag_options:
                index += 1
                continue
            return []
        if token.startswith("-"):
            options = token[1:]
            option_index = 0
            while option_index < len(options):
                option = options[option_index]
                if option in short_flags:
                    option_index += 1
                    continue
                if option not in short_value_options:
                    return []
                if option_index + 1 < len(options):
                    option_index = len(options)
                    continue
                operand = _shell_operand_after_redirections(tokens, index + 1)
                if operand is None:
                    return []
                value_index = operand[0]
                if not tokens[value_index] and option != "a":
                    return []
                index = value_index
                option_index = len(options)
            index += 1
            continue
        break
    return tokens[index:]


def _skip_wrapper_options(tokens: list[str], *, value_options: set[str]) -> list[str]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _is_unquoted_shell_separator(token):
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


def _strip_env_assignment_tokens(tokens: list[str]) -> list[str]:
    """Strip argv assignments accepted by env, whose names need not be shell identifiers."""

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _is_unquoted_shell_separator(token) or "=" not in token:
            break
        index += 1
    return tokens[index:]


def _strip_assignment_tokens(
    tokens: list[str],
    *,
    allow_quoted: bool = False,
) -> list[str]:
    index = 0
    while (
        index < len(tokens)
        and (
            allow_quoted
            or not getattr(tokens[index], "assignment_prefix_was_quoted", False)
        )
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[index])
    ):
        index += 1
    return tokens[index:]


def _command_token_segments(command: str) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in _shell_tokens(command):
        if token in {"&&", "||", ";", "&"} and not getattr(token, "was_quoted", False):
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
        if _is_unquoted_shell_separator(token):
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


def _git_subcommand_tokens(tokens: list[str]) -> list[str]:
    if not tokens or Path(tokens[0]).name != "git":
        return []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if _is_unquoted_shell_separator(token):
            return []
        if token == "--":
            return tokens[index + 1 :]
        if not token.startswith("-"):
            return tokens[index:]
        if token.startswith("--exec-path="):
            index += 1
            continue
        if token in GIT_GLOBAL_OPTIONS_WITHOUT_VALUES:
            index += 1
            continue
        if any(token.startswith(option) and token != option for option in {"-C", "-c"}):
            index += 1
            continue
        option_name = token.split("=", 1)[0]
        if option_name not in GIT_GLOBAL_OPTIONS_WITH_VALUES:
            return []
        if "=" in token:
            index += 1
            continue
        if index + 1 >= len(tokens) or tokens[index + 1] in SHELL_SEPARATORS:
            return []
        index += 2
    return []


def _skip_option_tokens(
    tokens: list[str],
    *,
    valueless_options: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _is_unquoted_shell_separator(token):
            return []
        if not token.startswith("-"):
            break
        index += 1
        option_name = token.split("=", 1)[0]
        if (
            option_name in OPTIONS_WITH_VALUES
            and option_name not in valueless_options
            and "=" not in token
            and index < len(tokens)
        ):
            index += 1
    return tokens[index:]


def _split_shell_punctuation_token(token: str) -> list[str]:
    if token == "{}":
        return [token]
    separators: list[str] = []
    index = 0
    while index < len(token):
        pair = token[index : index + 2]
        if pair in {"||", "&&", "|&"}:
            separators.append(pair)
            index += 2
            continue
        separators.append(token[index])
        index += 1
    return separators


def _is_unquoted_shell_separator(token: str) -> bool:
    return token in SHELL_SEPARATORS and not getattr(token, "was_quoted", False)


def _merge_shell_redirection_tokens(
    tokens: list[str],
    *,
    numeric_boundary_marker: str,
) -> list[str]:
    """Rejoin fd prefixes and duplication split by punctuation-aware shlex."""

    merged: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        clean_token = token.replace(numeric_boundary_marker, "")
        numeric_was_separated = numeric_boundary_marker in token
        if (
            clean_token.isdigit()
            and not numeric_was_separated
            and index + 1 < len(tokens)
            and tokens[index + 1] in {">", ">>", ">|", "<", "<>"}
        ):
            merged.append(clean_token + tokens[index + 1])
            index += 2
            continue
        if (
            clean_token.isdigit()
            and not numeric_was_separated
            and index + 2 < len(tokens)
            and tokens[index + 1] in {">&", "<&"}
            and not _is_unquoted_shell_separator(tokens[index + 2])
        ):
            merged.append(clean_token + tokens[index + 1] + tokens[index + 2])
            index += 3
            continue
        if (
            token in {">&", "<&"}
            and index + 1 < len(tokens)
            and not _is_unquoted_shell_separator(tokens[index + 1])
        ):
            target = tokens[index + 1]
            if token == ">&" and not re.fullmatch(r"(?:\d+|-)", target):
                merged.append("&>" + target)
            else:
                merged.append(token + target)
            index += 2
            continue
        merged.append(token)
        index += 1
    return merged


def _mark_quoted_shell_tokens(command: str) -> tuple[str, str, str]:
    """Mark quoted, escaped, and separated numeric shell syntax."""

    quote_marker = "\0"
    numeric_boundary_marker = "\x01"
    marked: list[str] = []
    quote: str | None = None
    escaped = False
    for index, character in enumerate(command):
        if escaped:
            if quote is None and character not in "\r\n":
                slash = marked.pop()
                marked.extend((quote_marker, slash, character, quote_marker))
            else:
                marked.append(character)
            escaped = False
            continue
        if character == "\\" and quote != "'":
            marked.append(character)
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                marked.extend((quote_marker, character))
                quote = None
            else:
                marked.append(character)
            continue
        if character in {"'", '"'}:
            marked.extend((character, quote_marker))
            quote = character
            continue
        if character.isspace():
            prefix = command[:index]
            suffix = command[index:].lstrip()
            if (
                re.search(r"(?:^|[\s;&|(){}<>])\d+$", prefix)
                and suffix.startswith(("<", ">"))
            ):
                marked.append(numeric_boundary_marker)
        marked.append(character)
    return "".join(marked), quote_marker, numeric_boundary_marker


def _shell_tokens(command: str) -> list[str]:
    try:
        marked_command, quote_marker, numeric_boundary_marker = _mark_quoted_shell_tokens(command)
        lexer = shlex.shlex(marked_command, posix=True, punctuation_chars="();&|{}<>")
        lexer.whitespace_split = True
        tokens: list[str] = []
        for token in lexer:
            if token and all(char in "();&|{}" for char in token):
                tokens.extend(_split_shell_punctuation_token(token))
            else:
                tokens.append(token)
        annotated: list[str] = []
        for token in _merge_shell_redirection_tokens(
            tokens,
            numeric_boundary_marker=numeric_boundary_marker,
        ):
            operator_positions = [
                position for position in (token.find("<"), token.find(">")) if position >= 0
            ]
            operator_position = min(operator_positions, default=-1)
            assignment_position = token.find("=")
            quote_positions = [
                position
                for position, character in enumerate(token)
                if character == quote_marker
            ]
            annotated.append(
                _ShellToken(
                    token.replace(quote_marker, "").replace(numeric_boundary_marker, ""),
                    redirection_operator_was_quoted=bool(
                        operator_position >= 0
                        and sum(position <= operator_position for position in quote_positions) % 2
                    ),
                    assignment_prefix_was_quoted=bool(
                        assignment_position >= 0
                        and any(position <= assignment_position for position in quote_positions)
                    ),
                    numeric_redirection_was_separated=numeric_boundary_marker in token,
                    was_quoted=bool(quote_positions),
                )
            )
        return annotated
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
