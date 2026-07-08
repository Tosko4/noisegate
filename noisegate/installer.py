from __future__ import annotations

import ast
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
HERMES_ENTRYPOINT_MODULES = {
    "hermes_cli",
    "hermes_cli.main",
    "hermes_cli.cli",
    "hermes_cli.__main__",
}
ENABLE_PLUGIN_CODE = """
from hermes_cli.config import load_config, save_config
cfg = load_config()
plugins = cfg.setdefault("plugins", {})
changed = False
raw_enabled = plugins.get("enabled")
enabled = list(raw_enabled) if isinstance(raw_enabled, list) else []
if "noisegate" not in enabled:
    enabled.append("noisegate")
if raw_enabled != enabled:
    plugins["enabled"] = enabled
    changed = True
raw_disabled = plugins.get("disabled")
if isinstance(raw_disabled, list):
    disabled = [name for name in raw_disabled if name != "noisegate"]
    if raw_disabled != disabled:
        plugins["disabled"] = disabled
        changed = True
if changed:
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
            "restart: not performed by Noisegate",
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
        hermes_python = _validated_python_command(command)
        _validate_hermes_python_console_launcher(executable, text)
        return hermes_python
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
        command_and_args = _shell_exec_command(tokens, assignments=assignments)
        if command_and_args is None:
            continue
        command, args = command_and_args
        if _looks_like_python(command):
            hermes_python = _validated_python_command(command)
            _validate_shell_python_invokes_hermes(executable, args, assignments=assignments)
            return hermes_python
        target = _candidate_launcher_path(command, executable.parent)
        if target is not None and target.exists():
            return _python_from_launcher(target, seen=seen)
    raise InstallHermesError(f"Unsupported Hermes shell launcher: {executable}")


def _shell_exec_command(
    tokens: list[str],
    *,
    assignments: dict[str, str],
) -> tuple[str, list[str]] | None:
    if len(tokens) < 2 or tokens[0] != "exec":
        return None
    args = [_expand_shell_vars(token, assignments) for token in tokens[1:]]
    if not args:
        return None
    if not _looks_like_env(args[0]):
        if _looks_like_shell_assignment(args[0]) or args[0] == "$@" or args[0].startswith("-"):
            return None
        return args[0], args[1:]

    index = 1
    expanded_env_args: list[str] = []
    while index < len(args):
        arg = args[index]
        if arg == "$@":
            return None
        if _looks_like_shell_assignment(arg):
            index += 1
            continue
        if arg in {"-i", "--ignore-environment", "-0", "--null"}:
            index += 1
            continue
        if arg in {"-u", "--unset", "--argv0"}:
            index += 2
            continue
        if arg in {"-S", "--split-string"}:
            split_index = index + 1
            if split_index >= len(args):
                return None
            try:
                expanded_env_args.extend(shlex.split(args[split_index]))
            except ValueError:
                return None
            expanded_env_args.extend(args[split_index + 1 :])
            index = len(args)
            break
        if arg.startswith("-"):
            index += 1
            continue
        break
    expanded_env_args.extend(args[index:])
    if not expanded_env_args:
        return None
    command = expanded_env_args[0]
    if command == "$@" or _looks_like_shell_assignment(command) or command.startswith("-"):
        return None
    return command, expanded_env_args[1:]


def _validate_hermes_python_console_launcher(executable: Path, text: str) -> None:
    body = "\n".join(text.splitlines()[1:])
    try:
        tree = ast.parse(body)
    except SyntaxError as exc:
        raise InstallHermesError(
            "Hermes launcher is not a valid Hermes Python console script: "
            f"{executable}"
        ) from exc
    entrypoint_names = _top_level_hermes_entrypoint_names(tree)
    if entrypoint_names and _module_calls_entrypoint(tree, entrypoint_names):
        return
    if _main_guard_imports_and_calls_entrypoint(tree):
        return
    raise InstallHermesError(
        "Hermes launcher is not a Hermes Python console script: " f"{executable}"
    )


def _top_level_hermes_entrypoint_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module not in HERMES_ENTRYPOINT_MODULES:
            continue
        for alias in node.names:
            if alias.name in {"main", "app"}:
                names.add(alias.asname or alias.name)
    return names


def _module_calls_entrypoint(tree: ast.Module, entrypoint_names: set[str]) -> bool:
    for node in tree.body:
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            if _body_calls_entrypoint(node.body, entrypoint_names):
                return True
            continue
        if isinstance(node, ast.Expr) and _statement_calls_entrypoint(node, entrypoint_names):
            return True
    return False


def _main_guard_imports_and_calls_entrypoint(tree: ast.Module) -> bool:
    for node in tree.body:
        if not isinstance(node, ast.If) or not _is_main_guard(node.test):
            continue
        entrypoint_names = _hermes_entrypoint_names_from_statements(node.body)
        if entrypoint_names and _body_calls_entrypoint(node.body, entrypoint_names):
            return True
    return False


def _hermes_entrypoint_names_from_statements(statements: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for node in statements:
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module not in HERMES_ENTRYPOINT_MODULES:
            continue
        for alias in node.names:
            if alias.name in {"main", "app"}:
                names.add(alias.asname or alias.name)
    return names


def _body_calls_entrypoint(statements: list[ast.stmt], entrypoint_names: set[str]) -> bool:
    return any(_statement_calls_entrypoint(node, entrypoint_names) for node in statements)


def _statement_calls_entrypoint(node: ast.AST, entrypoint_names: set[str]) -> bool:
    if isinstance(
        node,
        (
            ast.FunctionDef,
            ast.AsyncFunctionDef,
            ast.ClassDef,
            ast.Lambda,
            ast.If,
            ast.For,
            ast.AsyncFor,
            ast.While,
            ast.Try,
            ast.With,
            ast.AsyncWith,
        ),
    ):
        return False
    for child in ast.iter_child_nodes(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id in entrypoint_names
        ):
            return True
        if _statement_calls_entrypoint(child, entrypoint_names):
            return True
    return False


def _is_main_guard(test: ast.expr) -> bool:
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    left = test.left
    right = test.comparators[0]
    return (
        _is_name_constant(left, "__name__") and _is_string_constant(right, "__main__")
    ) or (_is_string_constant(left, "__main__") and _is_name_constant(right, "__name__"))


def _is_name_constant(node: ast.expr, value: str) -> bool:
    return isinstance(node, ast.Name) and node.id == value


def _is_string_constant(node: ast.expr, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _validate_shell_python_invokes_hermes(
    executable: Path,
    args: list[str],
    *,
    assignments: dict[str, str],
) -> None:
    args = [_expand_shell_vars(arg, assignments) for arg in args]
    module = _python_interpreter_module_arg(args)
    if module is not None and _is_hermes_cli_module(module):
        return
    raise InstallHermesError(
        "Hermes shell launcher does not invoke hermes_cli with the resolved Python: "
        f"{executable}"
    )


def _python_interpreter_module_arg(args: list[str]) -> str | None:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"$@", "--"}:
            return None
        if arg == "-m":
            module_index = index + 1
            return args[module_index] if module_index < len(args) else None
        if arg == "-c":
            return None
        if arg in {"-W", "-X"}:
            index += 2
            continue
        if arg.startswith("-W") or arg.startswith("-X") or arg.startswith("-"):
            index += 1
            continue
        return None
    return None


def _is_hermes_cli_module(value: str) -> bool:
    return value in HERMES_ENTRYPOINT_MODULES


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
