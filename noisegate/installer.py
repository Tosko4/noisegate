from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ._version import __version__

DEFAULT_PACKAGE_SPEC = f"noisegate-hermes=={__version__}"
PYTHON_ENV_VARS = ("PYTHONHOME", "PYTHONPATH")
SHELL_LAUNCHER_NAMES = {"bash", "dash", "sh", "zsh"}
WINDOWS_LAUNCHER_SUFFIXES = {".bat", ".cmd", ".exe"}
WINDOWS_PYTHON_CANDIDATES = ("python.exe", "python3.exe", "python", "python3")
SHELL_ASSIGNMENT_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)=(?P<quote>[\"']?)(.*?)(?P=quote)$"
)
SHELL_VARIABLE_REF_RE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)
ENABLE_PLUGIN_CODE = """
from hermes_cli.config import load_config, save_config
cfg = load_config()
plugins = cfg.setdefault("plugins", {})
enabled = plugins.get("enabled") if isinstance(plugins.get("enabled"), list) else []
disabled = plugins.get("disabled") if isinstance(plugins.get("disabled"), list) else []
if "noisegate" not in enabled:
    enabled.append("noisegate")
plugins["enabled"] = enabled
plugins["disabled"] = [name for name in disabled if name != "noisegate"]
save_config(cfg)
""".strip()


class InstallHermesError(RuntimeError):
    """Raised when Noisegate cannot safely install itself into Hermes."""


@dataclass(frozen=True)
class InstallHermesPlan:
    hermes_executable: Path
    hermes_python: str
    package_spec: str
    install_command: list[str]
    enable_command: list[str]
    doctor_command: list[str]

    def as_lines(self) -> list[str]:
        return [
            f"hermes: {self.hermes_executable}",
            f"hermes_python: {self.hermes_python}",
            f"package: {self.package_spec}",
            f"install: {shlex.join(self.install_command)}",
            "enable: " + shlex.join(self.enable_command),
            f"doctor: {shlex.join(self.doctor_command)}",
        ]


def build_install_hermes_plan(
    *,
    hermes: str = "hermes",
    package_spec: str = DEFAULT_PACKAGE_SPEC,
    installer: str | None = None,
) -> InstallHermesPlan:
    hermes_path = _resolve_executable(hermes)
    hermes_python = _python_from_launcher(hermes_path)
    install_command = _install_command(hermes_python, package_spec, installer=installer)
    return InstallHermesPlan(
        hermes_executable=hermes_path,
        hermes_python=hermes_python,
        package_spec=package_spec,
        install_command=install_command,
        enable_command=[hermes_python, "-c", ENABLE_PLUGIN_CODE],
        doctor_command=[hermes_python, "-m", "noisegate.cli", "doctor"],
    )


def install_hermes(
    *,
    hermes: str = "hermes",
    package_spec: str = DEFAULT_PACKAGE_SPEC,
    installer: str | None = None,
    dry_run: bool = False,
) -> InstallHermesPlan:
    plan = build_install_hermes_plan(
        hermes=hermes,
        package_spec=package_spec,
        installer=installer,
    )
    if dry_run:
        return plan
    for command in (plan.install_command, plan.enable_command, plan.doctor_command):
        _run(command)
    return plan


def _resolve_executable(name_or_path: str) -> Path:
    path = Path(name_or_path)
    if path.parent != Path(".") or path.is_absolute():
        resolved = path.expanduser().resolve()
        if not resolved.exists():
            raise InstallHermesError(f"Hermes executable not found: {name_or_path}")
        return resolved
    found = shutil.which(name_or_path)
    if not found:
        raise InstallHermesError(f"Hermes executable not found on PATH: {name_or_path}")
    return Path(found).resolve()


def _python_from_launcher(executable: Path, *, seen: set[Path] | None = None) -> str:
    seen = seen or set()
    executable = executable.expanduser().resolve()
    if executable in seen:
        raise InstallHermesError(f"Hermes launcher recursion detected: {executable}")
    seen.add(executable)
    try:
        text = executable.read_text(encoding="utf-8", errors="replace")
        first_line = text.splitlines()[0]
    except (OSError, IndexError) as exc:
        raise InstallHermesError(f"Cannot read Hermes launcher shebang: {executable}") from exc
    if not first_line.startswith("#!"):
        if _looks_like_windows_launcher(executable):
            return _python_from_windows_launcher(executable)
        raise InstallHermesError(f"Hermes launcher has no Python shebang: {executable}")
    parts = shlex.split(first_line[2:].strip())
    if not parts:
        raise InstallHermesError(f"Hermes launcher has an empty shebang: {executable}")
    command = _python_command_from_shebang_parts(parts)
    if _looks_like_python(command):
        return _validated_python_command(command)
    if _looks_like_shell(command):
        return _python_from_shell_launcher(executable, text, seen=seen)
    raise InstallHermesError(
        f"Hermes launcher shebang is not a supported Python interpreter: {first_line}"
    )


def _python_from_shell_launcher(executable: Path, text: str, *, seen: set[Path]) -> str:
    assignments = _shell_assignments(text)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("exec "):
            continue
        try:
            tokens = shlex.split(stripped)
        except ValueError:
            continue
        for token in tokens[1:]:
            if token == "$@" or token.startswith("-"):
                continue
            expanded = _expand_shell_vars(token, assignments)
            if _looks_like_env(expanded) or _looks_like_shell_assignment(expanded):
                continue
            if _looks_like_python(expanded):
                return _validated_python_command(expanded)
            target = _candidate_launcher_path(expanded, executable.parent)
            if target is not None and target.exists():
                return _python_from_launcher(target, seen=seen)
    raise InstallHermesError(f"Unsupported Hermes shell launcher: {executable}")


def _looks_like_windows_launcher(executable: Path) -> bool:
    return executable.suffix.lower() in WINDOWS_LAUNCHER_SUFFIXES


def _python_from_windows_launcher(executable: Path) -> str:
    for name in WINDOWS_PYTHON_CANDIDATES:
        candidate = executable.parent / name
        if candidate.exists():
            return _validated_python_command(str(candidate))
    raise InstallHermesError(
        "Hermes Windows launcher has no adjacent virtual-environment Python: "
        f"{executable}"
    )


def _shell_assignments(text: str) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "$(" in stripped:
            continue
        match = SHELL_ASSIGNMENT_RE.match(stripped)
        if match:
            assignments[match.group(1)] = match.group(3)
    return assignments


def _expand_shell_vars(value: str, assignments: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("bare")
        if name is None:
            return match.group(0)
        return assignments.get(name, match.group(0))

    return SHELL_VARIABLE_REF_RE.sub(replace, value)


def _candidate_launcher_path(value: str, base: Path) -> Path | None:
    if not value or "$" in value:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _python_command_from_shebang_parts(parts: list[str]) -> str:
    launcher = Path(parts[0]).name
    if launcher == "env":
        args = parts[1:]
        if args[:1] == ["-S"]:
            args = args[1:]
        if not args or args[0].startswith("-"):
            raise InstallHermesError("Hermes launcher uses an unsupported env shebang")
        return args[0]
    return parts[0]


def _looks_like_shell(command: str) -> bool:
    return Path(command).name.lower() in SHELL_LAUNCHER_NAMES


def _looks_like_env(command: str) -> bool:
    return Path(command).name.lower() == "env"


def _looks_like_shell_assignment(value: str) -> bool:
    return bool(SHELL_ASSIGNMENT_RE.match(value))


def _validated_python_command(command: str) -> str:
    path = Path(command)
    if not path.is_absolute():
        raise InstallHermesError(
            "Hermes launcher uses a non-absolute Python interpreter; "
            "use a Hermes launcher with an absolute venv Python shebang"
        )
    if not _looks_like_venv_python(path):
        raise InstallHermesError(
            "Hermes launcher uses a Python interpreter outside a virtual environment; "
            "use a Hermes launcher with an absolute venv Python shebang"
        )
    return command


def _looks_like_venv_python(path: Path) -> bool:
    candidates = [path.parent.parent, *path.parents]
    if any((candidate / "pyvenv.cfg").exists() for candidate in candidates):
        return True
    venv_names = {".venv", "venv", "virtualenv"}
    return path.parent.name in {"bin", "Scripts"} and path.parent.parent.name in venv_names


def _looks_like_python(command: str) -> bool:
    name = Path(command).name.lower()
    return name.startswith("python") or name.startswith("pypy")


def _install_command(
    hermes_python: str,
    package_spec: str,
    *,
    installer: str | None = None,
) -> list[str]:
    selected = installer or ("uv" if shutil.which("uv") else "pip")
    if selected == "uv":
        uv = shutil.which("uv") or "uv"
        return [uv, "pip", "install", "--python", hermes_python, package_spec]
    if selected == "pip":
        return [hermes_python, "-m", "pip", "install", package_spec]
    raise InstallHermesError(f"Unsupported installer: {selected}")


def _run(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True, env=_subprocess_env())
    except FileNotFoundError as exc:
        raise InstallHermesError(f"Command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        message = f"Command failed ({exc.returncode}): {shlex.join(command)}"
        raise InstallHermesError(message) from exc


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in PYTHON_ENV_VARS:
        env.pop(name, None)
    return env


def current_package_spec_for_dev() -> str:
    root = Path(__file__).resolve().parent.parent
    if (root / "pyproject.toml").exists():
        return str(root)
    return DEFAULT_PACKAGE_SPEC


def cli_error(exc: Exception) -> int:
    print(f"noisegate install-hermes: {exc}", file=sys.stderr)
    return 2
