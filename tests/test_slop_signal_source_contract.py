from __future__ import annotations

import json
from pathlib import Path

from noisegate.engine import NoisegateOptions, reduce_text
from noisegate.plugin import transform_tool_result


def opts(**overrides: object) -> NoisegateOptions:
    values: dict[str, object] = {
        "max_chars": 700,
        "max_lines": 30,
        "head_lines": 4,
        "tail_lines": 3,
        "important_context_lines": 1,
    }
    values.update(overrides)
    return NoisegateOptions(**values)  # type: ignore[arg-type]


def package_progress(prefix: str, count: int = 180) -> str:
    return "\n".join(f"{prefix} progress line {index:03d}" for index in range(count))


def assert_compacted(result_text: str, raw: str) -> None:
    assert len(result_text) < len(raw)
    assert "[noisegate: omitted" in result_text


def test_slop_package_and_build_commands_shrink_meaningfully() -> None:
    cases = {
        "apt-get update": (
            "Get:1 http://deb.example stable/main amd64 Packages\n"
            + package_progress("apt update")
        ),
        "/usr/bin/apt-get update": (
            "Get:1 http://deb.example stable/main amd64 Packages\n"
            + package_progress("apt path update")
        ),
        "sudo apt install postgresql": (
            "Reading package lists... Done\n" + package_progress("apt install")
        ),
        "uv sync": "Resolved 192 packages in 1.2s\n" + package_progress("uv sync"),
        "/home/me/.local/bin/uv sync": (
            "Resolved 192 packages in 1.2s\n" + package_progress("uv path sync")
        ),
        "python -m pip install requests": (
            "Collecting requests\n" + package_progress("pip install")
        ),
        "/opt/venv/bin/python -m pip install requests": (
            "Collecting requests\n" + package_progress("python path pip install")
        ),
        "npm install": "added 451 packages in 12s\n" + package_progress("npm install"),
        "/usr/bin/npm install": (
            "added 451 packages in 12s\n" + package_progress("npm path install")
        ),
        "docker build .": (
            "#1 [internal] load build definition from Dockerfile\n"
            + package_progress("docker build")
        ),
        "/usr/bin/docker build .": (
            "#1 [internal] load build definition from Dockerfile\n"
            + package_progress("docker path build")
        ),
        "docker buildx build .": (
            "#1 [internal] load build definition from Dockerfile\n"
            + package_progress("docker buildx")
        ),
        "docker --context default build .": (
            "#1 [internal] load build definition from Dockerfile\n"
            + package_progress("docker context")
        ),
        "docker -H unix:///tmp/docker.sock build .": (
            "#1 [internal] load build definition from Dockerfile\n"
            + package_progress("docker host")
        ),
        "docker --config /tmp/docker build .": (
            "#1 [internal] load build definition from Dockerfile\n"
            + package_progress("docker config")
        ),
        "docker --tlscacert ca.pem build .": (
            "#1 [internal] load build definition from Dockerfile\n"
            + package_progress("docker tls")
        ),
        "docker --log-level debug build .": (
            "#1 [internal] load build definition from Dockerfile\n"
            + package_progress("docker log-level")
        ),
        "docker buildx --builder default build .": (
            "#1 [internal] load build definition from Dockerfile\n"
            + package_progress("docker buildx builder")
        ),
        "docker buildx bake": (
            "#1 [internal] load build definition from Dockerfile\n"
            + package_progress("docker buildx bake")
        ),
        "docker compose --project-name app build": (
            "#1 [internal] load build definition from Dockerfile\n"
            + package_progress("docker compose")
        ),
        "docker compose --env-file .env build": (
            "#1 [internal] load build definition from Dockerfile\n"
            + package_progress("docker compose env-file")
        ),
        "docker compose up --build": (
            "#1 [internal] load build definition from Dockerfile\n"
            + package_progress("docker compose up build")
        ),
        "docker compose run --build app": (
            "#1 [internal] load build definition from Dockerfile\n"
            + package_progress("docker compose run build")
        ),
        "docker compose -p app --project-directory . build": (
            "#1 [internal] load build definition from Dockerfile\n"
            + package_progress("docker compose project")
        ),
        "docker compose -f compose.yaml logs api": (
            "api-1  | service booted\n" + package_progress("docker compose logs")
        ),
        "docker compose --ansi never logs api": (
            "api-1  | service booted\n" + package_progress("docker compose ansi logs")
        ),
        "docker container logs api": (
            "api-1  | service booted\n" + package_progress("docker container logs")
        ),
    }

    for command, raw in cases.items():
        result = reduce_text(raw, command=command, tool_name="terminal", options=opts())

        assert result.changed is True, command
        assert_compacted(result.text, raw)
        assert result.metadata["command_class"] in {
            "apt",
            "python_package",
            "node",
            "docker_build",
            "docker_logs",
        }


def test_signal_reducers_keep_failures_tracebacks_and_conflicts_visible() -> None:
    cases = {
        "apt install imaginary-package": (
            package_progress("apt install before", 80)
            + "\nE: Unable to locate package imaginary-package\n"
            + package_progress("apt install after", 80)
        ),
        "bash -lc 'apt install imaginary-package'": (
            package_progress("apt install before", 80)
            + "\nE: Unable to locate package imaginary-package\n"
            + package_progress("apt install after", 80)
        ),
        "uv sync": (
            package_progress("uv sync before", 80)
            + "\nResolutionImpossible: Because package-a conflicts with package-b\n"
            + package_progress("uv sync after", 80)
        ),
        "sh -c 'uv sync'": (
            package_progress("uv sync before", 80)
            + "\nResolutionImpossible: Because package-a conflicts with package-b\n"
            + package_progress("uv sync after", 80)
        ),
        "python -m pip install missing-package": (
            package_progress("pip before", 80)
            + "\nERROR: No matching distribution found for missing-package\n"
            + package_progress("pip after", 80)
        ),
        "python -I -u -m pip install missing-package": (
            package_progress("pip before", 80)
            + "\nERROR: No matching distribution found for missing-package\n"
            + package_progress("pip after", 80)
        ),
        "uv run pip install missing-package": (
            package_progress("pip before", 80)
            + "\nERROR: No matching distribution found for missing-package\n"
            + package_progress("pip after", 80)
        ),
        "uv run python -I -m pip install missing-package": (
            package_progress("pip before", 80)
            + "\nERROR: No matching distribution found for missing-package\n"
            + package_progress("pip after", 80)
        ),
        "xargs pip install missing-package": (
            package_progress("pip before", 80)
            + "\nERROR: No matching distribution found for missing-package\n"
            + package_progress("pip after", 80)
        ),
        "find reqs -name '*.txt' | xargs -I {} pip install -r {}": (
            package_progress("pip before", 80)
            + "\nERROR: No matching distribution found for missing-package\n"
            + package_progress("pip after", 80)
        ),
        "xargs -a packages.txt pip install missing-package": (
            package_progress("pip before", 80)
            + "\nERROR: No matching distribution found for missing-package\n"
            + package_progress("pip after", 80)
        ),
        "xargs -t pip install missing-package": (
            package_progress("pip before", 80)
            + "\nERROR: No matching distribution found for missing-package\n"
            + package_progress("pip after", 80)
        ),
        "cd repo && bash -lc 'uv sync'": (
            package_progress("uv sync before", 80)
            + "\nResolutionImpossible: Because package-a conflicts with package-b\n"
            + package_progress("uv sync after", 80)
        ),
        "pytest -q": (
            package_progress("pytest before", 80)
            + "\nTraceback (most recent call last):\n"
            + "E       AssertionError: boom\n"
            + "FAILED tests/test_demo.py::test_signal\n"
            + package_progress("pytest after", 80)
        ),
        "docker build .": (
            package_progress("docker before", 80)
            + "\n#8 ERROR: failed to solve: process "
            + '"/bin/sh -c false" did not complete successfully\n'
            + package_progress("docker after", 80)
        ),
        "/usr/bin/docker build .": (
            package_progress("docker before", 80)
            + "\n#8 ERROR: failed to solve: process exited\n"
            + package_progress("docker after", 80)
        ),
        "bash -lc 'docker build .'": (
            package_progress("docker before", 80)
            + "\n#8 ERROR: failed to solve: process exited\n"
            + package_progress("docker after", 80)
        ),
        "bash -lc '/usr/bin/docker build .'": (
            package_progress("docker before", 80)
            + "\n#8 ERROR: failed to solve: path-qualified shell process exited\n"
            + package_progress("docker after", 80)
        ),
        "make image": (
            package_progress("docker before", 80)
            + "\n#7 [internal] load build definition from Dockerfile\n"
            + "#8 ERROR: failed to solve: process exited\n"
            + package_progress("docker after", 80)
        ),
        "cd repo && bash -lc 'docker build .'": (
            package_progress("docker before", 80)
            + "\n#8 ERROR: failed to solve: nested shell process exited\n"
            + package_progress("docker after", 80)
        ),
        "cd /tmp && /usr/bin/docker build .": (
            package_progress("docker before", 80)
            + "\n#8 ERROR: failed to solve: chained path-qualified process exited\n"
            + package_progress("docker after", 80)
        ),
        "tar -cf - . | /usr/bin/docker build -": (
            package_progress("docker before", 80)
            + "\n#8 ERROR: failed to solve: piped path-qualified process exited\n"
            + package_progress("docker after", 80)
        ),
        "docker --log-level debug build .": (
            package_progress("docker before", 80)
            + "\n#8 ERROR: failed to solve: process exited\n"
            + package_progress("docker after", 80)
        ),
        "docker buildx --builder default build .": (
            package_progress("docker before", 80)
            + "\n#8 ERROR: failed to solve: process exited\n"
            + package_progress("docker after", 80)
        ),
        "docker compose --env-file .env build": (
            package_progress("docker before", 80)
            + "\n#8 ERROR: failed to solve: process exited\n"
            + package_progress("docker after", 80)
        ),
        "docker compose --parallel 4 build": (
            package_progress("docker before", 80)
            + "\n#8 ERROR: failed to solve: process exited\n"
            + package_progress("docker after", 80)
        ),
        "docker image build .": (
            package_progress("docker before", 80)
            + "\n#8 ERROR: failed to solve: process exited\n"
            + package_progress("docker after", 80)
        ),
        "docker builder build .": (
            package_progress("docker before", 80)
            + "\n#8 ERROR: failed to solve: process exited\n"
            + package_progress("docker after", 80)
        ),
        "docker --context prod compose --progress plain logs api": (
            package_progress("docker logs before", 80)
            + "\napi-1  | ERROR failed to connect to database\n"
            + package_progress("docker logs after", 80)
        ),
        "docker logs api": (
            "\n".join(f"ERROR stale startup noise {index}" for index in range(120))
            + "\n"
            + package_progress("docker runtime before", 80)
            + "\nTraceback (most recent call last):\nValueError: broken runtime state\n"
            + package_progress("docker runtime after", 80)
        ),
    }

    expected_signals = {
        "apt install imaginary-package": "Unable to locate package imaginary-package",
        "bash -lc 'apt install imaginary-package'": "Unable to locate package imaginary-package",
        "uv sync": "ResolutionImpossible",
        "sh -c 'uv sync'": "ResolutionImpossible",
        "python -m pip install missing-package": "No matching distribution found",
        "python -I -u -m pip install missing-package": "No matching distribution found",
        "uv run pip install missing-package": "No matching distribution found",
        "uv run python -I -m pip install missing-package": "No matching distribution found",
        "xargs pip install missing-package": "No matching distribution found",
        "find reqs -name '*.txt' | xargs -I {} pip install -r {}": "No matching distribution found",
        "xargs -a packages.txt pip install missing-package": "No matching distribution found",
        "xargs -t pip install missing-package": "No matching distribution found",
        "cd repo && bash -lc 'uv sync'": "ResolutionImpossible",
        "pytest -q": "FAILED tests/test_demo.py::test_signal",
        "docker build .": "failed to solve",
        "/usr/bin/docker build .": "failed to solve",
        "bash -lc 'docker build .'": "failed to solve",
        "bash -lc '/usr/bin/docker build .'": "failed to solve",
        "make image": "failed to solve",
        "cd repo && bash -lc 'docker build .'": "failed to solve",
        "cd /tmp && /usr/bin/docker build .": "failed to solve",
        "tar -cf - . | /usr/bin/docker build -": "failed to solve",
        "docker --log-level debug build .": "failed to solve",
        "docker buildx --builder default build .": "failed to solve",
        "docker compose --env-file .env build": "failed to solve",
        "docker compose --parallel 4 build": "failed to solve",
        "docker image build .": "failed to solve",
        "docker builder build .": "failed to solve",
        "docker --context prod compose --progress plain logs api": "failed to connect to database",
        "docker logs api": "ValueError: broken runtime state",
    }

    for command, raw in cases.items():
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=opts(max_chars=900),
        )

        assert result.changed is True, command
        assert_compacted(result.text, raw)
        assert expected_signals[command] in result.text
        assert "[noisegate: exit_code=1]" in result.text


def test_package_failures_outrank_warning_noise_even_under_tight_char_budget() -> None:
    raw = "\n".join(
        [
            *[f"ERROR: transient resolver noise {index}" for index in range(180)],
            *[f"resolver chatter {index}" for index in range(80)],
            "ResolutionImpossible: Because package-a conflicts with package-b",
            *[f"post resolver chatter {index}" for index in range(80)],
        ]
    )

    result = reduce_text(
        raw,
        command="uv sync",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=140, max_lines=6, head_lines=1, tail_lines=1),
    )

    assert result.changed is True
    assert "ResolutionImpossible" in result.text
    assert "conflicts with package-b" in result.text


def test_package_conflicts_outrank_verbose_generic_errors_under_default_char_budget() -> None:
    raw = "\n".join(
        [
            *[
                f"ERROR: compiling optional dependency chunk {index} " + ("x" * 90)
                for index in range(160)
            ],
            *[f"resolver chatter {index}" for index in range(80)],
            "ResolutionImpossible: Because package-a conflicts with package-b",
            *[f"post resolver chatter {index}" for index in range(80)],
        ]
    )

    result = reduce_text(
        raw,
        command="uv sync",
        tool_name="terminal",
        exit_code=1,
        options=NoisegateOptions(),
    )

    assert result.changed is True
    assert "ResolutionImpossible" in result.text
    assert "conflicts with package-b" in result.text


def test_tight_package_budget_preserves_exit_code_notice_with_priority_failure() -> None:
    raw = "\n".join(
        [
            *[f"ERROR: transient resolver noise {index}" for index in range(160)],
            "ResolutionImpossible: Because package-a conflicts with package-b",
            *[f"ERROR: trailing resolver noise {index}" for index in range(160)],
        ]
    )

    result = reduce_text(
        raw,
        command="uv sync",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=170, max_lines=5, head_lines=1, tail_lines=1),
    )

    assert result.changed is True
    assert "ResolutionImpossible" in result.text
    assert "[noisegate: exit_code=1]" in result.text
    assert len(result.text) <= 170

    tighter_result = reduce_text(
        raw.replace("transient resolver noise", "generic noisy line"),
        command="uv sync",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=97, max_lines=4, head_lines=1, tail_lines=1),
    )

    assert tighter_result.changed is True
    assert "ResolutionImpossible" in tighter_result.text
    assert "[noisegate: exit_code=1]" in tighter_result.text
    assert len(tighter_result.text) <= 97

    pytest_raw = "\n".join(
        [
            *[f"pytest noise before {index}" for index in range(80)],
            "FAILED tests/test_demo.py::test_signal",
            *[f"pytest noise after {index}" for index in range(80)],
        ]
    )

    pytest_result = reduce_text(
        pytest_raw,
        command="pytest -q",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=100, max_lines=4, head_lines=1, tail_lines=1),
    )

    assert pytest_result.changed is True
    assert "FAILED tests/test_demo.py::test_signal" in pytest_result.text
    assert "[noisegate: exit_code=1]" in pytest_result.text
    assert len(pytest_result.text) <= 100

    too_tight_result = reduce_text(
        raw.replace("transient resolver noise", "generic noisy line"),
        command="uv sync",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=85, max_lines=4, head_lines=1, tail_lines=1),
    )

    assert too_tight_result.changed is False
    assert "ResolutionImpossible" in too_tight_result.text


def test_docker_log_tracebacks_outrank_early_generic_errors_under_tight_budget() -> None:
    raw = "\n".join(
        [
            *[f"ERROR stale startup noise {index}" for index in range(180)],
            "Traceback (most recent call last):",
            "ValueError: broken runtime state",
            *[f"after {index}" for index in range(80)],
        ]
    )

    result = reduce_text(
        raw,
        command="docker logs api",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=180, max_lines=6, head_lines=1, tail_lines=1),
    )

    assert result.changed is True
    assert "Traceback (most recent call last)" in result.text
    assert "ValueError: broken runtime state" in result.text


def test_docker_log_tracebacks_outrank_repeated_not_found_noise() -> None:
    raw = "\n".join(
        [
            *[f"GET /missing/{index} 404 not found" for index in range(180)],
            "Traceback (most recent call last):",
            "RuntimeError: application crashed",
            *[f"after {index}" for index in range(80)],
        ]
    )

    result = reduce_text(
        raw,
        command="docker logs api",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=180, max_lines=6, head_lines=1, tail_lines=1),
    )

    assert result.changed is True
    assert "Traceback (most recent call last)" in result.text
    assert "RuntimeError: application crashed" in result.text


def test_generic_runtime_traceback_stays_visible_without_exit_code() -> None:
    raw = "\n".join(
        [
            *[f"startup noise {index}" for index in range(120)],
            "#8 ERROR: failed to solve: stale build text from previous log chunk",
            *[f"middle noise {index}" for index in range(120)],
            "Traceback (most recent call last):",
            "RuntimeError: actual runtime crash",
            *[f"after {index}" for index in range(80)],
        ]
    )

    result = reduce_text(
        raw,
        command="python app.py",
        tool_name="terminal",
        options=opts(max_chars=220, max_lines=8, head_lines=1, tail_lines=1),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == "generic"
    assert result.metadata["reducer"] == "generic_critical"
    assert "Traceback (most recent call last)" in result.text
    assert "RuntimeError: actual runtime crash" in result.text


def test_generic_runtime_traceback_not_misclassified_by_stale_docker_build_text() -> None:
    raw = "\n".join(
        [
            *[f"startup noise {index}" for index in range(120)],
            "#8 ERROR: failed to solve: stale build text from previous log chunk",
            *[f"middle noise {index}" for index in range(120)],
            "Traceback (most recent call last):",
            "RuntimeError: actual runtime crash",
            *[f"after {index}" for index in range(80)],
        ]
    )

    result = reduce_text(
        raw,
        command="python app.py",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=220, max_lines=8, head_lines=1, tail_lines=1),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == "generic"
    assert result.metadata["reducer"] == "generic_critical"
    assert "Traceback (most recent call last)" in result.text
    assert "RuntimeError: actual runtime crash" in result.text


def test_generic_critical_output_keeps_lcm_ref_and_failure_under_tight_budget() -> None:
    raw = "\n".join(
        [
            *[f"startup noise {index}" for index in range(120)],
            "[Externalized tool output: 8000 chars, ref=ng_runtime_ref]",
            *[f"middle noise {index}" for index in range(120)],
            "ERROR: No matching distribution found for private-runtime-package",
            *[f"after {index}" for index in range(80)],
        ]
    )

    result = reduce_text(
        raw,
        command="python app.py",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=300, max_lines=8, head_lines=1, tail_lines=1),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == "generic"
    assert result.metadata["reducer"] == "generic_critical"
    assert "ref=ng_runtime_ref" in result.text
    assert "No matching distribution found for private-runtime-package" in result.text


def test_docker_build_summary_outranks_early_generic_error_noise() -> None:
    raw = "\n".join(
        [
            *[
                f"ERROR package metadata not found in cache {index} " + ("x" * 40)
                for index in range(100)
            ],
            "ERROR: No matching distribution found for missing-package",
            *[f"more build chatter {index}" for index in range(80)],
            "#8 ERROR: failed to solve: process \"/bin/sh -c false\" did not complete successfully",
            *[f"post build chatter {index}" for index in range(80)],
        ]
    )

    for command in ("docker build .", "make image", "just docker-build"):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=opts(max_chars=220, max_lines=6, head_lines=1, tail_lines=1),
        )

        assert result.changed is True, command
        assert result.metadata["command_class"] == "docker_build", command
        assert "failed to solve" in result.text, command
        assert "did not complete successfully" in result.text, command

        char_budgeted = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=opts(max_chars=220, max_lines=200, head_lines=1, tail_lines=1),
        )

        assert char_budgeted.changed is True, command
        assert char_budgeted.metadata["command_class"] == "docker_build", command
        assert "failed to solve" in char_budgeted.text, command
        assert "did not complete successfully" in char_budgeted.text, command


def test_docker_build_keeps_traceback_root_cause_under_tight_budget() -> None:
    raw = "\n".join(
        [
            *[f"#1 build progress {index}" for index in range(80)],
            "Traceback (most recent call last):",
            "ValueError: build script root cause",
            *[f"#2 build progress {index}" for index in range(80)],
            '#8 ERROR: failed to solve: process "/bin/sh -c python build.py" '
            "did not complete successfully",
            *[f"#9 build progress {index}" for index in range(80)],
        ]
    )

    result = reduce_text(
        raw,
        command="docker build .",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=500, max_lines=8, head_lines=1, tail_lines=1),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == "docker_build"
    assert "Traceback (most recent call last)" in result.text
    assert "ValueError: build script root cause" in result.text


def test_non_build_reducers_do_not_let_stale_build_lines_outrank_tracebacks() -> None:
    raw = "\n".join(
        [
            "ERROR: failed to solve: stale build text from previous startup",
            *[f"stale noise {index}" for index in range(180)],
            "Traceback (most recent call last):",
            "ValueError: broken runtime state",
            *[f"after {index}" for index in range(80)],
        ]
    )

    for command in ("docker logs api", "pytest -q"):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=opts(max_chars=180, max_lines=6, head_lines=1, tail_lines=1),
        )

        assert result.changed is True
        assert "Traceback (most recent call last)" in result.text
        assert "ValueError: broken runtime state" in result.text


def test_path_qualified_pipeline_command_start_uses_downstream_command_class() -> None:
    raw = "\n".join(
        [
            *[f"ERROR package metadata not found in cache {index}" for index in range(100)],
            "ERROR: No matching distribution found for missing-package",
            *[f"more build chatter {index}" for index in range(80)],
            "#8 ERROR: failed to solve: process \"/bin/sh -c false\" did not complete successfully",
            *[f"post build chatter {index}" for index in range(80)],
        ]
    )

    result = reduce_text(
        raw,
        command="tar -cf - . | /usr/bin/docker build -",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=220, max_lines=6, head_lines=1, tail_lines=1),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == "docker_build"
    assert "failed to solve" in result.text
    assert "did not complete successfully" in result.text


def test_chained_preflight_commands_continue_to_later_compactable_intent() -> None:
    package_raw = "\n".join(
        [
            *[f"apt list noise {index}" for index in range(120)],
            "E: Unable to locate package imaginary-package",
            *[f"apt install after {index}" for index in range(80)],
        ]
    )
    package_result = reduce_text(
        package_raw,
        command="apt list imaginary-package && apt install imaginary-package",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=220, max_lines=6, head_lines=1, tail_lines=1),
    )

    assert package_result.changed is True
    assert package_result.metadata["command_class"] == "apt"
    assert "Unable to locate package imaginary-package" in package_result.text

    build_raw = "\n".join(
        [
            *[f"compose ps noise {index}" for index in range(120)],
            "#8 ERROR: failed to solve: process \"/bin/sh -c false\" did not complete successfully",
            *[f"compose build after {index}" for index in range(80)],
        ]
    )
    build_result = reduce_text(
        build_raw,
        command="docker compose ps && docker compose build",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=220, max_lines=6, head_lines=1, tail_lines=1),
    )

    assert build_result.changed is True
    assert build_result.metadata["command_class"] == "docker_build"
    assert "failed to solve" in build_result.text
    assert "did not complete successfully" in build_result.text

    log_raw = "\n".join(
        [
            *[f"compose config noise {index}" for index in range(120)],
            "Traceback (most recent call last):",
            "RuntimeError: api crashed",
            *[f"compose logs after {index}" for index in range(80)],
        ]
    )
    log_result = reduce_text(
        log_raw,
        command="docker compose config && docker compose logs api",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=220, max_lines=6, head_lines=1, tail_lines=1),
    )

    assert log_result.changed is True
    assert log_result.metadata["command_class"] == "docker_logs"
    assert "Traceback (most recent call last)" in log_result.text
    assert "RuntimeError: api crashed" in log_result.text


def test_runner_wrapped_compactable_commands_keep_their_specific_reducers() -> None:
    package_raw = "\n".join(
        [
            *[f"package manager chatter {index}" for index in range(90)],
            "ERROR: No matching distribution found for missing-package",
            *[f"more package manager chatter {index}" for index in range(90)],
        ]
    )
    build_raw = "\n".join(
        [
            *[f"build chatter {index}" for index in range(90)],
            "#8 ERROR: failed to solve: process \"/bin/sh -c false\" did not complete successfully",
            *[f"more build chatter {index}" for index in range(90)],
        ]
    )
    node_raw = "\n".join(
        [
            *[f"npm install chatter {index}" for index in range(90)],
            "npm ERR! code ERESOLVE",
            *[f"more npm install chatter {index}" for index in range(90)],
        ]
    )

    cases = (
        (
            "uv run pip install missing-package",
            package_raw,
            "python_package",
            "No matching distribution found",
        ),
        (
            "poetry run pip install missing-package",
            package_raw,
            "python_package",
            "No matching distribution found",
        ),
        ("uv run docker build .", build_raw, "docker_build", "failed to solve"),
        ("uv run npm install", node_raw, "node", "ERESOLVE"),
    )

    for command, raw, expected_class, signal in cases:
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=opts(max_chars=260, max_lines=8, head_lines=1, tail_lines=1),
        )

        assert result.changed is True, command
        assert result.metadata["command_class"] == expected_class, command
        assert signal in result.text, command


def test_node_reducer_keeps_late_js_exception_under_tight_budget() -> None:
    raw = "\n".join(
        [
            *[f"npm ERR! lifecycle noise before {index}" for index in range(120)],
            "TypeError: Cannot read properties of undefined (reading 'value')",
            "ReferenceError: missingValue is not defined",
            *[f"npm ERR! lifecycle noise after {index}" for index in range(120)],
        ]
    )

    result = reduce_text(
        raw,
        command="npm test",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=260, max_lines=8, head_lines=1, tail_lines=1),
    )

    assert result.changed is True
    assert result.metadata["command_class"] == "node"
    assert "TypeError: Cannot read properties" in result.text
    assert "ReferenceError: missingValue" in result.text


def test_node_reducer_keeps_late_plain_error_root_cause_under_tight_budget() -> None:
    raw = "\n".join(
        [
            *[f"npm ERR! lifecycle noise before {index}" for index in range(120)],
            "Error: Cannot find module 'left-pad'",
            "    at Object.<anonymous> (app.js:1:1)",
            *[f"npm ERR! lifecycle noise after {index}" for index in range(120)],
        ]
    )

    for command in (
        "npm test",
        "cat package.json && npm test",
        "cat package.json; npm test",
        "bash -lc 'cat package.json && npm test'",
    ):
        for max_chars, max_lines in ((220, 3), (220, 4), (220, 5), (260, 8)):
            result = reduce_text(
                raw,
                command=command,
                tool_name="terminal",
                exit_code=1,
                options=opts(max_chars=max_chars, max_lines=max_lines, head_lines=1, tail_lines=1),
            )

            case = f"{command} max_chars={max_chars} max_lines={max_lines}"
            assert result.changed is True, case
            assert result.metadata["command_class"] == "node", case
            assert "Error: Cannot find module 'left-pad'" in result.text, case
            assert "[noisegate: exit_code=1]" in result.text, case


def test_path_qualified_command_mentions_in_arguments_do_not_drive_classification() -> None:
    raw = package_progress("ordinary output")
    cases = {
        "echo /usr/bin/docker build .": "generic",
        "echo /usr/bin/git grep target": "generic",
        "echo /usr/bin/xargs rg target": "generic",
        "printf %s /usr/bin/apt-get update": "generic",
        "python script.py /usr/bin/pip install pkg": "generic",
        "node tool.js /usr/bin/docker build .": "node",
        "echo /usr/bin/npm install": "generic",
    }

    for command, expected_class in cases.items():
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            options=opts(max_chars=180, max_lines=8),
        )

        assert result.metadata["command_class"] == expected_class, command


def test_artifact_notice_path_keeps_package_failure_line(tmp_path: Path) -> None:
    raw_stdout = "\n".join(
        [
            *[f"apt noise before {index} " + ("x" * 60) for index in range(60)],
            "E: Unable to locate package imaginary-package",
            *[f"apt noise after {index} " + ("z" * 60) for index in range(60)],
        ]
    )
    payload = json.dumps(
        {"stdout": raw_stdout, "command": "apt install imaginary-package", "exit_code": 1}
    )

    transformed = transform_tool_result(
        payload,
        tool_name="terminal",
        noisegate_max_chars=220,
        noisegate_max_lines=10,
        noisegate_artifacts=True,
        noisegate_artifact_dir=str(tmp_path / "artifacts"),
    )

    assert transformed is not None
    stdout = json.loads(transformed)["stdout"]
    assert "E: Unable to locate package imaginary-package" in stdout


def test_log_filter_pipelines_still_use_the_original_slop_classifier() -> None:
    raw = "\n".join(
        [
            *[f"apt noise before {index}" for index in range(80)],
            "E: Unable to locate package imaginary-package",
            *[f"apt noise after {index}" for index in range(80)],
        ]
    )

    for command in (
        "apt install imaginary-package | grep ERROR",
        "apt install imaginary-package | head -100",
    ):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=opts(max_chars=500, max_lines=10),
        )

        assert result.changed is True
        assert result.metadata["command_class"] == "apt"
        assert "Unable to locate package imaginary-package" in result.text

    for command, expected_class, signal in (
        ("pytest -q | cat", "pytest", "FAILED tests/test_demo.py::test_signal"),
        ("docker build . | sed -n '1,120p'", "docker_build", "failed to solve"),
    ):
        signal_raw = "\n".join(
            [
                *[f"noise before {index}" for index in range(80)],
                signal,
                *[f"noise after {index}" for index in range(80)],
            ]
        )
        result = reduce_text(
            signal_raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=opts(max_chars=500, max_lines=10),
        )

        assert result.changed is True
        assert result.metadata["command_class"] == expected_class
        assert signal in result.text


def test_background_jobs_use_later_compactable_command_class() -> None:
    raw = "\n".join(
        [
            *[f"pytest noise before {index}" for index in range(80)],
            "FAILED tests/test_demo.py::test_signal",
            *[f"pytest noise after {index}" for index in range(80)],
        ]
    )

    for command in (
        "cat file.py & pytest -q",
        "rg target src & pytest -q",
        "cat file.py & bash -lc 'pytest -q'",
        "rg target src & bash -lc 'pytest -q'",
        "bash -lc 'pytest -q'",
        "sh -c 'pytest -q'",
        "python -m pytest -q",
        "python3 -m pytest -q",
        "uv run python -m pytest -q",
        ".venv/bin/pytest -q",
        "cat file.py | pytest -q",
        "{ cat file.py; } | pytest -q",
        "rg -l test tests | pytest -q",
        "pytest -q || cat file.py",
    ):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=opts(max_chars=500, max_lines=10),
        )

        assert result.changed is True
        assert result.metadata["command_class"] == "pytest"
        assert "FAILED tests/test_demo.py::test_signal" in result.text


def test_pytest_arguments_do_not_override_primary_command_intent() -> None:
    raw = "pytest is mentioned as an argument, not executed"

    for command, expected_class in (
        ("echo pytest", "generic"),
        ("python script.py pytest", "generic"),
        ("node tool.js pytest", "node"),
    ):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=opts(max_chars=20, max_lines=2),
        )

        assert result.metadata["command_class"] == expected_class, command


def test_reverse_background_exact_tail_stays_byte_for_byte_unchanged() -> None:
    raw_source = "\n".join(f"def function_{index}(): return {index}" for index in range(160))
    search_output = "\n".join(
        f"src/module_{index}.py:{index}:def target_{index}():" for index in range(180)
    )

    for command, raw, expected_class, expected_changed in (
        ("pytest -q & cat file.py", raw_source, "file_read", False),
        ("pytest -q & rg target src", search_output, "source_search", False),
        ("pytest -q & cat file.py; pytest -q", raw_source, "pytest", True),
        ("pytest -q & rg target src; pytest -q", search_output, "pytest", True),
        ("true & cat <(pytest -q)", raw_source, "pytest", True),
    ):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=opts(max_chars=400),
        )

        assert result.changed is expected_changed
        if not expected_changed:
            assert result.text == raw
        assert result.metadata["command_class"] == expected_class


def test_background_exact_tail_after_compactable_job_stays_byte_for_byte_unchanged() -> None:
    raw_source = "\n".join(f"def function_{index}(): return {index}" for index in range(160))

    for command in (
        "true & pytest -q & cat b.py",
        "cat a.py & pytest -q & cat b.py",
        "true & pytest -q & bash -lc 'cat b.py'",
    ):
        result = reduce_text(
            raw_source,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=opts(max_chars=400),
        )

        assert result.changed is False, command
        assert result.text == raw_source, command
        assert result.metadata["command_class"] == "file_read", command


def test_source_search_feeding_xargs_uses_consumer_command_class() -> None:
    pytest_raw = "\n".join(
        [
            *[f"pytest noise before {index}" for index in range(80)],
            "FAILED tests/test_demo.py::test_signal",
            *[f"pytest noise after {index}" for index in range(80)],
        ]
    )
    package_raw = "\n".join(
        [
            *[f"package noise before {index}" for index in range(80)],
            "ERROR: No matching distribution found for missing-package",
            *[f"package noise after {index}" for index in range(80)],
        ]
    )

    for command, raw, expected_class, signal in (
        (
            "rg -l test tests | xargs pytest -q",
            pytest_raw,
            "pytest",
            "FAILED tests/test_demo.py::test_signal",
        ),
        (
            "{ rg -l test tests; } | xargs pytest -q",
            pytest_raw,
            "pytest",
            "FAILED tests/test_demo.py::test_signal",
        ),
        (
            "find src -name '*.py' -exec rg target {} + | xargs pytest -q",
            pytest_raw,
            "pytest",
            "FAILED tests/test_demo.py::test_signal",
        ),
        (
            "rg -l test tests | xargs bash -lc 'pytest -q'",
            pytest_raw,
            "pytest",
            "FAILED tests/test_demo.py::test_signal",
        ),
        (
            "bash -lc 'rg -l test tests | xargs pytest -q'",
            pytest_raw,
            "pytest",
            "FAILED tests/test_demo.py::test_signal",
        ),
        (
            "rg -l reqs requirements | xargs pip install missing-package",
            package_raw,
            "python_package",
            "No matching distribution found for missing-package",
        ),
    ):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=opts(max_chars=500, max_lines=10),
        )

        assert result.changed is True
        assert result.metadata["command_class"] == expected_class
        assert signal in result.text
    swallowed_exit = reduce_text(
        pytest_raw,
        command="find tests -name '*.py' -print0 | xargs -0 -r pytest -q || true",
        tool_name="terminal",
        exit_code=0,
        options=opts(max_chars=500, max_lines=10),
    )

    assert swallowed_exit.changed is True
    assert swallowed_exit.metadata["command_class"] == "pytest"
    assert "FAILED tests/test_demo.py::test_signal" in swallowed_exit.text


def test_later_chained_compactable_commands_override_exact_passthrough() -> None:
    pytest_raw = "\n".join(
        [
            *[f"pytest noise before {index}" for index in range(80)],
            "FAILED tests/test_demo.py::test_signal",
            *[f"pytest noise after {index}" for index in range(80)],
        ]
    )
    node_raw = "\n".join(
        [
            *[f"npm noise before {index}" for index in range(80)],
            "npm ERR! code ERESOLVE",
            *[f"npm noise after {index}" for index in range(80)],
        ]
    )
    realistic_pytest_with_frame = "\n".join(
        [
            "=================================== FAILURES ===================================",
            "____________________________ test_writes_config ____________________________",
            "E       AssertionError: expected private mode",
            "tests/test_artifacts.py:37: AssertionError",
            *[f"pytest noise after {index}" for index in range(80)],
            "FAILED tests/test_artifacts.py::test_writes_config - AssertionError",
        ]
    )

    for command, raw, expected_class, signal in (
        (
            "rg -l test tests && pytest -q",
            pytest_raw,
            "pytest",
            "FAILED tests/test_demo.py::test_signal",
        ),
        (
            "rg -h target src && pytest -q",
            pytest_raw,
            "pytest",
            "FAILED tests/test_demo.py::test_signal",
        ),
        (
            "grep -h target src && pytest -q",
            pytest_raw,
            "pytest",
            "FAILED tests/test_demo.py::test_signal",
        ),
        (
            "rg target src && pytest -q",
            realistic_pytest_with_frame,
            "pytest",
            "FAILED tests/test_artifacts.py::test_writes_config",
        ),
        ("rg target src; npm install", node_raw, "node", "npm ERR! code ERESOLVE"),
        (
            "bash -lc 'rg -l test tests && pytest -q'",
            pytest_raw,
            "pytest",
            "FAILED tests/test_demo.py::test_signal",
        ),
    ):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=opts(max_chars=500, max_lines=10),
        )

        assert result.changed is True, command
        assert result.metadata["command_class"] == expected_class, command
        assert signal in result.text, command


def test_short_circuit_semantics_do_not_override_exact_reads_blindly() -> None:
    raw_source = "\n".join(f"def function_{index}(): return {index}" for index in range(160))
    pytest_raw = "\n".join(
        [
            *[f"pytest noise before {index}" for index in range(80)],
            "FAILED tests/test_demo.py::test_signal",
            *[f"pytest noise after {index}" for index in range(80)],
        ]
    )

    successful_cat = reduce_text(
        raw_source,
        command="cat file.py || pytest -q",
        tool_name="terminal",
        exit_code=0,
        options=opts(max_chars=400),
    )

    assert successful_cat.changed is False
    assert successful_cat.text == raw_source
    assert successful_cat.metadata["command_class"] == "file_read"

    source_with_test_words = "\n".join(
        [
            "FAILED tests/test_demo.py::test_signal literal in source",
            "const message = 'npm ERR! code ERESOLVE literal in source';",
            *[f"export const value_{index} = {index};" for index in range(120)],
        ]
    )
    plain_text_fixture_with_failure_words = "\n".join(
        [
            "Traceback (most recent call last):",
            "npm ERR! code ERESOLVE literal in fixture",
            "FAILED tests/test_demo.py::test_signal literal in fixture",
            *[f"literal fixture prose line {index:03d} ERROR failed" for index in range(80)],
        ]
    )
    readme_package_literal = "\n".join(
        [
            "# Notes",
            "ERROR: No matching distribution found for missing-package",
            *[f"literal prose failed line {index}" for index in range(100)],
        ]
    )
    readme_pytest_literal = "\n".join(
        [
            "# Notes",
            "Traceback (most recent call last):",
            '  File "tests/test_demo.py", line 3, in test_signal',
            "ModuleNotFoundError: No module named missing_package",
            "FAILED tests/test_demo.py::test_signal",
            *[f"literal prose line {index}" for index in range(100)],
        ]
    )
    for command, raw, exit_code in (
        ("pytest -q || cat file.py", source_with_test_words, 0),
        ("pytest -q || cat file.py", source_with_test_words, None),
        ("cat file.py && pytest -q", source_with_test_words, 0),
        ("cat file.py && pytest -q > pytest.log", source_with_test_words, 1),
        ("cat file.py; uv sync > uv.log", source_with_test_words, 1),
        ("cat README.md && uv sync", readme_package_literal, 1),
        ("pytest -q && cat README.md", readme_pytest_literal, None),
        ("bash -lc 'cat file.py && pytest -q > pytest.log'", source_with_test_words, 1),
        ("cat fixture.txt && pytest -q", plain_text_fixture_with_failure_words, 1),
        ("cat fixture.txt && pytest -q > pytest.log", plain_text_fixture_with_failure_words, 1),
        ("cat fixture.txt; pytest -q", plain_text_fixture_with_failure_words, 1),
        ("pytest -q && cat fixture.txt", plain_text_fixture_with_failure_words, None),
        ("bash -lc 'cat fixture.txt && pytest -q'", plain_text_fixture_with_failure_words, 1),
        ("pytest -q || cat fixture.txt", plain_text_fixture_with_failure_words, 0),
    ):
        literal_exact = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=exit_code,
            options=opts(max_chars=400),
        )

        assert literal_exact.changed is False, command
        assert literal_exact.text == raw, command
        assert literal_exact.metadata["command_class"] == "file_read", command

    failed_pytest = reduce_text(
        pytest_raw,
        command="pytest -q && cat file.py",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=500, max_lines=10),
    )

    assert failed_pytest.changed is True
    assert failed_pytest.metadata["command_class"] == "pytest"
    assert "FAILED tests/test_demo.py::test_signal" in failed_pytest.text

    failed_pytest_wrapped = reduce_text(
        pytest_raw,
        command="bash -lc 'pytest -q && cat file.py'",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=500, max_lines=10),
    )

    assert failed_pytest_wrapped.changed is True
    assert failed_pytest_wrapped.metadata["command_class"] == "pytest"
    assert "FAILED tests/test_demo.py::test_signal" in failed_pytest_wrapped.text

    failed_pytest_after_cat = reduce_text(
        pytest_raw,
        command="cat file.py && pytest -q",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=500, max_lines=10),
    )

    assert failed_pytest_after_cat.changed is True
    assert failed_pytest_after_cat.metadata["command_class"] == "pytest"
    assert "FAILED tests/test_demo.py::test_signal" in failed_pytest_after_cat.text

    failed_pytest_after_cat_semicolon = reduce_text(
        pytest_raw,
        command="cat file.py; pytest -q",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=500, max_lines=10),
    )

    assert failed_pytest_after_cat_semicolon.changed is True
    assert failed_pytest_after_cat_semicolon.metadata["command_class"] == "pytest"
    assert "FAILED tests/test_demo.py::test_signal" in failed_pytest_after_cat_semicolon.text

    failed_pytest_after_wrapped_cat = reduce_text(
        pytest_raw,
        command="bash -lc 'cat file.py && pytest -q'",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=500, max_lines=10),
    )

    assert failed_pytest_after_wrapped_cat.changed is True
    assert failed_pytest_after_wrapped_cat.metadata["command_class"] == "pytest"
    assert "FAILED tests/test_demo.py::test_signal" in failed_pytest_after_wrapped_cat.text

    for command, exit_code in (
        ("pytest -q || cat failure.log", 0),
        ("pytest -q || cat failure.log", None),
        ("pytest -q || rg FAILED .", 0),
        ("pytest -q || rg FAILED .", None),
        ("bash -lc 'pytest -q || cat failure.log'", 0),
    ):
        successful_pytest_with_unrun_fallback = reduce_text(
            pytest_raw,
            command=command,
            tool_name="terminal",
            exit_code=exit_code,
            options=opts(max_chars=500, max_lines=10),
        )

        assert successful_pytest_with_unrun_fallback.changed is True, command
        assert successful_pytest_with_unrun_fallback.metadata["command_class"] == "pytest", command
        assert (
            "FAILED tests/test_demo.py::test_signal"
            in successful_pytest_with_unrun_fallback.text
        )

    realistic_pytest_raw = "\n".join(
        [
            *[f"pytest noise before {index}" for index in range(80)],
            "Traceback (most recent call last):",
            "  File \"tests/test_demo.py\", line 3, in test_signal",
            "    import missing_package",
            "ModuleNotFoundError: No module named 'missing_package'",
            "FAILED tests/test_demo.py::test_signal",
            *[f"pytest noise after {index}" for index in range(80)],
        ]
    )
    for command, exit_code in (
        ("pytest -q || cat failure.log", 0),
        ("pytest -q || cat failure.log", None),
        ("bash -lc 'pytest -q || cat failure.log'", 0),
        ("cat file.py; pytest -q", None),
        ("pytest -q && cat file.py", None),
        ("pytest -q && rg FAILED .", None),
        ("bash -lc 'pytest -q && cat file.py'", None),
        ("cat file.py; pytest -q || true", 0),
        ("bash -lc 'cat file.py; pytest -q || true'", 0),
    ):
        compacted = reduce_text(
            realistic_pytest_raw,
            command=command,
            tool_name="terminal",
            exit_code=exit_code,
            options=opts(max_chars=800, max_lines=20),
        )

        assert compacted.changed is True, command
        assert compacted.metadata["command_class"] == "pytest", command
        assert "FAILED tests/test_demo.py::test_signal" in compacted.text

    source_with_failure_words = "\n".join(
        [
            "def test_name():",
            "    return 'FAILED tests/test_demo.py::test_signal npm ERR!'",
            *[f"def function_{index}(): return {index}" for index in range(80)],
        ]
    )
    successful_exact_then_pytest = reduce_text(
        source_with_failure_words,
        command="cat file.py && pytest -q",
        tool_name="terminal",
        exit_code=0,
        options=opts(max_chars=400),
    )

    assert successful_exact_then_pytest.changed is False
    assert successful_exact_then_pytest.text == source_with_failure_words
    assert successful_exact_then_pytest.metadata["command_class"] == "file_read"

    extensionless_search_hits = "\n".join(
        [
            "Makefile:10:FAILED tests/test_demo.py::test_signal",
            "Dockerfile:5:npm ERR! code ERESOLVE",
            *[f"src/package_{index}/BUILD:{index}:exact search hit" for index in range(80)],
        ]
    )
    exact_search_then_pytest = reduce_text(
        extensionless_search_hits,
        command="rg FAILED . && pytest -q",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=400),
    )

    assert exact_search_then_pytest.changed is False
    assert exact_search_then_pytest.text == extensionless_search_hits
    assert exact_search_then_pytest.metadata["command_class"] == "source_search"

    rg_json_with_failure_words = "\n".join(
        [
            '{"type":"match","data":{"path":{"text":"src/app.py"},'
            '"lines":{"text":"Traceback (most recent call last):\\n"}}}',
            '{"type":"match","data":{"path":{"text":"src/app.py"},'
            '"lines":{"text":"FAILED tests/test_demo.py::test_signal npm ERR!\\n"}}}',
            *[
                '{"type":"match","data":{"path":{"text":"src/file_'
                f'{index}.py"}},"lines":{{"text":"literal ERROR failed {index}\\n"}}}}}}'
                for index in range(80)
            ],
        ]
    )
    for command in (
        "rg --json ERROR src && pytest -q",
        "rg --json ERROR src; pytest -q",
        "bash -lc 'rg --json ERROR src && pytest -q'",
    ):
        exact_json_search = reduce_text(
            rg_json_with_failure_words,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=opts(max_chars=400),
        )

        assert exact_json_search.changed is False, command
        assert exact_json_search.text == rg_json_with_failure_words, command
        assert exact_json_search.metadata["command_class"] == "source_search", command

    structured_search_with_literal_error = "\n".join(
        [
            "src/app.py",
            "ERROR: literal source string, not uv sync output",
            *[f"src/module_{index}.py:{index}:exact search hit" for index in range(80)],
        ]
    )
    exact_search_then_package_command = reduce_text(
        structured_search_with_literal_error,
        command="rg --heading --no-line-number ERROR src && uv sync",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=400),
    )

    assert exact_search_then_package_command.changed is False
    assert exact_search_then_package_command.text == structured_search_with_literal_error
    assert exact_search_then_package_command.metadata["command_class"] == "source_search"

    single_file_search_without_filename = "\n".join(
        [
            "Traceback (most recent call last):",
            '  File "tests/test_demo.py", line 3, in test_signal',
            "ModuleNotFoundError: No module named missing_package",
            "FAILED tests/test_demo.py::test_signal",
            *[f"literal source ERROR failed {index}" for index in range(80)],
        ]
    )
    for command in (
        "rg Traceback src/app.py && pytest -q",
        "rg -i Traceback src/app.py && pytest -q",
        "rg -e Traceback src/app.py && pytest -q",
        "rg --regexp Traceback src/app.py && pytest -q",
        "grep -e Traceback src/app.py && pytest -q",
        "grep -eTraceback src/app.py && pytest -q",
        "rg --glob=*.py Traceback src/app.py && pytest -q",
    ):
        exact_single_file_search_then_pytest = reduce_text(
            single_file_search_without_filename,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=opts(max_chars=300),
        )

        assert exact_single_file_search_then_pytest.changed is False, command
        assert exact_single_file_search_then_pytest.text == single_file_search_without_filename, (
            command
        )
        assert exact_single_file_search_then_pytest.metadata["command_class"] == "source_search", (
            command
        )


def test_exact_pipeline_consumers_do_not_infer_node_from_literal_source_output() -> None:
    source_with_literal_node_error = "\n".join(
        [
            "const message = 'npm ERR! literal source text';",
            *[f"def function_{index}(): return {index}" for index in range(100)],
        ]
    )
    search_with_literal_node_error = "\n".join(
        [
            "src/app.py:1:npm ERR! literal source text",
            *[f"src/file_{index}.py:{index}:def target(): pass" for index in range(100)],
        ]
    )

    for command, raw, expected_class in (
        ("cat file.py | head -20", source_with_literal_node_error, "file_read"),
        ("rg target src | head -20", search_with_literal_node_error, "source_search"),
        ("find src -name '*.py' | xargs -r cat", source_with_literal_node_error, "file_read"),
        (
            "find src -name '*.py' | xargs -r rg target",
            search_with_literal_node_error,
            "source_search",
        ),
    ):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            options=opts(max_chars=400),
        )

        assert result.changed is False, command
        assert result.text == raw, command
        assert result.metadata["command_class"] == expected_class, command


def test_source_and_exact_terminal_commands_stay_byte_for_byte_unchanged() -> None:
    raw_source = "\n".join(f"def function_{index}(): return {index}" for index in range(160))
    diff = "\n".join(
        [
            "diff --git a/app.py b/app.py",
            "--- a/app.py",
            "+++ b/app.py",
            "@@ -1,2 +1,2 @@",
            "-old",
            "+new",
            *[f"+exact diff line {index}" for index in range(120)],
        ]
    )
    search_output = "\n".join(
        f"src/module_{index}.py:{index}:def target_{index}():" for index in range(180)
    )

    cases = {
        "cat file.py": raw_source,
        "cat < file.py": raw_source,
        "cat <file.py": raw_source,
        "cat file.py 2>/dev/null": raw_source,
        "cat file.py 2> /dev/null": raw_source,
        "/bin/cat file.py": raw_source,
        "sed -n '1,200p' file.py": raw_source,
        "sed 's/foo/bar/' file.py": raw_source,
        "head -200 file.py": raw_source,
        "/usr/bin/head -100 file.py": raw_source,
        "tail -200 file.py": raw_source,
        "nl -ba file.py": raw_source,
        "jq . data.json": raw_source,
        "yq . config.yaml": raw_source,
        "cat file.py | head -100": raw_source,
        "env -i cat file.py": raw_source,
        "sudo -H cat file.py": raw_source,
        "sudo LC_ALL=C cat file.py": raw_source,
        "time -p cat file.py": raw_source,
        "time -o timing.log cat file.py": raw_source,
        "time --output timing.log cat file.py": raw_source,
        "/usr/bin/time -o timing.log cat file.py": raw_source,
        "/usr/bin/time -o timing.log /bin/cat file.py": raw_source,
        "gtime -o timing.log cat file.py": raw_source,
        "bash -lc 'cat file.py'": raw_source,
        "bash --noprofile --norc -c 'cat file.py'": raw_source,
        "bash --noprofile --norc -lc 'cd repo && cat file.py'": raw_source,
        "bash -lc 'set -o pipefail; cat file.py'": raw_source,
        "bash -lc 'cd repo && cat file.py'": raw_source,
        "bash -lc 'source .venv/bin/activate && cat file.py'": raw_source,
        "bash -lc '. .venv/bin/activate && rg target src'": search_output,
        "bash -lc 'pwd && cat file.py'": raw_source,
        "echo heading && cat file.py": raw_source,
        "make noop; cat file.py": raw_source,
        "bash -lc 'echo heading && cat file.py'": raw_source,
        "bash -lc 'pytest -q' && cat file.py": raw_source,
        "cat file.py; docker ps": raw_source,
        "pytest -q && cat file.py": raw_source,
        "apt install jq && cat file.py": raw_source,
        "docker build .; cat Dockerfile": raw_source,
        "python -m pip install x && sed -n '1,200p' file.py": raw_source,
        "bash -lc 'true && rg target src'": search_output,
        "sh -c 'head -100 file.py'": raw_source,
        "zsh -lc 'tail -100 file.py'": raw_source,
        "git diff -- app.py": diff,
        "git diff -- app.py & pytest -q": diff,
        "git diff -- app.py & npm install": diff,
        "rg target src": search_output,
        "/usr/local/bin/rg target src": search_output,
        "grep -R target src": search_output,
        "/opt/homebrew/bin/grep -R target src": search_output,
        "rg 'apt install' src": search_output,
        "grep -R 'docker build' docs": search_output,
        "env LC_ALL=C rg target src": search_output,
        "env -i rg target src": search_output,
        "env -u GREP_OPTIONS rg target src": search_output,
        "sudo rg target src": search_output,
        "sudo -H rg target src": search_output,
        "sudo LC_ALL=C rg target src": search_output,
        "time rg target src": search_output,
        "time -p rg target src": search_output,
        "time -o timing.log rg target src": search_output,
        "time --output timing.log rg target src": search_output,
        "/usr/bin/time --output timing.log rg target src": search_output,
        "/usr/bin/time --output timing.log /usr/local/bin/rg target src": search_output,
        "gtime -o timing.log rg target src": search_output,
        "command rg target src": search_output,
        "command -p rg target src": search_output,
        "git grep target": search_output,
        "git -c color.grep=false grep target": search_output,
        "git -C repo grep target": search_output,
        "git --no-pager grep target": search_output,
        "rg target src; docker ps": search_output,
        "bash -lc 'npm install' && rg target src": search_output,
        "bash -lc 'rg target src'": search_output,
        "bash --noprofile --norc -c 'rg target src'": search_output,
        "bash --noprofile --norc -lc 'cd repo && rg target src'": search_output,
        "bash -lc 'cd repo && rg target src'": search_output,
        "uv run rg target src": search_output,
        "uv run --extra test rg target src": search_output,
        "uv run --group dev rg target src": search_output,
        "uv run --no-group docs rg target src": search_output,
        "uv run --only-group dev rg target src": search_output,
        "uv run --python-platform x86_64-manylinux2014 rg target src": search_output,
        "uv run --with ripgrep rg target src": search_output,
        "uv run --with-editable . rg target src": search_output,
        "uv run --with-requirements requirements.txt rg target src": search_output,
        "uvx --from ripgrep rg target src": search_output,
        "poetry run rg target src": search_output,
        "pipx run rg target src": search_output,
        "pipx run --spec ripgrep rg target src": search_output,
        "npx rg target src": search_output,
        "npx -c 'rg target src'": search_output,
        "npx --call 'rg target src'": search_output,
        "npm exec rg target src": search_output,
        "npm exec -c 'rg target src'": search_output,
        "npm exec --call 'rg target src'": search_output,
        "npm exec -w app rg target src": search_output,
        "npm exec --workspace app rg target src": search_output,
        "npm --workspace app exec rg target src": search_output,
        "npm --prefix frontend exec rg target src": search_output,
        "pnpm exec rg target src": search_output,
        "pnpm exec --dir app rg target src": search_output,
        "pnpm --filter app exec rg target src": search_output,
        "export LC_ALL=C && rg target src": search_output,
        "echo heading && rg target src": search_output,
        "make noop; grep -R target src": search_output,
        "xargs -I {} rg target": search_output,
        "find src -name '*.py' -exec rg target {} +": search_output,
        "env LC_ALL=C find src -name '*.py' -exec rg target {} +": search_output,
        "sudo find src -name '*.py' -exec rg target {} +": search_output,
        "time find src -name '*.py' -exec rg target {} +": search_output,
        "find src -name '*.py' | xargs rg target": search_output,
        "find src -name '*.py' | xargs cat": raw_source,
        "find src -name '*.py' -print0 | xargs -0 cat": raw_source,
        "find src -name '*.py' -exec cat {} +": raw_source,
        "find src -name '*.py' -print0 | xargs -0 rg target": search_output,
        "find src -name '*.py' | xargs -r rg target": search_output,
        "find src -name '*.py' | xargs -I {} rg target {}": search_output,
        "find src -name '*.py' -exec env LC_ALL=C cat {} +": raw_source,
        "find src -name '*.py' -exec sudo -H cat {} +": raw_source,
        "find src -name '*.py' -exec sh -c 'cat \"$1\"' _ {} \\;": raw_source,
        "find src -name '*.py' -exec env LC_ALL=C rg target {} +": search_output,
        "find src -name '*.py' -exec sudo -H rg target {} +": search_output,
        "find src -name '*.py' -exec sh -c 'rg target \"$1\"' _ {} \\;": search_output,
        "xargs -a files.txt rg target": search_output,
        "xargs --arg-file files.txt rg target": search_output,
        "xargs -d '\\n' rg target": search_output,
        "xargs -L 1 rg target": search_output,
        "xargs -t rg target": search_output,
        "xargs -p rg target": search_output,
        "xargs -o rg target": search_output,
    }

    for command, raw in cases.items():
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            options=opts(max_chars=400),
        )

        assert result.changed is False, command
        assert result.text == raw


def test_package_command_flags_still_preserve_failure_signal() -> None:
    raw = "\n".join(
        [
            *[f"package manager chatter {index}" for index in range(80)],
            "ERROR: No matching distribution found for missing-package",
            *[f"more package manager chatter {index}" for index in range(80)],
        ]
    )

    for command in (
        "apt-get -y install imaginary-package",
        "sudo DEBIAN_FRONTEND=noninteractive apt-get -y install imaginary-package",
        "/usr/bin/apt-get -y install imaginary-package",
        "apt-get -u install imaginary-package",
        "apt-get -t bookworm install imaginary-package",
        "apt-get --target-release bookworm install imaginary-package",
        "apt-get -o Acquire::Retries=3 install imaginary-package",
        "apt --option Acquire::Retries=3 install imaginary-package",
        "apt -qq update",
        "pip --disable-pip-version-check install missing-package",
        "pip --proxy http://proxy.example install missing-package",
        "pip --trusted-host pypi.org install missing-package",
        "pip --timeout 60 install missing-package",
        "pip --retries 2 install missing-package",
        "pip --cache-dir .cache install missing-package",
        "python -I -u -m pip install missing-package",
        "python -m pip --no-cache-dir install missing-package",
        "python -m pip --cert cert.pem install missing-package",
        "python -m pip --timeout 60 install missing-package",
        "uv --directory . sync",
        "uv --config-file uv.toml sync",
        "uv --cache-dir .uv-cache sync",
        "uv pip sync requirements.txt",
    ):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=opts(max_chars=500, max_lines=10),
        )

        assert result.changed is True, command
        assert result.metadata["command_class"] in {"apt", "python_package"}, command
        assert "No matching distribution found" in result.text, command


def test_package_names_that_match_read_tools_still_compact_install_logs() -> None:
    raw = "added package metadata\n" + package_progress("install jq/yq", 120)

    for command, command_class in (
        ("apt install jq", "apt"),
        ("pip install jq", "python_package"),
        ("npm install yq", "node"),
    ):
        result = reduce_text(raw, command=command, tool_name="terminal", options=opts())

        assert result.changed is True, command
        assert result.metadata["command_class"] == command_class


def test_context_payload_tools_stay_byte_for_byte_unchanged() -> None:
    raw_payload = json.dumps(
        {
            "content": "\n".join(f"important context line {index}" for index in range(180)),
            "metadata": {"source": "retrieval"},
        },
        ensure_ascii=False,
    )

    for tool_name in (
        "memory",
        "hindsight_recall",
        "hindsight_reflect",
        "lcm_expand",
        "lcm_expand_query",
        "mcp__mindlyos__get_note_page",
        "mcp__remarkable__remarkable_read",
        "web_extract",
    ):
        assert (
            transform_tool_result(raw_payload, tool_name=tool_name, noisegate_max_chars=400)
            is None
        )


def test_mixed_source_like_terminal_output_fails_raw_instead_of_guessing() -> None:
    raw = "\n".join(
        [
            "src/app.py:10:def important():",
            *[f"src/app.py:{index}:    exact code search hit {index}" for index in range(11, 180)],
            "ERROR appears inside source text but is not a build failure",
        ]
    )

    result = reduce_text(
        raw,
        command="rg ERROR src",
        tool_name="terminal",
        options=opts(max_chars=400),
    )

    assert result.changed is False
    assert result.text == raw


def test_non_build_docker_compose_commands_are_not_build_logs() -> None:
    raw = package_progress("docker compose context", 120)

    for command in ("docker compose ps", "docker compose config"):
        result = reduce_text(raw, command=command, tool_name="terminal", options=opts())

        assert result.metadata["command_class"] != "docker_build"
        assert result.metadata["command_class"] != "docker_logs"
