from __future__ import annotations

import copy
import sys
import types
from pathlib import Path

import pytest

from noisegate import installer as installer_module
from noisegate.installer import (
    ENABLE_PLUGIN_CODE,
    InstallHermesError,
    build_install_hermes_plan,
    install_hermes,
)


def write_hermes_console_script(
    path: Path,
    *,
    python: str = "/opt/hermes/.venv/bin/python",
) -> None:
    path.write_text(
        f"#!{python}\n"
        "# -*- coding: utf-8 -*-\n"
        "import re\n"
        "import sys\n"
        "from hermes_cli.cli import main\n"
        "if __name__ == '__main__':\n"
        "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
        "    sys.exit(main())\n",
        encoding="utf-8",
    )


def run_enable_plugin_code(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, object],
) -> list[dict[str, object]]:
    saved: list[dict[str, object]] = []
    hermes_cli = types.ModuleType("hermes_cli")
    config_module = types.ModuleType("hermes_cli.config")

    def load_config() -> dict[str, object]:
        return config

    def save_config(value: dict[str, object]) -> None:
        saved.append(copy.deepcopy(value))

    config_module.load_config = load_config  # type: ignore[attr-defined]
    config_module.save_config = save_config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_module)
    exec(ENABLE_PLUGIN_CODE, {})
    return saved

def test_build_install_hermes_plan_uses_hermes_python_shebang(tmp_path: Path) -> None:
    hermes = tmp_path / "hermes"
    write_hermes_console_script(hermes)

    plan = build_install_hermes_plan(
        hermes=str(hermes),
        package_spec="noisegate-hermes==1.2.3",
        installer="uv",
    )

    assert plan.hermes_executable == hermes
    assert plan.hermes_python == "/opt/hermes/.venv/bin/python"
    assert plan.package_spec == "noisegate-hermes==1.2.3"
    assert plan.install_command[-3:] == [
        "--python",
        "/opt/hermes/.venv/bin/python",
        "noisegate-hermes==1.2.3",
    ]
    assert plan.enable_command[0] == "/opt/hermes/.venv/bin/python"
    assert "hermes_cli.config" in plan.enable_command[-1]
    assert "plugins[\"disabled\"]" in plan.enable_command[-1]
    assert "name for name in raw_disabled if name != \"noisegate\"" in plan.enable_command[-1]
    assert plan.doctor_command == [
        "/opt/hermes/.venv/bin/python",
        "-m",
        "noisegate.cli",
        "doctor",
    ]


def test_build_install_hermes_plan_rejects_non_console_python_launcher(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/opt/hermes/.venv/bin/python\nprint('not hermes')\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallHermesError, match="not a Hermes Python console script"):
        build_install_hermes_plan(hermes=str(hermes), installer="pip")

def test_build_install_hermes_plan_rejects_empty_python_launcher(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text("#!/opt/hermes/.venv/bin/python\n", encoding="utf-8")

    with pytest.raises(InstallHermesError, match="not a Hermes Python console script"):
        build_install_hermes_plan(hermes=str(hermes), installer="pip")


def test_build_install_hermes_plan_rejects_hermes_words_without_import(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/opt/hermes/.venv/bin/python\nprint('main hermes_cli')\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallHermesError, match="not a Hermes Python console script"):
        build_install_hermes_plan(hermes=str(hermes), installer="pip")


def test_build_install_hermes_plan_rejects_prefixed_hermes_import(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/opt/hermes/.venv/bin/python\nfrom not_hermes_cli import main\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallHermesError, match="not a Hermes Python console script"):
        build_install_hermes_plan(hermes=str(hermes), installer="pip")


def test_build_install_hermes_plan_rejects_non_entrypoint_hermes_import(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/opt/hermes/.venv/bin/python\nimport hermes_cli.config\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallHermesError, match="not a Hermes Python console script"):
        build_install_hermes_plan(hermes=str(hermes), installer="pip")


def test_build_install_hermes_plan_rejects_hermes_import_in_docstring(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/opt/hermes/.venv/bin/python\n"
        "\"\"\"\n"
        "from hermes_cli.main import main\n"
        "\"\"\"\n"
        "print('not hermes')\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallHermesError, match="not a Hermes Python console script"):
        build_install_hermes_plan(hermes=str(hermes), installer="pip")


def test_build_install_hermes_plan_rejects_dead_hermes_import(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/opt/hermes/.venv/bin/python\n"
        "if False:\n"
        "    from hermes_cli.main import main\n"
        "print('not hermes')\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallHermesError, match="not a Hermes Python console script"):
        build_install_hermes_plan(hermes=str(hermes), installer="pip")


def test_build_install_hermes_plan_rejects_nested_hermes_import(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/opt/hermes/.venv/bin/python\n"
        "def unrelated():\n"
        "    from hermes_cli.main import main\n"
        "print('not hermes')\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallHermesError, match="not a Hermes Python console script"):
        build_install_hermes_plan(hermes=str(hermes), installer="pip")


def test_build_install_hermes_plan_supports_aliased_hermes_entrypoint(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/opt/hermes/.venv/bin/python\n"
        "import sys\n"
        "from hermes_cli.main import main as hermes_main\n"
        "if '__main__' == __name__:\n"
        "    sys.exit(hermes_main())\n",
        encoding="utf-8",
    )

    plan = build_install_hermes_plan(hermes=str(hermes), installer="pip")

    assert plan.hermes_python == "/opt/hermes/.venv/bin/python"


def test_build_install_hermes_plan_supports_main_guard_local_import(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/opt/hermes/.venv/bin/python\n"
        "if __name__ == '__main__':\n"
        "    from hermes_cli.main import main\n"
        "    main()\n",
        encoding="utf-8",
    )

    plan = build_install_hermes_plan(hermes=str(hermes), installer="pip")

    assert plan.hermes_python == "/opt/hermes/.venv/bin/python"


def test_build_install_hermes_plan_rejects_guarded_import_without_direct_call(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/opt/hermes/.venv/bin/python\n"
        "if __name__ == '__main__':\n"
        "    from hermes_cli.main import main\n"
        "    def not_called():\n"
        "        main()\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallHermesError, match="not a Hermes Python console script"):
        build_install_hermes_plan(hermes=str(hermes), installer="pip")


def test_build_install_hermes_plan_rejects_unreachable_guarded_entrypoint_call(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/opt/hermes/.venv/bin/python\n"
        "if __name__ == '__main__':\n"
        "    from hermes_cli.main import main\n"
        "    if False:\n"
        "        main()\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallHermesError, match="not a Hermes Python console script"):
        build_install_hermes_plan(hermes=str(hermes), installer="pip")


def test_build_install_hermes_plan_rejects_env_python_shebang(tmp_path: Path) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    with pytest.raises(InstallHermesError, match="non-absolute Python interpreter"):
        build_install_hermes_plan(hermes=str(hermes), installer="pip")


def test_build_install_hermes_plan_rejects_env_shebang_with_split_args(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text("#!/usr/bin/env -S python3 -s\n", encoding="utf-8")

    with pytest.raises(InstallHermesError, match="non-absolute Python interpreter"):
        build_install_hermes_plan(hermes=str(hermes), installer="pip")


def test_build_install_hermes_plan_supports_env_shebang_with_absolute_python(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    write_hermes_console_script(
        hermes, python="/usr/bin/env -S /opt/hermes/.venv/bin/python3 -s"
    )

    plan = build_install_hermes_plan(hermes=str(hermes), installer="pip")

    assert plan.hermes_python == "/opt/hermes/.venv/bin/python3"


def test_build_install_hermes_plan_supports_bash_shim_to_console_script(
    tmp_path: Path,
) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    console_script = venv_bin / "hermes"
    write_hermes_console_script(console_script, python="/opt/hermes/.venv/bin/python3")
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/usr/bin/env bash\n"
        "unset PYTHONHOME PYTHONPATH\n"
        f"HERMES_CONSOLE='{console_script}'\n"
        'exec "$HERMES_CONSOLE" "$@"\n',
        encoding="utf-8",
    )

    plan = build_install_hermes_plan(hermes=str(hermes), installer="pip")

    assert plan.hermes_python == "/opt/hermes/.venv/bin/python3"


def test_build_install_hermes_plan_expands_full_shell_variable_names(
    tmp_path: Path,
) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    console_script = venv_bin / "hermes"
    write_hermes_console_script(console_script, python="/opt/hermes/.venv/bin/python3")
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/usr/bin/env bash\n"
        f"HERMES='{tmp_path / 'wrong'}'\n"
        f"HERMES_CONSOLE='{console_script}'\n"
        'exec "$HERMES_CONSOLE" "$@"\n',
        encoding="utf-8",
    )

    plan = build_install_hermes_plan(hermes=str(hermes), installer="pip")

    assert plan.hermes_python == "/opt/hermes/.venv/bin/python3"


def test_build_install_hermes_plan_expands_braced_shell_variable_paths(
    tmp_path: Path,
) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    console_script = venv_bin / "hermes"
    write_hermes_console_script(console_script, python="/opt/hermes/.venv/bin/python3")
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/usr/bin/env bash\n"
        f"HERMES_HOME='{venv_bin.parent}'\n"
        'exec "${HERMES_HOME}/bin/hermes" "$@"\n',
        encoding="utf-8",
    )

    plan = build_install_hermes_plan(hermes=str(hermes), installer="pip")

    assert plan.hermes_python == "/opt/hermes/.venv/bin/python3"


def test_build_install_hermes_plan_supports_bash_shim_to_python(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/usr/bin/env bash\n"
        "unset PYTHONHOME PYTHONPATH\n"
        "exec /opt/hermes/.venv/bin/python3 -m hermes_cli.main \"$@\"\n",
        encoding="utf-8",
    )

    plan = build_install_hermes_plan(hermes=str(hermes), installer="pip")

    assert plan.hermes_python == "/opt/hermes/.venv/bin/python3"


def test_build_install_hermes_plan_rejects_absolute_system_python_shebang(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text("#!/usr/bin/python3\n", encoding="utf-8")

    with pytest.raises(InstallHermesError, match="outside a virtual environment"):
        build_install_hermes_plan(hermes=str(hermes), installer="uv")


def test_build_install_hermes_plan_rejects_env_absolute_system_python_shebang(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text("#!/usr/bin/env -S /usr/bin/python3 -s\n", encoding="utf-8")

    with pytest.raises(InstallHermesError, match="outside a virtual environment"):
        build_install_hermes_plan(hermes=str(hermes), installer="uv")


def test_build_install_hermes_plan_accepts_existing_custom_named_venv(
    tmp_path: Path,
) -> None:
    prefix = tmp_path / "hermes-runtime"
    python = prefix / "bin" / "python3"
    python.parent.mkdir(parents=True)
    (prefix / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    hermes = tmp_path / "hermes"
    write_hermes_console_script(hermes, python=str(python))

    plan = build_install_hermes_plan(hermes=str(hermes), installer="pip")

    assert plan.hermes_python == str(python)


def test_build_install_hermes_plan_supports_windows_exe_launcher(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    python = scripts / "python.exe"
    python.write_bytes(b"")
    (tmp_path / "venv" / "pyvenv.cfg").write_text("home = C:\\Python311\n", encoding="utf-8")
    hermes = scripts / "hermes.exe"
    hermes.write_bytes(b"MZ\x00\x00binary launcher")

    plan = build_install_hermes_plan(hermes=str(hermes), installer="pip")

    assert plan.hermes_python == str(python)


def test_build_install_hermes_plan_supports_windows_cmd_launcher(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    python = scripts / "python.exe"
    python.write_bytes(b"")
    (tmp_path / "venv" / "pyvenv.cfg").write_text("home = C:\\Python311\n", encoding="utf-8")
    hermes = scripts / "hermes.cmd"
    hermes.write_text("@echo off\r\nhermes.exe %*\r\n", encoding="utf-8")

    plan = build_install_hermes_plan(hermes=str(hermes), installer="pip")

    assert plan.hermes_python == str(python)


def test_build_install_hermes_plan_rejects_windows_launcher_without_venv_python(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    hermes = scripts / "hermes.exe"
    hermes.write_bytes(b"MZ\x00\x00binary launcher")

    with pytest.raises(InstallHermesError, match="no adjacent virtual-environment Python"):
        build_install_hermes_plan(hermes=str(hermes), installer="pip")


def test_build_install_hermes_plan_supports_bash_env_shim_to_python(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/usr/bin/env bash\n"
        "exec /usr/bin/env -S /opt/hermes/.venv/bin/python3 -m hermes_cli.main \"$@\"\n",
        encoding="utf-8",
    )

    plan = build_install_hermes_plan(hermes=str(hermes), installer="pip")

    assert plan.hermes_python == "/opt/hermes/.venv/bin/python3"


def test_build_install_hermes_plan_expands_shell_module_variable(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/usr/bin/env bash\n"
        "HERMES_MODULE='hermes_cli.main'\n"
        "exec /opt/hermes/.venv/bin/python3 -m \"$HERMES_MODULE\" \"$@\"\n",
        encoding="utf-8",
    )

    plan = build_install_hermes_plan(hermes=str(hermes), installer="pip")

    assert plan.hermes_python == "/opt/hermes/.venv/bin/python3"


def test_build_install_hermes_plan_rejects_prefixed_shell_module(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/usr/bin/env bash\n"
        "exec /opt/hermes/.venv/bin/python3 -m hermes_cli_evil \"$@\"\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallHermesError, match="does not invoke hermes_cli"):
        build_install_hermes_plan(hermes=str(hermes), installer="pip")


def test_build_install_hermes_plan_rejects_script_before_shell_module(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/usr/bin/env bash\n"
        "exec /opt/hermes/.venv/bin/python3 not_hermes.py -m hermes_cli.main\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallHermesError, match="does not invoke hermes_cli"):
        build_install_hermes_plan(hermes=str(hermes), installer="pip")


def test_build_install_hermes_plan_rejects_python_arg_to_shell_helper(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/usr/bin/env bash\n"
        "exec helper /opt/hermes/.venv/bin/python3 -m hermes_cli.main \"$@\"\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallHermesError, match="Unsupported Hermes shell launcher"):
        build_install_hermes_plan(hermes=str(hermes), installer="pip")


def test_build_install_hermes_plan_rejects_python_arg_to_env_helper(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/usr/bin/env bash\n"
        "exec /usr/bin/env helper /opt/hermes/.venv/bin/python3 -m hermes_cli.main\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallHermesError, match="Unsupported Hermes shell launcher"):
        build_install_hermes_plan(hermes=str(hermes), installer="pip")


def test_build_install_hermes_plan_rejects_bash_env_shim_to_bare_python(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/usr/bin/env bash\n"
        "exec /usr/bin/env python3 -m hermes_cli.main \"$@\"\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallHermesError, match="non-absolute Python interpreter"):
        build_install_hermes_plan(hermes=str(hermes), installer="pip")


def test_build_install_hermes_plan_rejects_non_python_shebang(tmp_path: Path) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text("#!/bin/sh\necho launcher\n", encoding="utf-8")

    with pytest.raises(InstallHermesError, match="Unsupported Hermes shell launcher"):
        build_install_hermes_plan(hermes=str(hermes))


def test_build_install_hermes_plan_rejects_unsupported_env_shebang(tmp_path: Path) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text("#!/usr/bin/env -i python3\n", encoding="utf-8")

    with pytest.raises(InstallHermesError, match="unsupported env shebang"):
        build_install_hermes_plan(hermes=str(hermes))


def test_build_install_hermes_plan_rejects_missing_shebang(tmp_path: Path) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text("print('not a launcher')\n", encoding="utf-8")

    with pytest.raises(InstallHermesError, match="no Python shebang"):
        build_install_hermes_plan(hermes=str(hermes))


def test_run_scrubs_python_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, str]] = []

    def fake_run(
        command: list[str],
        *,
        check: bool,
        env: dict[str, str],
    ) -> None:
        assert command == ["python", "-m", "noisegate.cli", "doctor"]
        assert check is True
        calls.append(env)

    monkeypatch.setenv("PYTHONPATH", "/tmp/checkout")
    monkeypatch.setenv("PYTHONHOME", "/tmp/python-home")
    monkeypatch.setenv("NOISEGATE_TEST_KEEP", "1")
    monkeypatch.setattr(installer_module.subprocess, "run", fake_run)

    installer_module._run(["python", "-m", "noisegate.cli", "doctor"])

    assert calls
    assert "PYTHONPATH" not in calls[0]
    assert "PYTHONHOME" not in calls[0]
    assert calls[0]["NOISEGATE_TEST_KEEP"] == "1"



def test_install_hermes_dry_run_does_not_execute_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes = tmp_path / "hermes"
    write_hermes_console_script(hermes)
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> None:
        calls.append(command)
        raise AssertionError("dry-run must not execute installer commands")

    monkeypatch.setattr(installer_module, "_run", fake_run)

    plan = install_hermes(hermes=str(hermes), installer="pip", dry_run=True)

    assert plan.hermes_python == "/opt/hermes/.venv/bin/python"
    assert calls == []


def test_enable_plugin_code_leaves_already_enabled_config_unsaved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config: dict[str, object] = {"plugins": {"enabled": ["noisegate"], "disabled": []}}

    saved = run_enable_plugin_code(monkeypatch, config)

    assert saved == []
    assert config == {"plugins": {"enabled": ["noisegate"], "disabled": []}}


def test_enable_plugin_code_removes_disabled_noisegate_and_saves_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config: dict[str, object] = {
        "plugins": {"enabled": ["other"], "disabled": ["noisegate", "legacy"]}
    }

    saved = run_enable_plugin_code(monkeypatch, config)

    assert saved == [
        {"plugins": {"enabled": ["other", "noisegate"], "disabled": ["legacy"]}}
    ]
    assert config == saved[0]
