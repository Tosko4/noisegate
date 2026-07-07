from __future__ import annotations

from pathlib import Path

import pytest

from noisegate import installer as installer_module
from noisegate.installer import (
    InstallHermesError,
    build_install_hermes_plan,
)


def test_build_install_hermes_plan_uses_hermes_python_shebang(tmp_path: Path) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text("#!/opt/hermes/.venv/bin/python\nprint('launcher')\n", encoding="utf-8")

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
    assert "name for name in disabled if name != \"noisegate\"" in plan.enable_command[-1]
    assert plan.doctor_command == [
        "/opt/hermes/.venv/bin/python",
        "-m",
        "noisegate.cli",
        "doctor",
    ]


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
    hermes.write_text("#!/usr/bin/env -S /opt/hermes/.venv/bin/python3 -s\n", encoding="utf-8")

    plan = build_install_hermes_plan(hermes=str(hermes), installer="pip")

    assert plan.hermes_python == "/opt/hermes/.venv/bin/python3"


def test_build_install_hermes_plan_supports_bash_shim_to_console_script(
    tmp_path: Path,
) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    console_script = venv_bin / "hermes"
    console_script.write_text("#!/opt/hermes/.venv/bin/python3\n", encoding="utf-8")
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
    console_script.write_text("#!/opt/hermes/.venv/bin/python3\n", encoding="utf-8")
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
    console_script.write_text("#!/opt/hermes/.venv/bin/python3\n", encoding="utf-8")
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
    hermes.write_text(f"#!{python}\n", encoding="utf-8")

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
