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
    if tool_name and not _is_compactable_tool_name(tool_name):
        return _unchanged(text, "protected_tool", command_class, reason="protected_tool")
    if command_class == "git_diff" and options.preserve_diffs:
        return _unchanged(text, "protected_diff", command_class, reason="diff_passthrough")
    if command_class == "patch":
        return _unchanged(text, "protected_patch", command_class, reason="patch_passthrough")
    if command_class in {"file_read", "source_mixed"}:
        return _unchanged(
            text,
            "protected_file_read",
            command_class,
            reason="file_read_passthrough",
        )
    if _has_bypass_marker(text) or _has_bypass_marker(command or ""):
        return _unchanged(text, "bypass", command_class, reason="bypass_marker")
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
    command_s = (command or "").strip()
    command_l = command_s.lower()
    sample_l = text[:4_000].lower()

    if not _has_unquoted_shell_operator(
        command_s,
        allow_input_redirection=True,
        allow_stderr_redirection=True,
    ):
        shell_payload = _shell_command_payload(command_s)
        if shell_payload and shell_payload != command_s:
            shell_payload_class = classify_command(shell_payload, text)
            if shell_payload_class != "generic":
                return shell_payload_class
        env_s_payload = _env_split_string_payload(command_s)
        if env_s_payload and env_s_payload != command_s:
            env_s_payload_class = classify_command(env_s_payload, text)
            if env_s_payload_class != "generic":
                return env_s_payload_class
    if _looks_like_mixed_source_command(command_s):
        return "source_mixed"
    if _looks_like_file_read_command(command_s):
        return "file_read"
    if _looks_like_v4a_patch(text) and not (
        _looks_like_pytest_command(command_l)
        or _contains_pytest_invocation(command_l)
        or _looks_like_unittest_command(command_l)
        or _contains_unittest_invocation(command_l)
    ):
        return "patch"
    if _looks_like_diff_command(command_l):
        return "git_diff"
    if "diff --git " in text or _looks_like_unified_diff(text):
        return "git_diff"
    if _is_os_package_command(command_l) and _looks_like_package_failure_output(sample_l):
        return "os_package"
    if _is_dependency_install_command(command_l) and _looks_like_package_failure_output(sample_l):
        return "dependency_install"
    if _looks_like_pytest_command(command_l) or _contains_pytest_invocation(command_l):
        return "pytest"
    if _looks_like_unittest_command(command_l) or _contains_unittest_invocation(command_l):
        return "unittest"
    if _is_node_runtime_or_test_command(command_l):
        if _looks_like_pytest_output(sample_l):
            return "pytest"
        if _looks_like_unittest_output(sample_l):
            return "unittest"
        return "node"
    if _is_os_package_command(command_l):
        return "os_package"
    if _is_dependency_install_command(command_l):
        return "dependency_install"
    if "docker build" in command_l or "docker compose" in command_l or "dockerfile" in sample_l:
        return "docker_build"
    if _is_inventory_command(command_l):
        return "inventory"
    if _is_search_command(command_l):
        return "search"
    if "git status" in command_l or ("on branch " in sample_l and "working tree" in sample_l):
        return "git_status"
    if "git log" in command_l:
        return "git_log"
    if _looks_like_pytest_output(sample_l):
        return "pytest"
    if _looks_like_unittest_output(sample_l):
        return "unittest"
    if _is_node_command(command_l, sample_l):
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
        return (
            CRITICAL_PATTERNS
            + OS_PACKAGE_PATTERNS
            + DEPENDENCY_INSTALL_PATTERNS
            + DOCKER_PATTERNS
            + lcm_patterns
        )
    if command_class == "node":
        return NODE_PRESERVATION_PATTERNS + lcm_patterns
    if command_class == "os_package":
        return OS_PACKAGE_PATTERNS + CRITICAL_PATTERNS + lcm_patterns
    if command_class == "dependency_install":
        return DEPENDENCY_INSTALL_PATTERNS + CRITICAL_PATTERNS + lcm_patterns
    if command_class == "inventory":
        return INVENTORY_PATTERNS + CRITICAL_PATTERNS + lcm_patterns
    if command_class == "docker_build":
        return DOCKER_PATTERNS + CRITICAL_PATTERNS + lcm_patterns
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
        return command_class, _important_lines(
            text,
            options,
            TEST_PATTERNS
            + OS_PACKAGE_PATTERNS
            + DEPENDENCY_INSTALL_PATTERNS
            + DOCKER_PATTERNS
            + lcm_patterns,
        )
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
        return "inventory", _important_lines(text, options, INVENTORY_PATTERNS + lcm_patterns)
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
        r"assertionerror|traceback|exceptiongroup|baseexception|\bexception\b\s*:",
        r"\b(?:type|reference|syntax|range|value|key|attribute|name|runtime|import|module)error\b",
        r"\bunhandled\s+exception\b",
        r"\bexception\s+in\b",
        r"^\s*E\s+|^\s*E:",
        r"\b[A-Za-z_]*(?:Type|Reference|Syntax|Range|URI|Eval)?Error\b(?::|$)",
        r"unable to locate package|no matching distribution|no match for argument",
        r"no packages? available|package .* not found|target not found",
        r"resolutionimpossible|no solution found|eresolve",
        r"permission denied|cannot access|no such file|not found",
        r"\d+\s+failed",
        r"^(failed|error)\s+",
        r"\bFAILED\b|\bERROR\b",
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

NODE_PRESERVATION_PATTERNS = (*NODE_PATTERNS, *CRITICAL_PATTERNS)

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


APT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^err:\d+|^e:|^w:.*gpg error",
        r"unable to locate package|could not get lock|gpg error|hash sum mismatch|failed to fetch",
        r"does not have a release file|repository .* not signed|no_pubkey|404\s+not found",
        r"reading package lists\.\.\. done|fetched .* in .*|\d+ upgraded, .* newly installed",
        r"setting up \S+|processing triggers for",
    )
)

SYSTEM_PACKAGE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^error:|^err:|^warning:|^fatal:",
        r"no such package|unable to locate|package .* not found|target not found",
        (
            r"no packages? available|no packages? available to install matching"
            r"|packages? .* not available"
        ),
        r"no match for argument|unable to find a match|no provider .* found",
        r"failed to synchronize|failed to fetch|failed retrieving|failed to solve",
        r"conflicting requests|broken dependencies|nothing provides|dependency problem",
        r"no available formula|no formula|cannot find package|could not resolve",
        r"\berror\b|\bfailed\b|\bfail\b|not found",
    )
)

PYTHON_PACKAGE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^error:|^error\b|resolutionimpossible|no solution found|resolution failed",
        (
            r"dependency conflict|conflicting dependencies|failed to prepare distributions"
            r"|could not find a version"
        ),
        r"no matching distribution|failed to build|because .* depends on|caused by:",
        r"successfully installed|resolved \d+ packages|installed \S+",
    )
)

NODE_INSTALL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"npm err!|pnpm err!|yarn.*error|yn\d{4}: error|err_pnpm|elifecycle|eresolve",
        r"\berror\b|\bfailed\b|\bfail\b|failed with errors",
        r"\bwarning\b|\bwarn\b",
        r"could not resolve|unable to resolve|peer dep|lockfile|e404|enotfound",
        r"added \d+ packages|found \d+ vulnerabilities|audited \d+ packages",
    )
)

DEPENDENCY_INSTALL_PATTERNS = PYTHON_PACKAGE_PATTERNS + NODE_INSTALL_PATTERNS

OS_PACKAGE_PATTERNS = APT_PATTERNS + SYSTEM_PACKAGE_PATTERNS + DEPENDENCY_INSTALL_PATTERNS

INVENTORY_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"permission denied|no such file|cannot access|not found",
        r"^(find|ls|fd|tree):",
    )
)

PYTHON_OPTIONS_WITH_VALUE = frozenset({"-W", "-X", "--check-hash-based-pycs"})

UV_RUN_OPTIONS_WITH_VALUE = frozenset(
    {
        "--active",
        "--config-file",
        "--default-index",
        "--directory",
        "--env-file",
        "--extra",
        "--find-links",
        "--from",
        "--group",
        "--index",
        "--index-url",
        "--keyring-provider",
        "--link-mode",
        "--project",
        "--python",
        "--resolution",
        "--with",
        "--with-editable",
        "--with-requirements",
        "-p",
    }
)

UV_GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {
        "--cache-dir",
        "--config-file",
        "--directory",
        "--index",
        "--index-url",
        "--keyring-provider",
        "--link-mode",
        "--project",
        "--python",
        "--resolution",
        "--with",
        "--with-editable",
        "--with-requirements",
        "-p",
    }
)

PACKAGE_RUNNER_OPTIONS_WITH_VALUE = frozenset(
    {
        "--cache",
        "--call",
        "--cwd",
        "--dir",
        "--from",
        "--package",
        "--prefix",
        "--registry",
        "--workspace",
        "-c",
        "-p",
        "-w",
    }
)

SUDO_OPTIONS_WITH_VALUE = frozenset(
    {
        "-u",
        "--user",
        "-g",
        "--group",
        "-c",
        "--close-from",
        "-p",
        "--prompt",
        "-d",
        "-D",
        "--chdir",
        "-r",
        "-R",
        "--chroot",
        "-t",
        "-T",
        "--command-timeout",
        "-h",
        "--host",
    }
)

ENV_OPTIONS_WITH_VALUE = frozenset({"-u", "--unset", "-c", "-C", "--chdir", "-s", "-S"})

TIMEOUT_OPTIONS_WITH_VALUE = frozenset({"-k", "--kill-after", "-s", "--signal"})

NPM_FAMILY_INSTALL_SUBCOMMANDS = frozenset({"install", "i", "ci", "add", "update", "up"})

NPM_FAMILY_OPTIONS_WITH_VALUE = frozenset(
    {
        "--cache",
        "--cwd",
        "--dir",
        "--filter",
        "--prefix",
        "--loglevel",
        "--omit",
        "--registry",
        "--scope",
        "--store-dir",
        "--workspace",
        "-c",
        "-f",
        "-w",
    }
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
    if _is_diagnostic_detail_line(line):
        return 0
    if re.search(
        r"unable to locate package|no matching distribution|no match for argument"
        r"|no packages? available|package .* not found|target not found"
        r"|resolutionimpossible|no solution found|eresolve",
        line,
        re.IGNORECASE,
    ):
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
            if (
                (start_line > 0 or end_line + 1 < len(lines))
                and "[noisegate: omitted" not in candidate
            ):
                return None
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


def _looks_like_pytest_command(command: str) -> bool:
    tokens = _shell_split(command)
    index = _skip_command_prefix_tokens(tokens)
    return _tokens_start_pytest(tokens[index:])

def _looks_like_unittest_command(command: str) -> bool:
    tokens = _shell_split(command)
    index = _skip_command_prefix_tokens(tokens)
    return _tokens_start_unittest(tokens[index:])

def _contains_pytest_invocation(command: str) -> bool:
    return any(_looks_like_pytest_command(segment) for segment in _shell_command_segments(command))

def _contains_unittest_invocation(command: str) -> bool:
    return any(
        _looks_like_unittest_command(segment) for segment in _shell_command_segments(command)
    )

def _looks_like_pytest_output(sample: str) -> bool:
    return bool(
        "=== failures ===" in sample
        or "short test summary info" in sample
        or re.search(r"(^|\n)failed\s+tests?[/\\]", sample)
        or re.search(r"(^|\n)e\s+assertionerror\b", sample)
    )

def _looks_like_unittest_output(sample: str) -> bool:
    return bool(re.search(r"ran \d+ tests?", sample) or re.search(r"(^|\n)fail: test_", sample))


def _looks_like_package_failure_output(sample: str) -> bool:
    return bool(
        re.search(r"(^|\n)\s*(e:|err:\d+|error:|fatal:)", sample)
        or re.search(
            r"unable to locate package|no matching distribution|no match for argument",
            sample,
        )
        or re.search(r"no packages? available|packages? .* not available", sample)
        or re.search(r"package .* not found|target not found|no such package", sample)
        or re.search(r"resolutionimpossible|no solution found|eresolve", sample)
        or re.search(r"could not find a version|failed to build|dependency conflict", sample)
        or re.search(r"failed to fetch|gpg error|could not get lock|no_pubkey", sample)
        or re.search(r"no provider .* found|nothing provides|broken dependencies", sample)
        or re.search(r"no available formula|could not resolve|peer dep|lockfile", sample)
        or re.search(r"e404|enotfound|hash sum mismatch|repository .* not signed", sample)
    )


def _shell_split(command: str) -> list[str]:
    if not command:
        return []
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()

def _shell_command_segments(command: str) -> list[str]:
    return [
        segment.strip()
        for segment in re.split(r"&&|\|\||[|;]|\r?\n", command)
        if segment.strip()
    ]

def _skip_command_prefix_tokens(tokens: list[str]) -> int:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        executable = token.rsplit("/", 1)[-1]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            index += 1
            continue
        if executable in {"env", "command", "sudo"}:
            if executable == "env":
                index = _skip_env_tokens(tokens, index + 1)
            elif executable == "sudo":
                index = _skip_sudo_tokens(tokens, index + 1)
            else:
                index += 1
            continue
        if executable == "timeout" and index + 1 < len(tokens):
            index = _skip_timeout_tokens(tokens, index + 1)
            continue
        break
    return index

def _option_key_for_value_skip(option: str) -> str:
    key = option.split("=", 1)[0]
    return key.lower() if key.startswith("--") else key

def _skip_env_tokens(tokens: list[str], index: int) -> int:
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        index += 1
        if "=" not in option and _option_key_for_value_skip(option) in ENV_OPTIONS_WITH_VALUE:
            index += 1
    return index

def _skip_sudo_tokens(tokens: list[str], index: int) -> int:
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        index += 1
        if "=" not in option and _option_key_for_value_skip(option) in SUDO_OPTIONS_WITH_VALUE:
            index += 1
    return index

def _skip_timeout_tokens(tokens: list[str], index: int) -> int:
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        index += 1
        if "=" not in option and _option_key_for_value_skip(option) in TIMEOUT_OPTIONS_WITH_VALUE:
            index += 1
    if index < len(tokens):
        index += 1
    return index

def _tokens_start_pytest(tokens: list[str]) -> bool:
    if not tokens:
        return False
    executable = tokens[0].rsplit("/", 1)[-1]
    if executable in {"pytest", "py.test"}:
        return True
    if executable in {"python", "python3"}:
        return _python_runs_module(tokens, "pytest")
    if executable in {"npx", "pnpx", "uvx"}:
        return _tokens_start_pytest(_skip_package_runner_options(tokens[1:]))
    if executable in {"npm-exec", "pnpm-dlx"}:
        return _tokens_start_pytest(_skip_package_runner_options(tokens[1:]))
    if executable in {"npm", "pnpm", "yarn"}:
        return _tokens_start_package_exec_pytest(tokens[1:])
    if executable == "uv":
        run_index = _uv_run_index(tokens[1:])
        if run_index is not None:
            return _tokens_start_pytest(_uv_run_command_tokens(tokens[run_index + 2 :]))
    return False

def _tokens_start_unittest(tokens: list[str]) -> bool:
    if not tokens:
        return False
    executable = tokens[0].rsplit("/", 1)[-1]
    if executable in {"python", "python3"}:
        return _python_runs_module(tokens, "unittest")
    if executable == "uv":
        run_index = _uv_run_index(tokens[1:])
        if run_index is not None:
            return _tokens_start_unittest(_uv_run_command_tokens(tokens[run_index + 2 :]))
    return False

def _python_runs_module(tokens: list[str], module: str) -> bool:
    index = 1
    while index < len(tokens):
        arg = tokens[index]
        if arg == "-m":
            return index + 1 < len(tokens) and tokens[index + 1] == module
        if arg == "--" or not arg.startswith("-"):
            return False
        option = arg.split("=", 1)[0]
        index += 1
        if "=" not in arg and option in PYTHON_OPTIONS_WITH_VALUE:
            index += 1
    return False

def _uv_run_command_tokens(args: list[str]) -> list[str]:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return args[index + 1 :]
        if arg == "-m":
            return ["python", "-m", *args[index + 1 :]]
        if not arg.startswith("-"):
            return args[index:]
        option = arg.split("=", 1)[0].lower()
        index += 1
        if "=" not in arg and option in UV_RUN_OPTIONS_WITH_VALUE:
            index += 1
    return []

def _uv_run_index(args: list[str]) -> int | None:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "run":
            return index
        if arg == "--":
            index += 1
            continue
        if arg.startswith("--"):
            option = arg.split("=", 1)[0].lower()
            index += 1
            if "=" not in arg and option in UV_GLOBAL_OPTIONS_WITH_VALUE:
                index += 1
        elif arg.startswith("-"):
            option = arg.split("=", 1)[0].lower()
            index += 1
            if option in UV_GLOBAL_OPTIONS_WITH_VALUE:
                index += 1
        else:
            return None
    return None

def _skip_package_runner_options(args: list[str]) -> list[str]:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return args[index + 1 :]
        if not arg.startswith("-"):
            return args[index:]
        option = arg.split("=", 1)[0].lower()
        index += 1
        if "=" not in arg and option in PACKAGE_RUNNER_OPTIONS_WITH_VALUE:
            index += 1
    return []

def _tokens_start_package_exec_pytest(args: list[str]) -> bool:
    args = _skip_package_runner_options(args)
    if not args:
        return False
    subcommand = args[0]
    if subcommand in {"exec", "x", "dlx"}:
        return _tokens_start_pytest(_skip_package_runner_options(args[1:]))
    return False

def _command_prefix_pattern() -> str:
    return (
        r"(^|[\s;&|])"
        r"(?:(?:env\s+)?[A-Za-z_][A-Za-z0-9_]*=\S+\s+|env\s+|command\s+|timeout\s+\S+\s+|sudo\s+)*"
    )

def _looks_like_mixed_source_command(command: str) -> bool:
    if not command:
        return False
    source_reader_pattern = r"(?:\S*/)?(cat|sed|head|tail|nl|bat|jq|yq)\b"
    if re.search(r"(^|[\s;&|])python(?:3)?\s+-\s*<<", command):
        return True
    if re.search(
        rf"(^|[\s;&|])(?:\S*/)?find\b.*\s-exec(?:dir)?\s+{source_reader_pattern}",
        command,
    ):
        return True
    if _find_exec_invokes_source_reader(command):
        return True
    if re.search(rf"\|\s*xargs\b.*\b{source_reader_pattern}", command):
        return True
    if re.search(r"(^|[\s;&|])(?:\S*/)?fd\b", command) and re.search(
        r"\s(-x|-X|--exec(?:-batch)?)(\s|=)",
        command,
    ):
        return bool(re.search(source_reader_pattern, command))
    if re.search(r"[|;&\n\r]", command) and _contains_safe_source_read_invocation(command):
        return True
    return bool(re.search(r"[|;&\n\r]", command) and _contains_source_search_invocation(command))

def _contains_source_search_invocation(command: str) -> bool:
    if not command:
        return False
    boundary = r"(^|[\s;&|\r\n])"
    if re.search(boundary + r"(?:\S*/)?(rg|grep|ag|ack)\s+--files\b", command):
        return False
    return bool(re.search(boundary + r"(?:\S*/)?(rg|grep|ag|ack)\b", command))

def _contains_source_read_invocation(command: str) -> bool:
    if not command:
        return False
    try:
        shlex.split(command)
    except ValueError:
        return False
    if _find_exec_invokes_source_reader(command):
        return True
    env_s_payload = _env_split_string_payload(command)
    if env_s_payload is not None and _contains_source_read_invocation(env_s_payload):
        return True
    shell_payload = _shell_command_payload(command)
    if shell_payload is not None and _contains_source_read_invocation(shell_payload):
        return True
    for segment in _shell_command_segments(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        if _tokens_start_source_read(tokens):
            return True
    # Shell wrappers and environment assignments do not change the intent: these still
    # display source/content and should stay exact by default.
    prefix = (
        r"(^|[;&|\r\n]\s*)"
        r"(?:(?:env\s+)?[A-Za-z_][A-Za-z0-9_]*=\S+\s+|env\s+|command\s+|timeout\s+\S+\s+|sudo\s+)*"
    )
    path_prefix = r"(?:\S*/)?"
    if bool(re.search(prefix + path_prefix + r"(cat|head|tail|less|more|bat)\b", command)):
        return True
    if bool(re.search(prefix + path_prefix + r"nl\b(?:\s+-[a-z0-9]+)*(?:\s+\S+|\s*<)", command)):
        return True
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if _tokens_start_source_read(tokens):
        return True
    return bool(re.search(prefix + path_prefix + r"(jq|yq)\b", command))


def _contains_safe_source_read_invocation(command: str) -> bool:
    if not command:
        return False
    if not _has_unquoted_shell_operator(
        command,
        allow_input_redirection=True,
        allow_stderr_redirection=True,
    ):
        env_s_payload = _env_split_string_payload(command)
        if env_s_payload is not None:
            return _contains_safe_source_read_invocation(env_s_payload)
        shell_payload = _shell_command_payload(command)
        if shell_payload is not None:
            return _contains_safe_source_read_invocation(shell_payload)
    for segment in _shell_command_segments(command):
        if _has_unquoted_shell_operator(
            segment,
            allow_input_redirection=True,
            allow_stderr_redirection=True,
        ):
            continue
        env_s_payload = _env_split_string_payload(segment)
        if env_s_payload is not None and _contains_safe_source_read_invocation(env_s_payload):
            return True
        shell_payload = _shell_command_payload(segment)
        if shell_payload is not None and _contains_safe_source_read_invocation(shell_payload):
            return True
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        if _tokens_start_source_read(tokens):
            return True
    return False


def _find_exec_invokes_source_reader(command: str) -> bool:
    tokens = _shell_split(command)
    for index, token in enumerate(tokens):
        if token not in {"-exec", "-execdir"}:
            continue
        exec_tokens: list[str] = []
        for exec_token in tokens[index + 1 :]:
            if exec_token == ";":
                break
            exec_tokens.append(exec_token)
        if not exec_tokens:
            continue
        if _tokens_start_source_read(exec_tokens):
            return True
        executable = exec_tokens[0].rsplit("/", 1)[-1].lower()
        if executable in {"bash", "sh", "zsh"}:
            payload = _shell_command_payload(shlex.join(exec_tokens))
            if payload is not None and _contains_source_read_invocation(payload):
                return True
    return False


def _tokens_start_source_read(tokens: list[str]) -> bool:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        executable = token.rsplit("/", 1)[-1].lower()
        if token in {"&&", "||", ";", "|"}:
            index += 1
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            index += 1
            continue
        if executable == "env":
            index = _skip_env_tokens(tokens, index + 1)
            continue
        if executable == "command":
            index += 1
            continue
        if executable == "sudo":
            index = _skip_sudo_tokens(tokens, index + 1)
            continue
        if executable == "timeout":
            index = _skip_timeout_tokens(tokens, index + 1)
            continue
        break
    if index >= len(tokens):
        return False
    executable = tokens[index].rsplit("/", 1)[-1].lower()
    args = tokens[index + 1 :]
    if any("$(" in arg or "`" in arg for arg in args):
        return False
    if _looks_like_git_show_file_read_tokens(tokens[index:]):
        return True
    if executable in {"cat", "head", "tail", "less", "more", "bat", "jq", "yq"}:
        return True
    if executable == "nl":
        return bool(args)
    if executable != "sed":
        return False
    saw_quiet = False
    for arg in args:
        if arg in {"--quiet", "--silent"} or (arg.startswith("-") and "n" in arg):
            saw_quiet = True
            continue
        if not arg.startswith("-"):
            return saw_quiet and _looks_like_sed_print_script(arg)
    return False

def _env_split_string_payload(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        executable = token.rsplit("/", 1)[-1].lower()
        if executable == "command" or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            index += 1
            continue
        if executable == "sudo":
            index = _skip_sudo_tokens(tokens, index + 1)
            continue
        if executable == "timeout" and index + 1 < len(tokens):
            index = _skip_timeout_tokens(tokens, index + 1)
            continue
        if executable != "env":
            return None
        break
    if index >= len(tokens):
        return None
    index += 1
    while index < len(tokens):
        arg = tokens[index]
        option = _option_key_for_value_skip(arg)
        if option in {"-s", "-S", "--split-string"} and "=" not in arg:
            if index + 1 >= len(tokens):
                return None
            return " ".join([tokens[index + 1], *tokens[index + 2 :]])
        if option == "--split-string" and "=" in arg:
            return " ".join([arg.split("=", 1)[1], *tokens[index + 1 :]])
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", arg):
            index += 1
            continue
        if not arg.startswith("-"):
            return None
        index += 1
        if "=" not in arg and option in ENV_OPTIONS_WITH_VALUE:
            index += 1
    return None

def _shell_command_payload(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        executable = token.rsplit("/", 1)[-1].lower()
        if executable == "command":
            index += 1
            continue
        if executable == "env":
            index = _skip_env_tokens(tokens, index + 1)
            continue
        if executable == "sudo":
            index = _skip_sudo_tokens(tokens, index + 1)
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            index += 1
            continue
        if executable == "timeout":
            index = _skip_timeout_tokens(tokens, index + 1)
            continue
        break
    if index >= len(tokens):
        return None
    shell_executable = tokens[index].rsplit("/", 1)[-1].lower()
    if shell_executable not in {"bash", "sh", "zsh"}:
        return None
    args = tokens[index + 1 :]
    arg_index = 0
    while arg_index < len(args):
        arg = args[arg_index]
        if not arg.startswith("-"):
            return None
        if (
            (arg.startswith("-") and not arg.startswith("--") and "c" in arg.lstrip("-"))
            or arg == "--command"
        ) and arg_index + 1 < len(args):
            return args[arg_index + 1]
        takes_value = arg in {"--rcfile", "--init-file", "-o", "+o"} or (
            arg.startswith("-") and not arg.startswith("--") and "o" in arg.lstrip("-")
        )
        arg_index += 1
        if takes_value and arg_index < len(args):
            arg_index += 1
    return None

def _is_inventory_command(command: str) -> bool:
    if not command:
        return False
    tokens = _shell_split(command)
    prefix_index = _skip_command_prefix_tokens(tokens)
    if 0 < prefix_index < len(tokens):
        return _is_inventory_command(" ".join(tokens[prefix_index:]))
    if re.search(r"[;&]", command):
        parts = [part.strip() for part in re.split(r"&&|;", command) if part.strip()]
        return bool(
            len(parts) > 1
            and all(_is_inventory_prefix_segment(part) for part in parts[:-1])
            and _is_inventory_command(parts[-1])
        )
    if re.search(r"[|><]", command):
        return False
    if not tokens:
        return False
    executable = tokens[0].rsplit("/", 1)[-1]
    args = tokens[1:]
    if executable == "ls":
        return True
    if executable == "tree":
        return True
    if executable == "fd":
        return not any(
            arg in {"-x", "-X", "--exec", "--exec-batch"}
            or arg.startswith("--exec=")
            or arg.startswith("--exec-batch=")
            for arg in args
        )
    if executable == "rg":
        return bool(args and args[0] == "--files")
    if executable == "git":
        index = 0
        while index < len(args) and args[index] == "-c":
            index += 2
        return index < len(args) and args[index] == "ls-files"
    return bool(executable == "find" and "-exec" not in args and "-execdir" not in args)

def _is_inventory_prefix_segment(command: str) -> bool:
    return bool(
        re.match(r"^(cd|pushd)\b", command)
        or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=\S+$", command)
    )

def _is_os_package_command(command: str) -> bool:
    if not command:
        return False
    opt_before_update_install = (
        r"(?:\s+-{1,2}[\w-]+(?:=\S+)?(?:\s+(?!(?:update|install)\b)\S+)?)"
    )
    opt_before_add = r"(?:\s+-{1,2}[\w-]+(?:=\S+)?(?:\s+(?!add\b)\S+)?)"
    return bool(
        re.search(
            rf"(^|[\s;&|])(?:\S*/)?apt(-get)?(?:{opt_before_update_install})*\s+"
            r"(update|install)\b",
            command,
        )
        or re.search(
            rf"(^|[\s;&|])(?:\S*/)?(dnf|yum)(?:{opt_before_update_install})*\s+install\b",
            command,
        )
        or re.search(rf"(^|[\s;&|])(?:\S*/)?apk(?:{opt_before_add})*\s+add\b", command)
        or re.search(rf"(^|[\s;&|])(?:\S*/)?pkg(?:{opt_before_add})*\s+install\b", command)
        or re.search(r"(^|[\s;&|])(?:\S*/)?pacman(?:\s+-[a-z]*s[a-z]*)\b", command)
        or re.search(
            rf"(^|[\s;&|])(?:\S*/)?zypper(?:{opt_before_update_install})*\s+install\b",
            command,
        )
        or re.search(
            rf"(^|[\s;&|])(?:\S*/)?brew(?:{opt_before_update_install})*\s+"
            r"(install|update|upgrade)\b",
            command,
        )
    )

def _is_dependency_install_command(command: str) -> bool:
    if not command:
        return False
    uv_opt = r"(?:\s+-{1,2}[\w-]+(?:=\S+)?(?:\s+(?!(?:sync|pip|add|lock)\b)\S+)?)"
    pip_opt = r"(?:\s+-{1,2}[\w-]+(?:=\S+)?(?:\s+(?!install\b)\S+)?)"
    return bool(
        re.search(
            rf"(^|[\s;&|])(?:\S*/)?uv(?:{uv_opt})*\s+(sync|pip\s+install|add|lock)\b",
            command,
        )
        or re.search(rf"(^|[\s;&|])(?:\S*/)?pip(?:3)?(?:{pip_opt})*\s+install\b", command)
        or re.search(
            rf"(^|[\s;&|])(?:\S*/)?python(?:3)?\s+-m\s+pip(?:{pip_opt})*\s+install\b",
            command,
        )
        or _is_npm_family_dependency_command(command)
    )

def _is_npm_family_dependency_command(command: str) -> bool:
    for segment in re.split(r"[;&|]", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        for index, token in enumerate(tokens):
            executable = token.rsplit("/", 1)[-1]
            if executable not in {"npm", "pnpm", "yarn"}:
                continue
            subcommand = _first_npm_family_subcommand(tokens[index + 1 :])
            if subcommand is None:
                return executable == "yarn"
            if subcommand in NPM_FAMILY_INSTALL_SUBCOMMANDS:
                return True
    return False

def _first_npm_family_subcommand(args: list[str]) -> str | None:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            continue
        if arg.startswith("--"):
            option = arg.split("=", 1)[0]
            index += 1
            if "=" not in arg and option in NPM_FAMILY_OPTIONS_WITH_VALUE:
                index += 1
            continue
        if arg == "-f":
            # Lower-cased command text means npm's flag-style -f and pnpm's
            # value-taking -F both arrive here. If the next token is an install
            # subcommand, keep -f as valueless; otherwise treat it as filter.
            if index + 1 < len(args) and args[index + 1] in NPM_FAMILY_INSTALL_SUBCOMMANDS:
                index += 1
            else:
                index += 2
            continue
        if arg in NPM_FAMILY_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return arg
    return None

def _is_node_runtime_or_test_command(command: str) -> bool:
    executable = r"(?:\S*/)?"
    if re.search(rf"(^|[\s;&|]){executable}(node|npx|vitest|jest|tsx|mocha)\b", command):
        return True
    test_subcommands = {"build", "run", "start", "test"}
    for segment in re.split(r"[;&|]", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        for index, token in enumerate(tokens):
            if token.rsplit("/", 1)[-1] not in {"npm", "pnpm", "yarn"}:
                continue
            subcommand = _first_npm_family_subcommand(tokens[index + 1 :])
            if subcommand in test_subcommands:
                return True
    return False

def _contains_command(command: str, names: tuple[str, ...]) -> bool:
    tokens = re.split(r"[\s;&|()]+", command)
    return any(name in tokens for name in names)


def _looks_like_diff_command(command: str) -> bool:
    if not command:
        return False
    git_executable = r"(?:\S*/)?git"
    diff_executable = r"(?:\S*/)?diff"
    if bool(re.search(rf"(^|[\s;&|]){git_executable}\b.*\bdiff\b", command)) or bool(
        re.search(rf"(^|[\s;&|]){diff_executable}\s", command)
    ):
        return True
    if re.search(
        rf"(^|[\s;&|]){git_executable}\b.*\blog\b.*?(\s-p\b|\s--patch\b)",
        command,
    ):
        return True
    return bool(re.search(rf"(^|[\s;&|]){git_executable}\b.*\bshow\b", command)) and not bool(
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
    if not command or _has_unquoted_shell_operator(
        command,
        allow_input_redirection=True,
        allow_stderr_redirection=True,
    ):
        return False
    return _contains_source_read_invocation(command)


def _has_unquoted_shell_operator(
    command: str,
    *,
    allow_input_redirection: bool = False,
    allow_stderr_redirection: bool = False,
) -> bool:
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
        if char in "|;&`\n\r" or (char == "$" and command[index + 1 : index + 2] == "("):
            if (
                char == "&"
                and allow_stderr_redirection
                and index >= 2
                and command[index - 1] == ">"
                and command[index - 2].isdigit()
                and command[index - 2] != "1"
                and command[index + 1 : index + 2].isdigit()
            ):
                continue
            return True
        if char == ">" and not (
            allow_stderr_redirection
            and index > 0
            and command[index - 1].isdigit()
            and command[index - 1] != "1"
        ):
            return True
        if char == "<" and not allow_input_redirection:
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
    if not tokens or tokens[0].rsplit("/", 1)[-1].lower() != "git" or "show" not in tokens:
        return False
    show_index = tokens.index("show")
    skip_next = False
    for token in tokens[show_index + 1 :]:
        if skip_next:
            skip_next = False
            continue
        if token == "--":
            continue
        if token in {"-L", "--line-range"}:
            return False
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


def _is_node_command(command: str, sample: str) -> bool:
    executable = r"(?:\S*/)?"
    if re.search(rf"(^|[\s;&|]){executable}(npm|pnpm|yarn)\s+", command):
        return True
    if re.search(rf"(^|[\s;&|]){executable}(node|npx|vitest|jest|tsx|mocha)\b", command):
        return True
    return "npm err!" in sample or "err_pnpm" in sample or "yarn run" in sample


def _is_search_command(command: str) -> bool:
    if not command or re.search(r"[;&|]", command):
        return False
    tokens = _shell_split(command)
    prefix_index = _skip_command_prefix_tokens(tokens)
    if 0 < prefix_index < len(tokens):
        return _is_search_command(" ".join(tokens[prefix_index:]))
    if not tokens:
        return False
    return tokens[0].rsplit("/", 1)[-1] in {"rg", "grep", "ag", "ack"}


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
