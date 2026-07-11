from __future__ import annotations

import json
from pathlib import Path

from noisegate.engine import (
    NoisegateOptions,
    _redirected_stream_visibility,
    _shell_tokens,
    _tokens_redirect_stderr,
    _tokens_redirect_stdout,
    classify_command,
    reduce_text,
)
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


def test_artifact_notice_path_marks_gaps_between_stitched_failure_lines(tmp_path: Path) -> None:
    raw_stdout = "\n".join(
        [
            *[f"pytest setup noise {index}" for index in range(30)],
            "Traceback (most recent call last):",
            "ModuleNotFoundError: No module named missing_pkg",
            *[f"pytest more noise {index}" for index in range(16)],
            "FAILED tests/test_demo.py::test_signal",
            *[f"pytest tail noise {index}" for index in range(30)],
        ]
    )

    result = reduce_text(
        raw_stdout,
        command="pytest -q",
        tool_name="terminal",
        exit_code=1,
        options=opts(
            max_chars=900,
            max_lines=12,
            head_lines=1,
            tail_lines=1,
            artifact_enabled=True,
            artifact_dir=tmp_path / "artifacts",
        ),
    )

    assert result.changed is True
    assert "Traceback (most recent call last):" in result.text
    assert "ModuleNotFoundError: No module named missing_pkg" in result.text
    assert "FAILED tests/test_demo.py::test_signal" in result.text
    assert "[noisegate: omitted" in result.text
    assert result.text.index("Traceback") < result.text.index("ModuleNotFoundError")
    assert result.text.index("ModuleNotFoundError") < result.text.index("FAILED")


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
        "apt install imaginary-package |& grep ERROR",
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


def test_python_module_pytest_flags_and_xargs_use_pytest_reducer() -> None:
    successful_pytest_raw = "\n".join(
        [
            *[f"tests/test_{index}.py::test_ok PASSED" for index in range(120)],
            "123 passed in 2.00s",
        ]
    )

    for command in (
        "rg target src && pytest -q",
        "rg target src; pytest -q",
        "python -I -m pytest -q",
        "python -X dev -m pytest -q",
        "python -W ignore -m pytest -q",
        "python -Im pytest -q",
        "python -uIm pytest -q",
        "python -mpytest -q",
        "python -u -m pytest",
        "find tests -name '*.py' | xargs pytest -q",
        'rg "$(cat pattern.txt)" src | xargs pytest -q',
        "rg -f <(cat patterns.txt) src | xargs pytest -q",
    ):
        result = reduce_text(
            successful_pytest_raw,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=opts(max_chars=180, max_lines=3, head_lines=1, tail_lines=1),
        )

        assert result.changed is True, command
        assert result.metadata["command_class"] == "pytest", command
        assert "123 passed in 2.00s" in result.text, command


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
    docker_raw = "\n".join(
        [
            *[f"docker build progress before {index}" for index in range(80)],
            "#1 [internal] load build definition from Dockerfile",
            "#8 ERROR: failed to solve: process exited",
            *[f"docker build progress after {index}" for index in range(80)],
        ]
    )
    docker_after_source_hit_raw = "\n".join(
        [
            "src/Dockerfile:1:# Dockerfile literal",
            *[f"docker build progress before {index}" for index in range(80)],
            "#1 [internal] load build definition from Dockerfile",
            "#8 ERROR: failed to solve: process exited",
            *[f"docker build progress after {index}" for index in range(80)],
        ]
    )
    pytest_pass_raw = "\n".join(
        [
            *[f"pytest progress before {index}" for index in range(80)],
            "123 passed in 2.00s",
            *[f"pytest progress after {index}" for index in range(80)],
        ]
    )
    node_success_raw = "\n".join(
        [
            *[f"npm progress before {index}" for index in range(80)],
            "added 451 packages in 12s",
            "found 0 vulnerabilities",
            *[f"npm progress after {index}" for index in range(80)],
        ]
    )
    node_success_after_source_hit_raw = "\n".join(
        [
            "src/package.json:1:added 451 packages literal",
            *[f"npm progress before {index}" for index in range(80)],
            "added 451 packages in 12s",
            "found 0 vulnerabilities",
            *[f"npm progress after {index}" for index in range(80)],
        ]
    )
    node_success_after_path_hits_raw = "\n".join(
        [
            *[f"src/file_{index}.py" for index in range(10)],
            *[f"npm progress before {index}" for index in range(80)],
            "added 451 packages in 12s",
            "found 0 vulnerabilities",
            *[f"npm progress after {index}" for index in range(80)],
        ]
    )
    node_up_to_date_raw = "\n".join(
        [
            *[f"npm progress before {index}" for index in range(80)],
            "up to date in 1s",
            *[f"npm progress after {index}" for index in range(80)],
        ]
    )
    node_test_raw = "\n".join(
        [
            *[f"npm test progress before {index}" for index in range(80)],
            "1 failed, 123 passed",
            *[f"npm test progress after {index}" for index in range(80)],
        ]
    )
    node_error_after_path_hits_raw = "\n".join(
        [
            *[f"src/file_{index}.js" for index in range(5)],
            *[f"npm test noise before {index}" for index in range(80)],
            "Error: boom",
            "    at Object.<anonymous> (src/app.test.js:3:9)",
            *[f"npm test noise after {index}" for index in range(80)],
        ]
    )
    node_pass_raw = "\n".join(
        [
            *[f"npm test progress before {index}" for index in range(80)],
            "123 passed in 2.00s",
            *[f"npm test progress after {index}" for index in range(80)],
        ]
    )
    pip_success_raw = "\n".join(
        [
            *[f"pip progress before {index}" for index in range(80)],
            "Installing collected packages: requests",
            "Successfully installed requests-2.32.0",
            *[f"pip progress after {index}" for index in range(80)],
        ]
    )
    pip_error_after_path_hits_raw = "\n".join(
        [
            *[f"src/file_{index}.py" for index in range(5)],
            *[f"pip chatter {index}" for index in range(80)],
            "ERROR: No matching distribution found for missing-package",
            *[f"pip after {index}" for index in range(80)],
        ]
    )
    uv_error_after_path_hits_raw = "\n".join(
        [
            *[f"src/file_{index}.py" for index in range(5)],
            *[f"uv chatter {index}" for index in range(80)],
            "ResolutionImpossible: package versions conflict",
            *[f"uv after {index}" for index in range(80)],
        ]
    )
    pip_success_after_source_hit_raw = "\n".join(
        [
            "src/notes.py:1:Successfully installed literal",
            *[f"pip progress before {index}" for index in range(80)],
            "Installing collected packages: requests",
            "Successfully installed requests-2.32.0",
            *[f"pip progress after {index}" for index in range(80)],
        ]
    )
    uv_success_raw = "\n".join(
        [
            *[f"uv progress before {index}" for index in range(80)],
            "Resolved 192 packages in 1.2s",
            *[f"uv progress after {index}" for index in range(80)],
        ]
    )
    uv_success_after_source_hit_raw = "\n".join(
        [
            "src/lock.py:1:Resolved 192 packages literal",
            *[f"uv progress before {index}" for index in range(80)],
            "Resolved 192 packages in 1.2s",
            *[f"uv progress after {index}" for index in range(80)],
        ]
    )
    uv_audit_raw = "\n".join(
        [
            *[f"uv progress before {index}" for index in range(80)],
            "Audited 157 packages in 0.13ms",
            *[f"uv progress after {index}" for index in range(80)],
        ]
    )
    uv_no_solution_after_path_hits_raw = "\n".join(
        [
            *[f"src/file_{index}.py" for index in range(5)],
            *[f"uv progress before {index}" for index in range(80)],
            "  × No solution found when resolving dependencies:",  # noqa: RUF001
            "  ╰─▶ requirements are unsatisfiable.",
            *[f"uv progress after {index}" for index in range(80)],
        ]
    )
    apt_dpkg_error_after_path_hits_raw = "\n".join(
        [
            *[f"src/file_{index}.py" for index in range(5)],
            *[f"apt progress before {index}" for index in range(80)],
            "dpkg: error processing archive /var/cache/apt/archives/foo.deb (--unpack):",
            " unable to create file: Permission denied",
            "Errors were encountered while processing:",
            " /var/cache/apt/archives/foo.deb",
            *[f"apt progress after {index}" for index in range(80)],
        ]
    )
    apt_success_after_path_hits_raw = "\n".join(
        [
            *[f"src/file_{index}.py" for index in range(5)],
            *[f"apt progress before {index}" for index in range(80)],
            "0 upgraded, 1 newly installed, 0 to remove and 0 not upgraded.",
            "Setting up jq (1.6-2.1) ...",
            *[f"apt progress after {index}" for index in range(80)],
        ]
    )
    single_path_then_package_anchor_cases = [
        (
            "rg --files; npm test",
            ["src/file.py", "Error: boom", *[f"plain npm line {index}" for index in range(60)]],
            "node",
            "Error: boom",
            1,
        ),
        (
            "rg --files src; npm install",
            [
                *[f"src/file_{index}.py" for index in range(20)],
                *[f"plain npm progress {index}" for index in range(60)],
                "added 451 packages in 12s",
                "found 0 vulnerabilities",
                *[f"plain npm after {index}" for index in range(60)],
            ],
            "node",
            "added 451 packages",
            0,
        ),
        (
            "rg --files; uv sync",
            [
                "src/file.py",
                "No solution found when resolving dependencies",
                *[f"plain uv line {index}" for index in range(60)],
            ],
            "python_package",
            "No solution found",
            1,
        ),
        (
            "rg --files; python -m pip install requests",
            [
                "src/file.py",
                "Successfully installed requests-2.32.0",
                *[f"plain pip line {index}" for index in range(60)],
            ],
            "python_package",
            "Successfully installed",
            0,
        ),
        (
            "rg --files; apt install jq",
            [
                "src/file.py",
                "Setting up jq (1.6-2.1) ...",
                *[f"plain apt line {index}" for index in range(60)],
            ],
            "apt",
            "Setting up jq",
            0,
        ),
        (
            "rg -l target src && apt-get update",
            [
                *[f"src/file_{index}.py" for index in range(5)],
                "Get:1 http://deb.example stable/main amd64 Packages",
                "Hit:2 http://deb.example stable InRelease",
                "Fetched 12.3 MB in 2s (6,123 kB/s)",
                "Reading package lists... Done",
                *[f"plain apt update line {index}" for index in range(60)],
            ],
            "apt",
            "Reading package lists",
            0,
        ),
        (
            "rg --files; apt-get update",
            [
                "src/file.py",
                "Get:1 http://deb.example stable/main amd64 Packages",
                "Reading package lists... Done",
                *[f"plain apt update line {index}" for index in range(60)],
            ],
            "apt",
            "Get:1",
            0,
        ),
        (
            "rg --files; docker build .",
            [
                "src/file.py",
                "failed to solve: process exited",
                *[f"plain docker line {index}" for index in range(60)],
            ],
            "docker_build",
            "failed to solve",
            1,
        ),
    ]
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
    traceback_only_pytest = "\n".join(
        [
            *[f"pytest collection noise before {index}" for index in range(70)],
            "Traceback (most recent call last):",
            "src/app.py:3: in <module>",
            "    import missing_package",
            "ModuleNotFoundError: No module named 'missing_package'",
            *[f"pytest collection noise after {index}" for index in range(70)],
        ]
    )
    short_frame_pytest = "\n".join(
        [
            *[f"pytest collection noise before {index}" for index in range(70)],
            "src/app.py:3: in <module>",
            "    import missing_package",
            "ModuleNotFoundError: No module named 'missing_package'",
            *[f"pytest collection noise after {index}" for index in range(70)],
        ]
    )
    readme_prefix_then_npm_raw = "\n".join(
        [
            "# Notes",
            *[f"npm before {index}" for index in range(80)],
            "added 451 packages in 12s",
            "found 0 vulnerabilities",
            *[f"npm after {index}" for index in range(80)],
        ]
    )
    source_prefix_then_pytest_raw = "\n".join(
        [
            "def fixture():",
            "    return 1",
            *[f"pytest before {index}" for index in range(80)],
            "FAILED tests/test_demo.py::test_signal",
            *[f"pytest after {index}" for index in range(80)],
        ]
    )

    for command, raw, expected_class, signal, exit_code in (
        (
            "cat README && npm install",
            readme_prefix_then_npm_raw,
            "node",
            "added 451 packages",
            0,
        ),
        (
            "cat file.py && pytest -q",
            source_prefix_then_pytest_raw,
            "pytest",
            "FAILED tests/test_demo.py::test_signal",
            1,
        ),
        (
            "rg -l test tests && pytest -q",
            pytest_raw,
            "pytest",
            "FAILED tests/test_demo.py::test_signal",
            1,
        ),
        (
            "rg -I target src && pytest -q",
            pytest_raw,
            "pytest",
            "FAILED tests/test_demo.py::test_signal",
            1,
        ),
        (
            "grep -h target src && pytest -q",
            pytest_raw,
            "pytest",
            "FAILED tests/test_demo.py::test_signal",
            1,
        ),
        (
            "rg target src; python -Im pytest -q",
            pytest_raw,
            "pytest",
            "FAILED tests/test_demo.py::test_signal",
            1,
        ),
        (
            "rg passed src && pytest -q",
            pytest_pass_raw,
            "pytest",
            "123 passed in 2.00s",
            0,
        ),
        (
            "rg '123 passed' src; pytest -q",
            pytest_pass_raw,
            "pytest",
            "123 passed in 2.00s",
            0,
        ),
        (
            "rg FAILED src && pytest -q",
            pytest_raw,
            "pytest",
            "FAILED tests/test_demo.py::test_signal",
            1,
        ),
        (
            "rg -e NotPresent src || pytest -q",
            pytest_pass_raw,
            "pytest",
            "123 passed in 2.00s",
            0,
        ),
        (
            "rg target src && pytest -q",
            realistic_pytest_with_frame,
            "pytest",
            "FAILED tests/test_artifacts.py::test_writes_config",
            1,
        ),
        (
            "rg -e NotPresent src/app.py; pytest -q",
            pytest_raw,
            "pytest",
            "FAILED tests/test_demo.py::test_signal",
            1,
        ),
        (
            "rg -I -e NotPresent src/app.py; pytest -q",
            realistic_pytest_with_frame,
            "pytest",
            "FAILED tests/test_artifacts.py::test_writes_config",
            1,
        ),
        (
            "rg --no-filename -e NotPresent src/app.py; pytest -q",
            realistic_pytest_with_frame,
            "pytest",
            "FAILED tests/test_artifacts.py::test_writes_config",
            1,
        ),
        (
            "rg -e NotPresent src/app.py && pytest -q",
            traceback_only_pytest,
            "pytest",
            "ModuleNotFoundError: No module named 'missing_package'",
            1,
        ),
        (
            "rg -e src/app.py src && pytest -q",
            traceback_only_pytest,
            "pytest",
            "ModuleNotFoundError: No module named 'missing_package'",
            1,
        ),
        (
            "rg --regexp NotPresent src/app.py; pytest -q",
            traceback_only_pytest,
            "pytest",
            "ModuleNotFoundError: No module named 'missing_package'",
            1,
        ),
        (
            "rg -I -e NotPresent src/app.py && pytest -q",
            traceback_only_pytest,
            "pytest",
            "ModuleNotFoundError: No module named 'missing_package'",
            1,
        ),
        (
            "rg --no-filename -e NotPresent src/app.py && pytest -q",
            traceback_only_pytest,
            "pytest",
            "ModuleNotFoundError: No module named 'missing_package'",
            1,
        ),
        (
            "bash -lc 'rg -e NotPresent src/app.py && pytest -q'",
            traceback_only_pytest,
            "pytest",
            "ModuleNotFoundError: No module named 'missing_package'",
            1,
        ),
        (
            "grep -e NotPresent src/app.py && pytest -q",
            traceback_only_pytest,
            "pytest",
            "ModuleNotFoundError: No module named 'missing_package'",
            1,
        ),
        (
            "rg -e NotPresent src/app.py && pytest -q",
            short_frame_pytest,
            "pytest",
            "ModuleNotFoundError: No module named 'missing_package'",
            1,
        ),
        (
            "rg -f patterns.txt src/app.py && pytest -q",
            short_frame_pytest,
            "pytest",
            "ModuleNotFoundError: No module named 'missing_package'",
            1,
        ),
        (
            "rg --file=patterns.txt src/app.py && pytest -q",
            short_frame_pytest,
            "pytest",
            "ModuleNotFoundError: No module named 'missing_package'",
            1,
        ),
        ("rg failed src; docker build .", docker_raw, "docker_build", "failed to solve", 1),
        (
            "rg 'failed to solve' src; docker build .",
            docker_raw,
            "docker_build",
            "failed to solve",
            1,
        ),
        ("rg Dockerfile src; docker build .", docker_raw, "docker_build", "failed to solve", 1),
        (
            "rg Dockerfile src; docker build .",
            docker_after_source_hit_raw,
            "docker_build",
            "failed to solve",
            1,
        ),
        ("rg >(pytest -q) src", pytest_pass_raw, "pytest", "123 passed in 2.00s", 0),
        ('rg "$(docker build .)" src', docker_raw, "docker_build", "failed to solve", 1),
        ("rg target src; npm install", node_raw, "node", "npm ERR! code ERESOLVE", 1),
        ("rg target src; npm install", node_success_raw, "node", "added 451 packages", 0),
        (
            "rg 'added 451 packages' src; npm install",
            node_success_raw,
            "node",
            "added 451 packages",
            0,
        ),
        (
            "rg 'added 451 packages' src; npm install",
            node_success_after_source_hit_raw,
            "node",
            "added 451 packages",
            0,
        ),
        (
            "rg -l target src; npm install",
            node_success_after_path_hits_raw,
            "node",
            "added 451 packages",
            0,
        ),
        (
            "rg --files; npm install",
            node_success_after_path_hits_raw,
            "node",
            "added 451 packages",
            0,
        ),
        ("bash -lc 'rg \"$(npm install)\" src'", node_success_raw, "node", "added 451 packages", 0),
        ('rg "$(npm install)" src', node_success_raw, "node", "added 451 packages", 0),
        ("rg added src; npm install", node_success_raw, "node", "added 451 packages", 0),
        ("rg target src; npm install --no-audit", node_up_to_date_raw, "node", "up to date", 0),
        (
            "rg 'up to date' src; npm install --no-audit",
            node_up_to_date_raw,
            "node",
            "up to date",
            0,
        ),
        ("rg target src; npm test", node_test_raw, "node", "1 failed, 123 passed", 1),
        ("rg --files; npm test", node_error_after_path_hits_raw, "node", "Error: boom", 1),
        ("rg -l target src; npm test", node_error_after_path_hits_raw, "node", "Error: boom", 1),
        ("rg target src; npm test", node_pass_raw, "node", "123 passed in 2.00s", 0),
        (
            "rg target src; python -m pip install requests",
            pip_success_raw,
            "python_package",
            "Successfully installed requests-2.32.0",
            0,
        ),
        (
            "rg --files; python -m pip install missing-package",
            pip_error_after_path_hits_raw,
            "python_package",
            "No matching distribution found",
            1,
        ),
        (
            "rg 'Successfully installed' src; python -m pip install requests",
            pip_success_after_source_hit_raw,
            "python_package",
            "Successfully installed requests-2.32.0",
            0,
        ),
        (
            'rg "$(python -m pip install requests)" src',
            pip_success_raw,
            "python_package",
            "Successfully installed requests-2.32.0",
            0,
        ),
        (
            "bash -lc 'rg \"$(python -m pip install requests)\" src'",
            pip_success_raw,
            "python_package",
            "Successfully installed requests-2.32.0",
            0,
        ),
        (
            "rg target src; python -Im pip install requests",
            pip_success_raw,
            "python_package",
            "Successfully installed requests-2.32.0",
            0,
        ),
        (
            "rg target src; python -W ignore -m pip install requests",
            pip_success_raw,
            "python_package",
            "Successfully installed requests-2.32.0",
            0,
        ),
        (
            "rg target src; python -X dev -m pip install requests",
            pip_success_raw,
            "python_package",
            "Successfully installed requests-2.32.0",
            0,
        ),
        (
            "rg installed src; python -m pip install requests",
            pip_success_raw,
            "python_package",
            "Successfully installed requests-2.32.0",
            0,
        ),
        ("rg target src; uv sync", uv_success_raw, "python_package", "Resolved 192 packages", 0),
        ("rg resolved src; uv sync", uv_success_raw, "python_package", "Resolved 192 packages", 0),
        (
            "rg resolved src; uv sync",
            uv_success_after_source_hit_raw,
            "python_package",
            "Resolved 192 packages",
            0,
        ),
        ("rg target src; uv sync", uv_audit_raw, "python_package", "Audited 157 packages", 0),
        (
            "rg --files; uv sync",
            uv_error_after_path_hits_raw,
            "python_package",
            "ResolutionImpossible",
            1,
        ),
        (
            "rg --files; uv sync",
            uv_no_solution_after_path_hits_raw,
            "python_package",
            "No solution found",
            1,
        ),
        ("rg audited src; uv sync", uv_audit_raw, "python_package", "Audited 157 packages", 0),
        (
            "rg --files; apt install foo",
            apt_dpkg_error_after_path_hits_raw,
            "apt",
            "dpkg: error",
            1,
        ),
        (
            "rg --files; apt install jq",
            apt_success_after_path_hits_raw,
            "apt",
            "Setting up jq",
            0,
        ),
        *(
            (command, "\n".join(lines), expected_class, signal, exit_code)
            for command, lines, expected_class, signal, exit_code in (
                single_path_then_package_anchor_cases
            )
        ),
        (
            "bash -lc 'rg -l test tests && pytest -q'",
            pytest_raw,
            "pytest",
            "FAILED tests/test_demo.py::test_signal",
            1,
        ),
    ):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=exit_code,
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

    successful_extensionless_cat = reduce_text(
        "\n".join(
            [
                "Release notes",
                "123 passed in 2.00s historical note",
                *[f"plain prose line {index}" for index in range(100)],
            ]
        ),
        command="cat README || pytest -q",
        tool_name="terminal",
        exit_code=0,
        options=opts(max_chars=120, max_lines=5, head_lines=1, tail_lines=1),
    )

    assert successful_extensionless_cat.changed is False
    assert successful_extensionless_cat.metadata["command_class"] == "file_read"

    successful_extensionless_cat_then_pytest = reduce_text(
        successful_extensionless_cat.text,
        command="cat README && pytest -q",
        tool_name="terminal",
        exit_code=0,
        options=opts(max_chars=120, max_lines=5, head_lines=1, tail_lines=1),
    )

    assert successful_extensionless_cat_then_pytest.changed is False
    assert successful_extensionless_cat_then_pytest.metadata["command_class"] == "file_read"

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
        ("cat README.md || pytest -q", readme_pytest_literal, 0),
        ("pytest -q && cat README.md", readme_pytest_literal, None),
        ("bash -lc 'cat file.py && pytest -q > pytest.log'", source_with_test_words, 1),
        ("cat fixture.txt && pytest -q > pytest.log", plain_text_fixture_with_failure_words, 1),
        ("pytest -q && cat fixture.txt", plain_text_fixture_with_failure_words, None),
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

    hidden_source_search_literal = "\n".join(
        [
            "123 passed in 2.00s literal historical note",
            *[f"literal source prose line {index}" for index in range(100)],
        ]
    )
    hidden_source_search_or_pytest = reduce_text(
        hidden_source_search_literal,
        command='rg -I "123 passed" notes.md || pytest -q',
        tool_name="terminal",
        exit_code=0,
        options=opts(max_chars=400),
    )

    assert hidden_source_search_or_pytest.changed is True
    assert hidden_source_search_or_pytest.metadata["command_class"] == "pytest"
    assert "123 passed in 2.00s" in hidden_source_search_or_pytest.text

    filename_source_search_or_pytest = reduce_text(
        "\n".join(
            f"src/file_{index}.py:{index}:123 passed in fixture docs"
            for index in range(80)
        ),
        command="rg '123 passed' src || pytest -q",
        tool_name="terminal",
        exit_code=0,
        options=opts(max_chars=400),
    )

    assert filename_source_search_or_pytest.changed is False
    assert filename_source_search_or_pytest.metadata["command_class"] == "source_search"

    failed_cat_then_npm = reduce_text(
        "\n".join(
            [
                "cat: MISSING: No such file or directory",
                *[f"npm progress before {index}" for index in range(80)],
                "added 451 packages in 12s",
                "found 0 vulnerabilities",
                *[f"npm progress after {index}" for index in range(80)],
            ]
        ),
        command="cat MISSING || npm install",
        tool_name="terminal",
        exit_code=0,
        options=opts(max_chars=500, max_lines=10),
    )

    assert failed_cat_then_npm.changed is True
    assert failed_cat_then_npm.metadata["command_class"] == "node"
    assert "added 451 packages" in failed_cat_then_npm.text

    failed_bat_then_npm = reduce_text(
        "\n".join(
            [
                "bat: missing.txt: No such file or directory",
                *[f"npm progress before {index}" for index in range(80)],
                "added 451 packages in 12s",
                "found 0 vulnerabilities",
                *[f"npm progress after {index}" for index in range(80)],
            ]
        ),
        command="bat missing.txt || npm install",
        tool_name="terminal",
        exit_code=0,
        options=opts(max_chars=500, max_lines=10),
    )

    assert failed_bat_then_npm.changed is True
    assert failed_bat_then_npm.metadata["command_class"] == "node"
    assert "added 451 packages" in failed_bat_then_npm.text

    failed_less_readme_then_npm = reduce_text(
        "\n".join(
            [
                "README: No such file or directory",
                *[f"npm progress before {index}" for index in range(80)],
                "added 451 packages in 12s",
                "found 0 vulnerabilities",
                *[f"npm progress after {index}" for index in range(80)],
            ]
        ),
        command="less README || npm install",
        tool_name="terminal",
        exit_code=0,
        options=opts(max_chars=500, max_lines=10),
    )

    assert failed_less_readme_then_npm.changed is True
    assert failed_less_readme_then_npm.metadata["command_class"] == "node"
    assert "added 451 packages" in failed_less_readme_then_npm.text

    for command, raw in (
        ("cat missing.py || npm test", "cat: missing.py: No such file or directory"),
        ("bat missing.txt || npm install", "bat: missing.txt: No such file or directory"),
        ("less missing.txt || npm install", "missing.txt: No such file or directory"),
        ("less README || npm install", "README: No such file or directory"),
        (
            "more missing.txt || npm install",
            "more: cannot open missing.txt: No such file or directory",
        ),
    ):
        assert classify_command(command, raw, exit_code=1) != "file_read", command

    failed_cat_then_npm_test = reduce_text(
        "\n".join(
            [
                "cat: missing.py: No such file or directory",
                *[f"npm test noise before {index}" for index in range(80)],
                "Error: boom",
                "    at Object.<anonymous> (src/app.test.js:3:9)",
                *[f"npm test noise after {index}" for index in range(80)],
            ]
        ),
        command="cat missing.py || npm test",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=500, max_lines=10),
    )

    assert failed_cat_then_npm_test.changed is True
    assert failed_cat_then_npm_test.metadata["command_class"] == "node"
    assert "Error: boom" in failed_cat_then_npm_test.text

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

    hidden_heading_search_with_literal_error = "\n".join(
        [
            "src/app.py",
            *[f"ERROR literal source line {index}" for index in range(80)],
        ]
    )
    exact_hidden_heading_search = reduce_text(
        hidden_heading_search_with_literal_error,
        command="rg --heading --no-line-number ERROR src && uv sync",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=400),
    )

    assert exact_hidden_heading_search.changed is False
    assert exact_hidden_heading_search.text == hidden_heading_search_with_literal_error
    assert exact_hidden_heading_search.metadata["command_class"] == "source_search"

    hidden_context_search_with_literal_error = "\n".join(
        [
            "ERROR literal source line 1",
            *[f"plain source context line {index}" for index in range(80)],
            "ERROR literal source line 2",
        ]
    )
    exact_hidden_context_search = reduce_text(
        hidden_context_search_with_literal_error,
        command="rg -I -C1 ERROR src && uv sync",
        tool_name="terminal",
        exit_code=1,
        options=opts(max_chars=400),
    )

    assert exact_hidden_context_search.changed is False
    assert exact_hidden_context_search.text == hidden_context_search_with_literal_error
    assert exact_hidden_context_search.metadata["command_class"] == "source_search"

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
        "rg -e NotPresent -e Traceback src/app.py && pytest -q",
        "rg --sort-files Traceback src/app.py && pytest -q",
        "rg --ignore-file .ignore Traceback src/app.py && pytest -q",
        "rg --pre-glob '*.py' Traceback src/app.py && pytest -q",
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


def test_unsafe_search_substitutions_and_null_input_generators_are_compacted() -> None:
    pytest_raw = "\n".join(
        [
            *[f"pytest noise before {index}" for index in range(80)],
            "FAILED tests/test_demo.py::test_signal",
            *[f"pytest noise after {index}" for index in range(80)],
        ]
    )
    generated_json = "\n".join(str(index) for index in range(1000))

    for command in (
        'rg "$(pytest -q)" src',
        "rg $(pytest -q) src",
        'rg "prefix$(pytest -q)" src',
        "true & rg prefix$(pytest -q) src",
        'true & rg "prefix$(pytest -q)" src',
        'rg "prefix$(tail -f log & pytest -q)" src',
        "bash -lc 'rg \"$(pytest -q)\" src'",
        "rg `pytest -q` src",
        'echo heading && cat "$(pytest -q)"',
        'rg "$(echo $(pytest -q))" src',
    ):
        search_with_command_substitution = reduce_text(
            pytest_raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=opts(max_chars=500, max_lines=10),
        )

        assert search_with_command_substitution.changed is True, command
        assert search_with_command_substitution.metadata["command_class"] == "pytest", command
        assert "FAILED tests/test_demo.py::test_signal" in search_with_command_substitution.text

    null_input_jq = reduce_text(
        generated_json,
        command="jq -n 'range(0;1000)'",
        tool_name="terminal",
        exit_code=0,
        options=opts(max_chars=120, max_lines=8, head_lines=1, tail_lines=1),
    )

    assert null_input_jq.changed is True
    assert null_input_jq.metadata["command_class"] == "generic"
    assert "[noisegate: omitted" in null_input_jq.text


def test_sed_mutating_and_search_options_do_not_receive_exact_passthrough() -> None:
    raw = "\n".join(f"def function_{index}(): return {index}" for index in range(160))
    compactable_commands = (
        'sed -i "1,20p" file.py',
        'sed -I .bak -n "1,20p" file.py',
        'sed -I.bak -n "1,20p" file.py',
        'sed -i.bak -n "1,20p" file.py',
        'sed --in-place -n "1,20p" file.py',
        'sed --in-place=.bak -n "1,20p" file.py',
        'sed --i -n "1,20p" file.py',
        'sed --in -n "1,20p" file.py',
        'sed --in- -n "1,20p" file.py',
        'sed --in-p -n "1,20p" file.py',
        'sed --in-plac=.bak -n "1,20p" file.py',
        'sed -ni "1,20p" file.py',
        'sed -Ein "1,20p" file.py',
        'sed -n -e/ERROR/p build.log',
        'sed -n -- /ERROR/p build.log',
        'sed --quiet -- /ERROR/p build.log',
        'sed -nl /ERROR/p build.log',
        'sed -n -l /ERROR/p build.log',
        'sed -n -l 80 /ERROR/p build.log',
        'sed -nl 80 /ERROR/p build.log',
        'sed -nl80 /ERROR/p build.log',
        'sed -n --line-length 80 /ERROR/p build.log',
        'sed -n --line-length=80 /ERROR/p build.log',
        'sed -n --exp=/ERROR/p build.log',
        'sed -n --exp /ERROR/p build.log',
        'sed --quiet /ERROR/p build.log',
    )
    display_commands = (
        'sed -e "1,200p" -n file.py',
        "sed -e '' README.md",
        'sed --expression="1,200p" --quiet file.py',
        'sed -n "1,200p" file.py',
        "sed -es/inline/visible/ file.py",
        "sed -finline.sed file.py",
        "sed -n -e1,20p file.py",
        "sed -n -es/inline/visible/ file.py",
        "sed -n -finput.sed file.py",
        "sed -f <file.py input.sed",
        'sed --line-length 2>/dev/null +80 "1p" file.py',
        'sed -n -l 80 "1,20p" file.py',
        'sed -nl80 "1,20p" file.py',
        'sed -n --line-length=80 "1,20p" file.py',
        'sed -n --line-length=+80 "1,20p" file.py',
        'sed -n --line-length +80 "1,20p" file.py',
        'sed -n --exp="1,20p" file.py',
        'sed --quiet "1,20p" file.py',
        'sed -n -- 1,20p file.py',
        'sed -n 1,20p <>file.py',
        'sed -nl 1,20p file.py',
        'sed -n -l 1,20p file.py',
        "sed --file effects.sed /tmp/p",
        "sed --file=effects.sed /tmp/p",
        "sed --fil effects.sed /tmp/p",
        "sed --fil=effects.sed /tmp/p",
    )

    for command in compactable_commands:
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            options=opts(max_chars=400),
        )

        assert result.changed is True, command
        assert result.metadata["command_class"] == "generic", command

    for command in display_commands:
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            options=opts(max_chars=400),
        )

        assert result.changed is False, command
        assert result.metadata["command_class"] == "file_read", command
        assert result.text == raw, command


def test_sed_inputless_and_invalid_forms_do_not_protect_fallback_output() -> None:
    pytest_raw = "\n".join(
        [
            "sed: invalid invocation",
            *[f"tests/test_{index}.py::test_case_{index} PASSED" for index in range(180)],
            "================ 180 passed in 2.00s ================",
        ]
    )
    commands = (
        "sed --file=/dev/null 2>&1 || pytest -q",
        "sed --bogus '1,20p' file.py || pytest -q",
        "sed -f 2>/dev/null script.sed || pytest -q",
        "sed --file 2>/dev/null script.sed || pytest -q",
        "sed -e 2>/dev/null p || pytest -q",
        "sed -f script.sed -- - || pytest -q",
        "sed -f script.sed /dev/stdin || pytest -q",
        "sed -f script.sed /dev/fd/0 || pytest -q",
        "sed --line-length bogus 1p file.py || pytest -q",
    )

    for command in commands:
        result = reduce_text(
            pytest_raw,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=opts(max_chars=300, max_lines=20),
        )

        assert result.changed is True, command
        assert result.metadata["command_class"] == "pytest", command

    pytest_like_source = "\n".join(
        f"tests/test_{index}.py::test_case_{index} PASSED" for index in range(180)
    )
    for line_length in ("+5", "-1", "999999999999999999999999"):
        command = f"sed -l {line_length} -n 1p file.py || pytest -q"
        result = reduce_text(
            pytest_like_source,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=opts(max_chars=300, max_lines=20),
        )

        assert result.changed is False, command
        assert result.metadata["command_class"] == "file_read", command

    for command in (
        "sed -l -n p test-results.txt || pytest -q",
        "sed -l --quiet p test-results.txt || pytest -q",
        "sed -nl -n p test-results.txt || pytest -q",
        "sed -l p test-results.txt || pytest -q",
        "sed -l '$p' test-results.txt || pytest -q",
        "sed -l s/x/y/ test-results.txt || pytest -q",
        "sed -nl 1,20p test-results.txt || pytest -q",
        "sed -n -l 1,20p test-results.txt || pytest -q",
        "sed -el test-results.txt || pytest -q",
        "sed -fl test-results.txt || pytest -q",
        "sed -l bogus p test-results.txt; true || pytest -q",
    ):
        result = reduce_text(
            pytest_like_source,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=opts(max_chars=300, max_lines=20),
        )

        assert result.changed is False, command
        assert result.metadata["command_class"] == "file_read", command

    later_exact_source = "\n".join(
        [
            "tests/test_api.py::test_case PASSED",
            *[f"def function_{index}(): return {index}" for index in range(180)],
        ]
    )
    command = "sed -l p file.py || pytest -q && cat source.py"
    result = reduce_text(
        later_exact_source,
        command=command,
        tool_name="terminal",
        exit_code=0,
        options=opts(max_chars=300, max_lines=20),
    )
    assert result.changed is False
    assert result.metadata["command_class"] == "file_read"


def test_quoted_pipeline_tokens_remain_exact_file_operands() -> None:
    raw = "\n".join(f"def function_{index}(): return {index}" for index in range(180))

    commands = (
        "cat '|' npm test",
        "cat '|&' pytest -q",
        r"cat \| npm test",
        "cat '>README'",
        "cat '2>/dev/null'",
        "sed -n '1,20p' '>README'",
    )
    for command in commands:
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            options=opts(max_chars=300),
        )

        assert result.changed is False, command
        assert result.metadata["command_class"] == "file_read", command
        assert result.text == raw, command

    for command in ("cat '>README'", "cat '2>/dev/null'", "cat '&>README'"):
        tokens = _shell_tokens(command)
        assert _tokens_redirect_stdout(tokens) is False, command
        assert _tokens_redirect_stderr(tokens) is False, command
        assert _redirected_stream_visibility(tokens) == (True, True), command

    quoted_fd_tokens = _shell_tokens('cat "2" >/dev/null')
    assert _tokens_redirect_stdout(quoted_fd_tokens) is True
    assert _redirected_stream_visibility(quoted_fd_tokens) == (False, True)

    readwrite_stdin_tokens = _shell_tokens("cat file.py <>input.txt")
    assert _tokens_redirect_stdout(readwrite_stdin_tokens) is False
    assert _redirected_stream_visibility(readwrite_stdin_tokens) == (True, True)
    readwrite_stdout_tokens = _shell_tokens("cat file.py 1<>artifact.txt")
    assert _tokens_redirect_stdout(readwrite_stdout_tokens) is True
    assert _redirected_stream_visibility(readwrite_stdout_tokens) == (False, True)

    for command in ("1<&- cat file.py", "1</dev/null cat file.py"):
        tokens = _shell_tokens(command)
        assert _redirected_stream_visibility(tokens) == (False, True), command
        assert classify_command(command, "def exact_source(): pass") == "generic", command

    separated_numeric = "cat 1 < /dev/null file.py"
    separated_tokens = _shell_tokens(separated_numeric)
    assert _redirected_stream_visibility(separated_tokens) == (True, True)
    assert classify_command(separated_numeric, "def exact_source(): pass") == "file_read"


def test_visible_stdout_redirections_keep_exact_output() -> None:
    source = "\n".join(f"def function_{index}(): return {index}" for index in range(180))
    search_output = "\n".join(
        f"src/module_{index}.py:{index}:def target_{index}():" for index in range(180)
    )
    cases = (
        ("rg target src 2>errors.log", search_output, "source_search"),
        ("cat file.py 2>&-", source, "file_read"),
        ("cat file.py 3>/dev/null", source, "file_read"),
        ("cat file.py 1>/dev/stdout", source, "file_read"),
        ("sed --line-length 80 >/dev/stdout -n 1p file.py", source, "file_read"),
    )

    for command, raw, expected_class in cases:
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            options=opts(max_chars=300, max_lines=20),
        )

        assert result.changed is False, command
        assert result.metadata["command_class"] == expected_class, command
        assert result.text == raw, command

    assert list(_shell_tokens("sudo -u 1000 >/dev/stdout cat file.py")) == [
        "sudo",
        "-u",
        "1000",
        ">",
        "/dev/stdout",
        "cat",
        "file.py",
    ]
    assert list(_shell_tokens("cat file.py 2>/dev/null")) == [
        "cat",
        "file.py",
        "2>",
        "/dev/null",
    ]


def test_env_wrapper_value_options_preserve_exact_children() -> None:
    source = "\n".join(f"def function_{index}(): return {index}" for index in range(180))
    commands = (
        "env -C /tmp cat file.py",
        "env -C/tmp cat file.py",
        "env --chdir /tmp cat file.py",
        "env --chdir=/tmp cat file.py",
        'env -S "cat file.py"',
        'env --split-string "cat file.py"',
        'env --split-string="cat file.py"',
        "env -a custom cat file.py",
        "env -acustom cat file.py",
        "env --argv0 custom cat file.py",
        "env --argv0=custom cat file.py",
        "env -u FOO cat file.py",
        "env -iv cat file.py",
        'env "FOO=bar" cat file.py',
        'env "1=bar" cat file.py',
        'env "A-B=bar" cat file.py',
        'env "=bar" cat file.py',
        "env --argv0= cat file.py",
        "env --split-string= cat file.py",
        "2>/dev/null cat file.py",
        "command 2>/dev/null cat file.py",
        "env 2>/dev/null cat file.py",
    )

    for command in commands:
        result = reduce_text(
            source,
            command=command,
            tool_name="terminal",
            options=opts(max_chars=300, max_lines=20),
        )

        assert result.changed is False, command
        assert result.metadata["command_class"] == "file_read", command

    for command in (
        "env -h cat file.py",
        "env --help cat file.py",
        "env -V cat file.py",
        "env --unknown cat file.py",
        "env -C cat file.py",
        "env -C '' cat file.py",
        "env --chdir '' cat file.py",
        "env -u '' cat file.py",
        "env --unset '' cat file.py",
        "env -f '' cat file.py",
        "env --file '' cat file.py",
        "env --split-string='2>&9 cat file.py'",
        "env -S '2>&9 cat file.py'",
        "env -S '' cat file.py",
    ):
        assert classify_command(command, source) == "generic", command


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
    literal_dollar_search_output = "\n".join(
        f"src/module_{index}.py:{index}:literal $FOO target" for index in range(180)
    )
    literal_substitution_search_output = "\n".join(
        f"src/module_{index}.py:{index}:literal $(fixture) target" for index in range(180)
    )
    literal_process_substitution_search_output = "\n".join(
        f"src/module_{index}.py:{index}:literal <(pytest -q) target" for index in range(180)
    )

    cases = {
        "cat file.py": raw_source,
        "cat < file.py": raw_source,
        "cat <file.py": raw_source,
        "cat file.py 2>/dev/null": raw_source,
        "cat file.py 2> /dev/null": raw_source,
        "cat 'a>b.py' 2>/dev/null": raw_source,
        "cat 'a<b.py' 2>/dev/null": raw_source,
        "cat '<(fixture).py'": raw_source,
        'cat "$(printf file.py)"': raw_source,
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
        "rg target src 2>/dev/null": search_output,
        "rg target src 2> /dev/null": search_output,
        "rg '$FOO' src 2>/dev/null": literal_dollar_search_output,
        "rg '$FOO' src 2> /dev/null": literal_dollar_search_output,
        "rg '$(fixture)' src 2>/dev/null": literal_substitution_search_output,
        "rg '$(fixture)' src 2> /dev/null": literal_substitution_search_output,
        'rg "<(pytest -q)" src': literal_process_substitution_search_output,
        "rg '`fixture`' src 2>/dev/null": search_output,
        "rg target '<(fixture)'": search_output,
        'rg "$(cat pattern.txt)" src': search_output,
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


def test_sudo_value_options_preserve_exact_command_intent() -> None:
    raw_source = "\n".join(
        f"def function_{index}(): return 'FAILED ERROR {index}'" for index in range(160)
    )
    search_output = "\n".join(
        f"src/module_{index}.py:{index}:def target_{index}():" for index in range(180)
    )
    cases = {
        "sudo -a auth cat file.py": (raw_source, "file_read"),
        "sudo -C 3 cat file.py": (raw_source, "file_read"),
        "sudo -C3 rg target src": (search_output, "source_search"),
        "sudo -c login_class cat file.py": (raw_source, "file_read"),
        "sudo -D /root cat file.py": (raw_source, "file_read"),
        "sudo -D/root cat file.py": (raw_source, "file_read"),
        "sudo -nD/root cat file.py": (raw_source, "file_read"),
        "sudo -g staff cat file.py": (raw_source, "file_read"),
        "sudo -p prompt cat file.py": (raw_source, "file_read"),
        "sudo -p '' cat file.py": (raw_source, "file_read"),
        "sudo -p '|' cat file.py": (raw_source, "file_read"),
        "sudo -R/root cat file.py": (raw_source, "file_read"),
        "sudo -r role cat file.py": (raw_source, "file_read"),
        "sudo -t type cat file.py": (raw_source, "file_read"),
        "sudo -T5 cat file.py": (raw_source, "file_read"),
        "sudo -n -Eu root cat file.py": (raw_source, "file_read"),
        "sudo -nEu root rg target src": (search_output, "source_search"),
        "sudo -uroot cat file.py": (raw_source, "file_read"),
        "sudo --auth-type pam cat file.py": (raw_source, "file_read"),
        "sudo --close-from 3 cat file.py": (raw_source, "file_read"),
        "sudo --clo 3 cat file.py": (raw_source, "file_read"),
        "sudo --login-class default cat file.py": (raw_source, "file_read"),
        "sudo --chdir=/root cat file.py": (raw_source, "file_read"),
        "sudo --chd /root cat file.py": (raw_source, "file_read"),
        "sudo --group staff cat file.py": (raw_source, "file_read"),
        "sudo --host localhost cat file.py": (raw_source, "file_read"),
        "sudo --prompt prompt cat file.py": (raw_source, "file_read"),
        "sudo --prompt '' cat file.py": (raw_source, "file_read"),
        "sudo --prompt '|' cat file.py": (raw_source, "file_read"),
        "sudo --prompt= cat file.py": (raw_source, "file_read"),
        "sudo --prom= cat file.py": (raw_source, "file_read"),
        "sudo --chroot /root cat file.py": (raw_source, "file_read"),
        "sudo --role role cat file.py": (raw_source, "file_read"),
        "sudo --type type cat file.py": (raw_source, "file_read"),
        "sudo --command-timeout 5 cat file.py": (raw_source, "file_read"),
        "sudo --user root cat file.py": (raw_source, "file_read"),
        "sudo --non cat file.py": (raw_source, "file_read"),
        "sudo --res cat file.py": (raw_source, "file_read"),
        "sudo -H -P rg target src": (search_output, "source_search"),
    }

    observed: dict[str, tuple[bool, object]] = {}
    for command, (raw, _expected_class) in cases.items():
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            options=opts(max_chars=400),
        )
        observed[command] = (result.changed, result.metadata["command_class"])

    assert observed == {
        command: (False, expected_class)
        for command, (_raw, expected_class) in cases.items()
    }


def test_sudo_value_options_preserve_compactable_command_intent() -> None:
    raw = package_progress("underlying command output")
    cases = {
        "sudo -a auth pytest -q": "pytest",
        "sudo -C 3 pytest -q": "pytest",
        "sudo -C3 apt-get update": "apt",
        "sudo -D /root pytest -q": "pytest",
        "sudo -D/root apt-get update": "apt",
        "sudo -nD/root pytest -q": "pytest",
        "sudo -R/root apt-get update": "apt",
        "sudo -T5 pytest -q": "pytest",
        "sudo -n -Eu root pytest -q": "pytest",
        "sudo -nEu root apt-get update": "apt",
        "sudo -uroot pytest -q": "pytest",
        "sudo --chdir /root pytest -q": "pytest",
        "sudo --chd /root pytest -q": "pytest",
        "sudo --command-timeout=5 apt-get update": "apt",
        "sudo -H -P apt-get update": "apt",
    }

    observed: dict[str, tuple[bool, object]] = {}
    for command in cases:
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            options=opts(max_chars=400),
        )
        observed[command] = (result.changed, result.metadata["command_class"])

    assert observed == {
        command: (True, expected_class) for command, expected_class in cases.items()
    }


def test_sudo_short_options_remain_case_sensitive() -> None:
    raw_source = "\n".join(f"def function_{index}(): return {index}" for index in range(160))

    assert classify_command("sudo -P cat file.py", raw_source) == "file_read"
    assert classify_command("sudo -p cat file.py", raw_source) == "generic"


def test_sudo_nonexecuting_modes_do_not_expose_apparent_child_commands() -> None:
    raw_source = "\n".join(f"def function_{index}(): return {index}" for index in range(160))
    nonexecuting_commands = (
        "sudo -nlu root cat file.py",
        "sudo -ne cat file.py",
        "sudo -nl cat file.py",
        "sudo -nv cat file.py",
        "sudo -nV cat file.py",
        "sudo -nK cat file.py",
        "sudo --edit cat file.py",
        "sudo --list cat file.py",
        "sudo --validate cat file.py",
        "sudo --version cat file.py",
        "sudo --help cat file.py",
        "sudo --remove-timestamp cat file.py",
        "sudo --ed cat file.py",
        "sudo --lis cat file.py",
        "sudo --val cat file.py",
        "sudo --vers cat file.py",
        "sudo --hel cat file.py",
        "sudo --rem cat file.py",
        "sudo --ch /root cat file.py",
        "sudo --pres cat file.py",
        "sudo --l cat file.py",
        "sudo --v cat file.py",
        "sudo --user '' cat file.py",
        "sudo -u '' cat file.py",
        "sudo -U other cat file.py",
        "sudo -Uother cat file.py",
        "sudo --other-user other cat file.py",
        "sudo --other-user=other cat file.py",
        "sudo -T '' cat file.py",
        "sudo --command-timeout '' cat file.py",
        "sudo --command-timeout= cat file.py",
        "sudo -h",
    )

    for command in nonexecuting_commands:
        assert classify_command(command, raw_source) == "generic", command

    for command in (
        "sudo -k cat file.py",
        "sudo --reset-timestamp cat file.py",
        "sudo -urootV cat file.py",
        "sudo -pvalidate cat file.py",
    ):
        assert classify_command(command, raw_source) == "file_read", command


def test_sudo_shell_layouts_preserve_valid_exact_children() -> None:
    raw_source = "\n".join(f"def function_{index}(): return {index}" for index in range(180))
    commands = (
        "sudo -u root 2>/dev/null cat file.py",
        r"sudo -u \2>/dev/stdout cat file.py",
        'sudo -u "2">/dev/stdout cat file.py',
        "sudo <>/dev/null cat file.py",
        "sudo 3<>/dev/null cat file.py",
        "sudo FOO=bar -u root cat file.py",
        "sudo -u root FOO=bar cat file.py",
        "sudo -p '2>/dev/null' cat file.py",
        "sudo --prompt='2>/dev/null' cat file.py",
        "sudo -h localhost cat file.py",
        "sudo -hlocalhost cat file.py",
        "sudo -u 1000 >/dev/stdout cat file.py",
    )

    for command in commands:
        result = reduce_text(
            raw_source,
            command=command,
            tool_name="terminal",
            options=opts(max_chars=300),
        )

        assert result.changed is False, command
        assert result.metadata["command_class"] == "file_read", command


def test_invalid_sudo_forms_do_not_protect_fallback_output() -> None:
    pytest_raw = "\n".join(
        [
            "sudo: invalid invocation",
            *[f"tests/test_{index}.py::test_case_{index} PASSED" for index in range(180)],
            "================ 180 passed in 2.00s ================",
        ]
    )
    commands = (
        "sudo -T1h2h cat file.py || pytest -q",
        "sudo --command-timeout=1s2 cat file.py || pytest -q",
        "sudo -h -n cat file.py || pytest -q",
        "sudo -h -- cat file.py || pytest -q",
        "sudo -h FOO=bar cat file.py || pytest -q",
        "sudo --host=-n cat file.py || pytest -q",
        "sudo --host=FOO=bar cat file.py || pytest -q",
        "sudo -- FOO=bar cat file.py || pytest -q",
        "sudo -Z cat file.py || pytest -q",
        "sudo -d legacy cat file.py || pytest -q",
        "sudo -iE cat file.py || pytest -q",
        "sudo -u root -u admin cat file.py || pytest -q",
    )

    for command in commands:
        result = reduce_text(
            pytest_raw,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=opts(max_chars=300, max_lines=20),
        )

        assert result.changed is True, command
        assert result.metadata["command_class"] == "pytest", command


def test_quoted_assignment_syntax_does_not_expose_an_apparent_child() -> None:
    pytest_raw = "\n".join(
        [
            "sh: FOO=bar: command not found",
            *[f"tests/test_{index}.py::test_case_{index} PASSED" for index in range(180)],
            "================ 180 passed in 2.00s ================",
        ]
    )
    invalid_commands = (
        '"FOO=bar" cat file.py || pytest -q',
        r"FOO\=bar cat file.py || pytest -q",
        r"F\OO=bar cat file.py || pytest -q",
        'time "FOO=bar" cat file.py || pytest -q',
        'command "FOO=bar" cat file.py || pytest -q',
    )
    for command in invalid_commands:
        result = reduce_text(
            pytest_raw,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=opts(max_chars=300, max_lines=20),
        )

        assert result.changed is True, command
        assert result.metadata["command_class"] == "pytest", command

    source = "\n".join(f"def function_{index}(): return {index}" for index in range(180))
    for command in ('FOO="bar" cat file.py', 'env "FOO=bar" cat file.py'):
        result = reduce_text(
            source,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=opts(max_chars=300, max_lines=20),
        )

        assert result.changed is False, command
        assert result.metadata["command_class"] == "file_read", command


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

def test_local_codex_p2_exact_owner_and_later_dominance_regressions() -> None:
    compact_opts = opts(max_chars=220, max_lines=8, head_lines=1, tail_lines=1)

    fixed_string_source = "\n".join(
        [
            "ERROR: literal source string, not uv output",
            *[f"literal ERROR source line {index}" for index in range(80)],
        ]
    )
    exact_fixed_string = reduce_text(
        fixed_string_source,
        command="rg -F -I ERROR src && uv sync",
        tool_name="terminal",
        exit_code=1,
        options=compact_opts,
    )
    assert exact_fixed_string.changed is False
    assert exact_fixed_string.metadata["command_class"] == "source_search"

    fallback_read = "\n".join(
        [
            "cat: MISSING: No such file or directory",
            "Release notes",
            *[f"plain line {index}" for index in range(80)],
        ]
    )
    exact_fallback_read = reduce_text(
        fallback_read,
        command="cat MISSING || cat README",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert exact_fallback_read.changed is False
    assert exact_fallback_read.metadata["command_class"] == "file_read"

    pytest_success_wall = "\n".join(
        [
            "# Project",
            *[f"plain README line {index}" for index in range(30)],
            *[f"tests/test_{index}.py::test_ok PASSED" for index in range(80)],
            "123 passed in 2.00s",
        ]
    )
    compacted_pytest_success = reduce_text(
        pytest_success_wall,
        command="cat README && pytest -q",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert compacted_pytest_success.changed is True
    assert compacted_pytest_success.metadata["command_class"] == "pytest"
    assert "123 passed" in compacted_pytest_success.text

    docker_success_wall = "\n".join(
        [
            "# Project",
            *[f"plain README line {index}" for index in range(30)],
            "#1 [internal] load build definition from Dockerfile",
            "#1 transferring dockerfile: 2B done",
            "#2 DONE 0.1s",
            *[f"#2 build line {index}" for index in range(50)],
        ]
    )
    compacted_docker_success = reduce_text(
        docker_success_wall,
        command="cat README && docker build .",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert compacted_docker_success.changed is True
    assert compacted_docker_success.metadata["command_class"] == "docker_build"
    assert "DONE" in compacted_docker_success.text

    heading_signal_pytest = "\n".join(
        [
            "src/test_notes.md",
            *[f"123 passed literal source note {index}" for index in range(40)],
            "123 passed in 2.00s",
        ]
    )
    compacted_heading_signal = reduce_text(
        heading_signal_pytest,
        command="rg --heading --no-line-number '123 passed' src && pytest -q",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert compacted_heading_signal.changed is True
    assert compacted_heading_signal.metadata["command_class"] == "pytest"

    compound_failed_read_then_npm = reduce_text(
        "\n".join(
            [
                "cat: MISSING: No such file or directory",
                "# README",
                "intro",
                "added 451 packages in 12s",
                *[f"npm after {index}" for index in range(30)],
            ]
        ),
        command="cat MISSING || cat README && npm install",
        tool_name="terminal",
        exit_code=1,
        options=compact_opts,
    )
    assert compound_failed_read_then_npm.changed is True
    assert compound_failed_read_then_npm.metadata["command_class"] == "node"
    assert "added 451 packages" in compound_failed_read_then_npm.text

    apt_progress_prefixed_with_path = reduce_text(
        "\n".join(
            f"src/file.py | {index}% [Working] apt progress package lists"
            for index in range(80)
        ),
        command="rg target src && apt-get update",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert apt_progress_prefixed_with_path.changed is True
    assert apt_progress_prefixed_with_path.metadata["command_class"] == "apt"

    docker_steps_prefixed_with_path = reduce_text(
        "\n".join(
            f"src/file.py | Step {index}/120 : RUN echo hi" for index in range(80)
        ),
        command="rg target src && docker build .",
        tool_name="terminal",
        exit_code=1,
        options=compact_opts,
    )
    assert docker_steps_prefixed_with_path.changed is True
    assert docker_steps_prefixed_with_path.metadata["command_class"] == "docker_build"

    for command, raw in (
        (
            "cat README && docker build .",
            "\n".join(
                [
                    "# Project",
                    *[f"plain README line {index}" for index in range(30)],
                    *[
                        f"=> [internal] load build definition from Dockerfile {index}"
                        for index in range(80)
                    ],
                ]
            ),
        ),
        (
            "rg target src && docker build .",
            "\n".join(
                f"src/file.py | => [internal] load build definition from Dockerfile {index}"
                for index in range(80)
            ),
        ),
    ):
        compacted_buildkit_progress = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=compact_opts,
        )
        assert compacted_buildkit_progress.changed is True, command
        assert compacted_buildkit_progress.metadata["command_class"] == "docker_build", command

    yarn_cwd_source_search = "\n".join(
        f"src/module_{index}.py:{index}:def target_{index}():" for index in range(80)
    )
    exact_yarn_cwd_search = reduce_text(
        yarn_cwd_source_search,
        command="yarn --cwd web exec rg target src",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert exact_yarn_cwd_search.changed is False
    assert exact_yarn_cwd_search.metadata["command_class"] == "source_search"

    literal = "literal source line with target token"
    exact_hidden_fixed_package_literal = reduce_text(
        "\n".join(literal for _ in range(80)),
        command=f"rg -F -I '{literal}' src || npm install",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert exact_hidden_fixed_package_literal.changed is False
    assert exact_hidden_fixed_package_literal.metadata["command_class"] == "source_search"

    ambiguous_hidden_fixed_package_output = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command="rg -F -I 'added 451 packages in 12s' src || npm install",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert ambiguous_hidden_fixed_package_output.changed is True
    assert ambiguous_hidden_fixed_package_output.metadata["command_class"] == "node"

    for literal, later_command, expected_class in (
        ("added 451 packages in 12s", "npm install", "node"),
        ("123 passed in 2.00s", "pytest -q", "pytest"),
    ):
        for separator in ("&&", ";"):
            compacted_hidden_fixed_signal_literal = reduce_text(
                "\n".join(literal for _ in range(80)),
                command=f"rg -F -I '{literal}' src {separator} {later_command}",
                tool_name="terminal",
                exit_code=0,
                options=compact_opts,
            )
            assert compacted_hidden_fixed_signal_literal.changed is True, literal
            assert (
                compacted_hidden_fixed_signal_literal.metadata["command_class"]
                == expected_class
            ), literal

    def plain_context(literal: str) -> str:
        return "\n".join(
            [
                *[f"plain context before {index}" for index in range(40)],
                literal,
                *[f"plain context after {index}" for index in range(40)],
            ]
        )

    for command, raw, expected_class in (
        (
            "rg -F -I -C40 'added 451 packages in 12s' src && npm install",
            plain_context("added 451 packages in 12s"),
            "source_search",
        ),
        (
            "rg -F -I -C40 '123 passed in 2.00s' src && pytest -q",
            plain_context("123 passed in 2.00s"),
            "source_search",
        ),
        (
            "rg -F -I -C40 'Reading package lists' src && apt-get update",
            plain_context("Reading package lists... Done"),
            "source_search",
        ),
        (
            "rg -F -I -C40 'writing image' src && docker build .",
            plain_context("#3 writing image sha256:abc"),
            "source_search",
        ),
        (
            "rg -I -C40 'added 451 packages in 12s' src && npm install",
            plain_context("added 451 packages in 12s"),
            "source_search",
        ),
        (
            "rg -I -C40 'Reading package lists' src && apt-get update",
            plain_context("Reading package lists... Done"),
            "source_search",
        ),
        ("cat README && npm install", plain_context("added 451 packages in 12s"), "file_read"),
        ("cat README && pytest -q", plain_context("123 passed in 2.00s"), "file_read"),
        (
            "cat README && apt-get update",
            plain_context("Reading package lists... Done"),
            "file_read",
        ),
        ("cat README && docker build .", plain_context("#3 writing image sha256:abc"), "file_read"),
        (
            "cat README && python -m pip install requests",
            plain_context("Successfully installed requests-2.32.0"),
            "file_read",
        ),
        ("cat README && uv sync", plain_context("Audited 157 packages in 0.13ms"), "file_read"),
    ):
        exact_context_with_compactable_literal = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert exact_context_with_compactable_literal.changed is False, command
        assert exact_context_with_compactable_literal.text == raw, command
        assert (
            exact_context_with_compactable_literal.metadata["command_class"] == expected_class
        ), command

    late_failed_fallback_read_after_npm = reduce_text(
        "\n".join(
            [
                "cat: MISSING: No such file or directory",
                "npm ERR! code ERESOLVE",
                *[f"npm noise {index}" for index in range(80)],
                "cat: README: No such file or directory",
            ]
        ),
        command="cat MISSING || npm install || cat README",
        tool_name="terminal",
        exit_code=1,
        options=compact_opts,
    )
    assert late_failed_fallback_read_after_npm.changed is True
    assert late_failed_fallback_read_after_npm.metadata["command_class"] == "node"
    assert "npm ERR! code ERESOLVE" in late_failed_fallback_read_after_npm.text

    skipped_read_fallback_after_npm_success = reduce_text(
        "\n".join(
            [
                "cat: MISSING: No such file or directory",
                "added 451 packages in 12s",
                *[f"npm noise {index}" for index in range(80)],
            ]
        ),
        command="cat MISSING || npm install || cat README",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert skipped_read_fallback_after_npm_success.changed is True
    assert skipped_read_fallback_after_npm_success.metadata["command_class"] == "node"
    assert "added 451 packages" in skipped_read_fallback_after_npm_success.text

    executed_exact_fallback_after_npm_failure = reduce_text(
        "\n".join(
            [
                "cat: MISSING: No such file or directory",
                "npm ERR! code ERESOLVE",
                "def fallback():",
                *[f"    return {index}" for index in range(80)],
            ]
        ),
        command="cat MISSING || npm install || cat file.py",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert executed_exact_fallback_after_npm_failure.changed is False
    assert executed_exact_fallback_after_npm_failure.metadata["command_class"] == "file_read"

    executed_exact_tail_after_successful_compactable_fallback = reduce_text(
        "\n".join(
            [
                "cat: MISSING: No such file or directory",
                "added 451 packages in 12s",
                "found 0 vulnerabilities",
                "def fallback():",
                *[f"    return {index}" for index in range(80)],
            ]
        ),
        command="cat MISSING || npm install; cat file.py",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert executed_exact_tail_after_successful_compactable_fallback.changed is False
    assert (
        executed_exact_tail_after_successful_compactable_fallback.metadata["command_class"]
        == "file_read"
    )

    executed_exact_and_tail_after_successful_compactable_fallback = reduce_text(
        "\n".join(
            [
                "cat: MISSING: No such file or directory",
                "added 451 packages in 12s",
                "found 0 vulnerabilities",
                "def fallback():",
                *[f"    return {index}" for index in range(80)],
            ]
        ),
        command="cat MISSING || npm install && cat file.py",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert executed_exact_and_tail_after_successful_compactable_fallback.changed is False
    assert (
        executed_exact_and_tail_after_successful_compactable_fallback.metadata[
            "command_class"
        ]
        == "file_read"
    )

    skipped_compactable_after_successful_read_fallback = reduce_text(
        "\n".join(
            [
                "cat: MISSING: No such file or directory",
                "# README",
                *["added 451 packages in 12s" for _ in range(80)],
            ]
        ),
        command="cat MISSING || cat README || npm install",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert skipped_compactable_after_successful_read_fallback.changed is False
    assert (
        skipped_compactable_after_successful_read_fallback.metadata["command_class"]
        == "file_read"
    )

    ambiguous_compactable_after_search_fallback = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command="rg missing src || rg -F -I 'added 451 packages in 12s' docs || npm install",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert ambiguous_compactable_after_search_fallback.changed is True
    assert ambiguous_compactable_after_search_fallback.metadata["command_class"] == "node"

    skipped_compactable_after_successful_hidden_search_left = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command='rg -I "added 451 packages" src || { npm install; exit 7; }',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert skipped_compactable_after_successful_hidden_search_left.changed is False
    assert (
        skipped_compactable_after_successful_hidden_search_left.metadata["command_class"]
        == "source_search"
    )

    redirected_noisy_prefix_then_source_search = reduce_text(
        "\n".join(
            f"src/app.py:{index}:FAILED literal source hit" for index in range(80)
        ),
        command="npm install >/dev/null && rg FAILED src",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert redirected_noisy_prefix_then_source_search.changed is False
    assert redirected_noisy_prefix_then_source_search.metadata["command_class"] == "source_search"

    background_source_search_with_redirected_compactable_foreground = reduce_text(
        "\n".join(
            f"src/app.py:{index}:FAILED literal source hit" for index in range(80)
        ),
        command="rg FAILED src & pytest -q >/dev/null",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert background_source_search_with_redirected_compactable_foreground.changed is False
    assert (
        background_source_search_with_redirected_compactable_foreground.metadata[
            "command_class"
        ]
        == "source_search"
    )

    hidden_background_source_search_owns_stdout_shaped_literal = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command='rg -F -I "added 451 packages in 12s" src & npm install >/dev/null',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert hidden_background_source_search_owns_stdout_shaped_literal.changed is False
    assert (
        hidden_background_source_search_owns_stdout_shaped_literal.metadata["command_class"]
        == "source_search"
    )

    ambiguous_successful_or_fallback_with_compactable_output = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command='rg -I "added 451 packages" src || npm install',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert ambiguous_successful_or_fallback_with_compactable_output.changed is True
    assert (
        ambiguous_successful_or_fallback_with_compactable_output.metadata["command_class"]
        == "node"
    )

    successful_intervening_fallback_skips_compactable_tail = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command='rg -I "added 451 packages" src || true || npm install',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert successful_intervening_fallback_skips_compactable_tail.changed is False
    assert (
        successful_intervening_fallback_skips_compactable_tail.metadata["command_class"]
        == "source_search"
    )

    substitution_with_successful_or_guard_skips_compactable_tail = reduce_text(
        "\n".join(
            f"tests/test_demo.py:{index}:FAILED tests/test_demo.py::test_signal"
            for index in range(80)
        ),
        command='rg "$(pytest -q)" src || true || npm install',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert substitution_with_successful_or_guard_skips_compactable_tail.changed is True
    assert (
        substitution_with_successful_or_guard_skips_compactable_tail.metadata[
            "command_class"
        ]
        == "pytest"
    )

    compactable_group_before_true_still_owns_output = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command='rg -I "added 451 packages" src || { npm install; true; } || npm install',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert compactable_group_before_true_still_owns_output.changed is True
    assert compactable_group_before_true_still_owns_output.metadata["command_class"] == "node"

    guarded_exit_does_not_make_compactable_fallback_unreachable = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command='rg -I "added 451 packages" src || { npm install || exit 7; } || npm install',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert guarded_exit_does_not_make_compactable_fallback_unreachable.changed is True
    assert (
        guarded_exit_does_not_make_compactable_fallback_unreachable.metadata["command_class"]
        == "node"
    )

    shell_terminating_or_fallback_keeps_successful_exact_left = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command='rg -I "added 451 packages" src || { npm install; exit 7; } || npm install',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert shell_terminating_or_fallback_keeps_successful_exact_left.changed is False
    assert (
        shell_terminating_or_fallback_keeps_successful_exact_left.metadata["command_class"]
        == "source_search"
    )

    compact_shell_terminating_or_fallback_keeps_successful_exact_left = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command='rg -I "added 451 packages" src || { npm install; exit 7;}|| npm install',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert compact_shell_terminating_or_fallback_keeps_successful_exact_left.changed is False
    assert (
        compact_shell_terminating_or_fallback_keeps_successful_exact_left.metadata[
            "command_class"
        ]
        == "source_search"
    )

    for exit_status in (0, 7):
        top_level_exit_makes_later_fallback_unreachable = reduce_text(
            "\n".join("added 451 packages in 12s" for _ in range(80)),
            command=(
                f'rg -I "added 451 packages" src || exit {exit_status} || npm install'
            ),
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert top_level_exit_makes_later_fallback_unreachable.changed is False, exit_status
        assert (
            top_level_exit_makes_later_fallback_unreachable.metadata["command_class"]
            == "source_search"
        ), exit_status

    for command, expected_class in (
        (
            'rg -I "added 451 packages" src '
            "|| { exit 7; npm install; } || npm install",
            "source_search",
        ),
        (
            'rg -I "added 451 packages" src '
            "&& { exit 0; npm install; } && npm install",
            "source_search",
        ),
        ("cat README && { exit 0; npm install; } && npm install", "file_read"),
    ):
        unreachable_commands_after_exit = reduce_text(
            "\n".join("added 451 packages in 12s" for _ in range(80)),
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert unreachable_commands_after_exit.changed is False, command
        assert unreachable_commands_after_exit.metadata["command_class"] == expected_class, command

    for failed_fallback in ("false", "( exit 7 )", "{ false; }"):
        reachable_second_fallback = reduce_text(
            "\n".join("added 451 packages in 12s" for _ in range(80)),
            command=(
                'rg -F -I "added 451 packages in 12s" src '
                f"|| {failed_fallback} || npm install"
            ),
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert reachable_second_fallback.changed is True, failed_fallback
        assert reachable_second_fallback.metadata["command_class"] == "node", failed_fallback

    warning_fallback_output = "\n".join(
        f"npm WARN deprecated package-{index}" for index in range(80)
    )
    warning_fallback_without_search_hits = reduce_text(
        warning_fallback_output,
        command="rg ERROR src || npm install",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert warning_fallback_without_search_hits.changed is True
    assert warning_fallback_without_search_hits.metadata["command_class"] == "node"

    warning_lines_that_match_search_pattern = reduce_text(
        warning_fallback_output,
        command="rg deprecated src || npm install",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert warning_lines_that_match_search_pattern.changed is False
    assert warning_lines_that_match_search_pattern.metadata["command_class"] == "source_search"

    for command in (
        "npm install & rg target src",
        "npm install & cat README",
    ):
        background_package_output_before_exact_tail = reduce_text(
            warning_fallback_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert background_package_output_before_exact_tail.changed is True, command
        assert (
            background_package_output_before_exact_tail.metadata["command_class"] == "node"
        ), command

    for package_manager_output in (
        warning_fallback_output,
        "\n".join(
            ["npm error code ERESOLVE"]
            + [f"npm error dependency noise {index}" for index in range(80)]
        ),
        "\n".join(
            ["pnpm error code ERESOLVE"]
            + [f"pnpm error dependency noise {index}" for index in range(80)]
        ),
    ):
        for command in (
            "cat README && npm install",
            "rg target src && npm install",
        ):
            foreground_package_output_after_exact_command = reduce_text(
                package_manager_output,
                command=command,
                tool_name="terminal",
                exit_code=1,
                options=compact_opts,
            )
            assert foreground_package_output_after_exact_command.changed is True, command
            assert (
                foreground_package_output_after_exact_command.metadata["command_class"]
                == "node"
            ), command

    exact_package_literal_output = "\n".join(
        "added 451 packages in 12s" for _ in range(80)
    )
    for command, expected_class in (
        (
            'rg -F -I "added 451 packages in 12s" src || '
            "npm install >/dev/null 2>&1",
            "source_search",
        ),
        ("cat package.log && { npm install; } >/dev/null 2>&1", "file_read"),
        ('cat package.log && echo "$(npm install)" >/dev/null', "file_read"),
        (
            "cat package.log && { true && exit 0; npm install; } && npm install",
            "file_read",
        ),
        (
            "cat package.log && { false || exit 0; npm install; } && npm install",
            "file_read",
        ),
        (
            'rg -F -I "added 451 packages in 12s" src && '
            'echo "$(npm install)" >/dev/null',
            "source_search",
        ),
    ):
        hidden_compactable_output_keeps_exact_owner = reduce_text(
            exact_package_literal_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert hidden_compactable_output_keeps_exact_owner.changed is False, command
        assert (
            hidden_compactable_output_keeps_exact_owner.metadata["command_class"]
            == expected_class
        ), command

    for command in (
        "npm install && true || cat README",
        "npm install && false || cat README",
        "npm install || true && cat README",
        "npm install || false && cat README",
    ):
        skipped_or_unowned_file_read = reduce_text(
            exact_package_literal_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert skipped_or_unowned_file_read.changed is True, command
        assert skipped_or_unowned_file_read.metadata["command_class"] == "node", command

    plain_read_after_compactable = "\n".join(
        f"plain README line {index}" for index in range(80)
    )
    executed_file_read_after_compactable = reduce_text(
        plain_read_after_compactable,
        command="npm install && cat README",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert executed_file_read_after_compactable.changed is False
    assert executed_file_read_after_compactable.metadata["command_class"] == "file_read"

    for command in (
        "npm install >/dev/null && cat README",
        "{ npm install; } >/dev/null && cat README",
        "npm install 2>&1 >/dev/null && cat README",
    ):
        hidden_earlier_compactable_keeps_later_file_read = reduce_text(
            exact_package_literal_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert hidden_earlier_compactable_keeps_later_file_read.changed is False, command
        assert (
            hidden_earlier_compactable_keeps_later_file_read.metadata["command_class"]
            == "file_read"
        ), command

    skipped_grouped_fallback_keeps_exact_source_search = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command='rg -I "added 451 packages" src || { npm install; npm install; exit 7; }',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert skipped_grouped_fallback_keeps_exact_source_search.changed is False
    assert (
        skipped_grouped_fallback_keeps_exact_source_search.metadata["command_class"]
        == "source_search"
    )

    skipped_grouped_fallback_keeps_exact_file_read = reduce_text(
        "\n".join(f"plain README line {index}" for index in range(80)),
        command="cat README || { npm install; npm install; exit 7; }",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert skipped_grouped_fallback_keeps_exact_file_read.changed is False
    assert skipped_grouped_fallback_keeps_exact_file_read.metadata["command_class"] == "file_read"

    ambiguous_successful_grouped_fallback_with_compactable_output = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command='rg -I "added 451 packages" src || { false; npm install; }',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert ambiguous_successful_grouped_fallback_with_compactable_output.changed is True
    assert (
        ambiguous_successful_grouped_fallback_with_compactable_output.metadata[
            "command_class"
        ]
        == "node"
    )

    unconditional_compactable_after_forced_nonzero_fallback = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command='rg -I "added 451 packages" src || { npm install; exit 7; } && npm install',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert unconditional_compactable_after_forced_nonzero_fallback.changed is True
    assert (
        unconditional_compactable_after_forced_nonzero_fallback.metadata["command_class"]
        == "node"
    )

    compact_semicolon_after_grouped_fallback = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command='rg -I "added 451 packages" src || { npm install; exit 7;}; npm install',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert compact_semicolon_after_grouped_fallback.changed is True
    assert compact_semicolon_after_grouped_fallback.metadata["command_class"] == "node"

    compact_background_after_grouped_fallback = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command='rg -I "added 451 packages" src || { npm install; exit 7; } & npm install',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert compact_background_after_grouped_fallback.changed is True
    assert compact_background_after_grouped_fallback.metadata["command_class"] == "node"

    unconditional_compactable_after_successful_read_left = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command="cat README || false; npm install",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert unconditional_compactable_after_successful_read_left.changed is True
    assert (
        unconditional_compactable_after_successful_read_left.metadata["command_class"]
        == "node"
    )

    skipped_substitution_fallback_keeps_exact_file_read = reduce_text(
        "\n".join(f"plain README line {index}" for index in range(80)),
        command='cat README || rg "$(pytest -q)" src',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert skipped_substitution_fallback_keeps_exact_file_read.changed is False
    assert (
        skipped_substitution_fallback_keeps_exact_file_read.metadata["command_class"]
        == "file_read"
    )

    skipped_substitution_fallback_keeps_exact_source_search = reduce_text(
        "\n".join(f"src/app.py:{index}:target source hit" for index in range(80)),
        command='rg target src || rg "$(pytest -q)" src',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert skipped_substitution_fallback_keeps_exact_source_search.changed is False
    assert (
        skipped_substitution_fallback_keeps_exact_source_search.metadata[
            "command_class"
        ]
        == "source_search"
    )

    active_substitution_search_with_later_segment = reduce_text(
        "\n".join(
            f"tests/test_demo.py:{index}:FAILED tests/test_demo.py::test_signal"
            for index in range(80)
        ),
        command='rg "$(pytest -q)" src && true',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert active_substitution_search_with_later_segment.changed is True
    assert active_substitution_search_with_later_segment.metadata["command_class"] == "pytest"

    active_substitution_with_later_node_output = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command='rg "$(pytest -q)" src && npm install',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert active_substitution_with_later_node_output.changed is True
    assert active_substitution_with_later_node_output.metadata["command_class"] == "node"

    active_substitution_with_later_file_read_output = reduce_text(
        "\n".join(f"plain README line {index}" for index in range(80)),
        command='rg "$(pytest -q)" src && cat README',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert active_substitution_with_later_file_read_output.changed is False
    assert (
        active_substitution_with_later_file_read_output.metadata["command_class"]
        == "file_read"
    )

    active_substitution_with_later_source_search_output = reduce_text(
        "\n".join(f"src/app.py:{index}:target source hit" for index in range(80)),
        command='rg "$(pytest -q)" src && rg target src',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert active_substitution_with_later_source_search_output.changed is False
    assert (
        active_substitution_with_later_source_search_output.metadata["command_class"]
        == "source_search"
    )

    background_source_search_with_redirected_compactable_stderr = reduce_text(
        "\n".join(
            [
                "npm ERR! code ERESOLVE",
                *[f"npm ERR! dependency noise {index}" for index in range(80)],
            ]
        ),
        command="rg NOMATCH src & npm install >/dev/null",
        tool_name="terminal",
        exit_code=1,
        options=compact_opts,
    )
    assert background_source_search_with_redirected_compactable_stderr.changed is True
    assert (
        background_source_search_with_redirected_compactable_stderr.metadata[
            "command_class"
        ]
        == "node"
    )

    background_source_search_with_unknown_redirected_compactable = reduce_text(
        "\n".join(
            f"src/app.py:{index}:FAILED literal source hit" for index in range(80)
        ),
        command="rg FAILED src & npm install >/dev/null",
        tool_name="terminal",
        exit_code=None,
        options=compact_opts,
    )
    assert background_source_search_with_unknown_redirected_compactable.changed is False
    assert (
        background_source_search_with_unknown_redirected_compactable.metadata[
            "command_class"
        ]
        == "source_search"
    )

    background_source_search_with_intervening_segment = reduce_text(
        "\n".join(
            f"src/app.py:{index}:FAILED literal source hit" for index in range(80)
        ),
        command="rg FAILED src & true & npm install >/dev/null",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert background_source_search_with_intervening_segment.changed is False
    assert (
        background_source_search_with_intervening_segment.metadata["command_class"]
        == "source_search"
    )

    background_source_search_with_amp_redirected_compactable = reduce_text(
        "\n".join(
            f"src/app.py:{index}:FAILED literal source hit" for index in range(80)
        ),
        command="rg FAILED src & npm install &>/dev/null",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert background_source_search_with_amp_redirected_compactable.changed is False
    assert (
        background_source_search_with_amp_redirected_compactable.metadata[
            "command_class"
        ]
        == "source_search"
    )

    background_redirected_compactable_stderr_with_unknown_exit = reduce_text(
        "\n".join(
            [
                "npm ERR! code ERESOLVE",
                *[f"npm ERR! dependency noise {index}" for index in range(80)],
            ]
        ),
        command="rg NOMATCH src & npm install >/dev/null",
        tool_name="terminal",
        exit_code=None,
        options=compact_opts,
    )
    assert background_redirected_compactable_stderr_with_unknown_exit.changed is True
    assert (
        background_redirected_compactable_stderr_with_unknown_exit.metadata[
            "command_class"
        ]
        == "node"
    )

    file_read_then_redirected_compactable_stderr = reduce_text(
        "\n".join(
            [
                "npm ERR! code ERESOLVE",
                *[f"npm ERR! dependency noise {index}" for index in range(80)],
            ]
        ),
        command="cat README && npm install >/dev/null",
        tool_name="terminal",
        exit_code=1,
        options=compact_opts,
    )
    assert file_read_then_redirected_compactable_stderr.changed is True
    assert file_read_then_redirected_compactable_stderr.metadata["command_class"] == "node"

    mixed_file_read_and_redirected_compactable_stderr = reduce_text(
        "\n".join(
            [
                "# README",
                "plain project documentation",
                "npm ERR! code ERESOLVE",
                *[f"npm ERR! dependency noise {index}" for index in range(80)],
            ]
        ),
        command="cat README && npm install >/dev/null",
        tool_name="terminal",
        exit_code=1,
        options=compact_opts,
    )
    assert mixed_file_read_and_redirected_compactable_stderr.changed is True
    assert (
        mixed_file_read_and_redirected_compactable_stderr.metadata["command_class"]
        == "node"
    )

    background_file_read_then_redirected_compactable_stderr = reduce_text(
        "\n".join(
            [
                "npm ERR! code ERESOLVE",
                *[f"npm ERR! dependency noise {index}" for index in range(80)],
            ]
        ),
        command="cat README & npm install >/dev/null",
        tool_name="terminal",
        exit_code=1,
        options=compact_opts,
    )
    assert background_file_read_then_redirected_compactable_stderr.changed is True
    assert (
        background_file_read_then_redirected_compactable_stderr.metadata[
            "command_class"
        ]
        == "node"
    )

    fd_dup_redirection_does_not_create_fake_background_segment = reduce_text(
        "\n".join(f"plain README line {index}" for index in range(80)),
        command="cat README & npm install 2>&1 >/dev/null",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert fd_dup_redirection_does_not_create_fake_background_segment.changed is False
    assert (
        fd_dup_redirection_does_not_create_fake_background_segment.metadata[
            "command_class"
        ]
        == "file_read"
    )

    failed_file_read_with_hidden_stderr_runs_compactable_fallback = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command="cat MISSING 2>/dev/null || npm install",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert failed_file_read_with_hidden_stderr_runs_compactable_fallback.changed is True
    assert (
        failed_file_read_with_hidden_stderr_runs_compactable_fallback.metadata[
            "command_class"
        ]
        == "node"
    )

    shell_terminating_fallback_proves_hidden_stderr_read_succeeded = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command="cat README 2>/dev/null || { true; exit 7; } || npm install",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert shell_terminating_fallback_proves_hidden_stderr_read_succeeded.changed is False
    assert (
        shell_terminating_fallback_proves_hidden_stderr_read_succeeded.metadata[
            "command_class"
        ]
        == "file_read"
    )

    zero_exit_fallback_can_own_hidden_stderr_read_output = reduce_text(
        "\n".join("added 451 packages in 12s" for _ in range(80)),
        command="cat MISSING 2>/dev/null || { npm install; exit 0; } || npm install",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert zero_exit_fallback_can_own_hidden_stderr_read_output.changed is True
    assert zero_exit_fallback_can_own_hidden_stderr_read_output.metadata["command_class"] == "node"

    background_source_search_with_nonzero_redirected_compactable = reduce_text(
        "\n".join(
            f"src/app.py:{index}:ERROR literal source hit" for index in range(80)
        ),
        command="rg ERROR src & npm install >/dev/null",
        tool_name="terminal",
        exit_code=1,
        options=compact_opts,
    )
    assert background_source_search_with_nonzero_redirected_compactable.changed is False
    assert (
        background_source_search_with_nonzero_redirected_compactable.metadata[
            "command_class"
        ]
        == "source_search"
    )

    for raw in (
        "\n".join(
            f"src/file.py | {index}% [Connecting to deb.debian.org]" for index in range(80)
        ),
        "\n".join("src/file.py | Reading package lists... Done" for _ in range(80)),
        "\n".join("src/file.py | Building dependency tree... Done" for _ in range(80)),
        "\n".join("src/file.py | Reading state information... Done" for _ in range(80)),
        "\n".join("src/file.py | Fetched 123 kB in 1s" for _ in range(80)),
        "\n".join("src/file.py | Setting up pkg (1.0)" for _ in range(80)),
    ):
        connecting_apt_progress = reduce_text(
            raw,
            command="rg target src && apt-get update",
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert connecting_apt_progress.changed is True
        assert connecting_apt_progress.metadata["command_class"] == "apt"

        read_then_apt_progress = reduce_text(
            raw,
            command="cat README && apt-get update",
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert read_then_apt_progress.changed is True
        assert read_then_apt_progress.metadata["command_class"] == "apt"

    read_then_apt_install_state = reduce_text(
        "\n".join(
            [
                "# README",
                *[f"plain readme line {index}" for index in range(10)],
                *[
                    f"src/file.py | 0 upgraded, 1 newly installed, 0 to remove {index}"
                    for index in range(80)
                ],
            ]
        ),
        command="cat README && apt-get install jq",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert read_then_apt_install_state.changed is True
    assert read_then_apt_install_state.metadata["command_class"] == "apt"

    for raw in (
        "\n".join(f"src/file.py | #{index} DONE 0.{index}s" for index in range(80)),
        "\n".join(f"src/file.py | #2 exporting layers {index}" for index in range(80)),
        "\n".join(f"src/file.py | #3 writing image sha256:{index}" for index in range(80)),
        "\n".join(f"src/file.py | #2 exporting layers 0.{index}s done" for index in range(80)),
        "\n".join(f"src/file.py | => DONE 0.{index}s" for index in range(80)),
        "\n".join(f"src/file.py | => load metadata for image {index}" for index in range(80)),
        "\n".join(f"src/file.py | => load .dockerignore {index}" for index in range(80)),
        "\n".join(f"src/file.py | => exporting layers 0.{index}s done" for index in range(80)),
        "\n".join(
            f"src/file.py | => CACHED [builder 2/5] {index}" for index in range(80)
        ),
    ):
        compacted_buildkit_state = reduce_text(
            raw,
            command="rg target src && docker build .",
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert compacted_buildkit_state.changed is True
        assert compacted_buildkit_state.metadata["command_class"] == "docker_build"

        read_then_buildkit_state = reduce_text(
            raw,
            command="cat README && docker build .",
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert read_then_buildkit_state.changed is True
        assert read_then_buildkit_state.metadata["command_class"] == "docker_build"

    for command, raw, expected_class, expected_signal in (
        (
            "rg -F 'added 451 packages' src; npm install",
            "\n".join(
                ["added 451 packages in 12s", *[f"npm noise {index}" for index in range(80)]]
            ),
            "node",
            "added 451 packages",
        ),
        (
            "rg -F '123 passed' src; pytest -q",
            "\n".join(
                [
                    *[f"tests/test_{index}.py::test_ok PASSED" for index in range(80)],
                    "123 passed in 2.00s",
                ]
            ),
            "pytest",
            "123 passed",
        ),
        (
            "rg -F 'failed to solve' src; docker build .",
            "\n".join(["failed to solve: bad", *[f"#1 noise {index}" for index in range(80)]]),
            "docker_build",
            "failed to solve",
        ),
        (
            "rg -F 'Resolved 192 packages' src; uv sync",
            "\n".join(
                ["Resolved 192 packages in 1ms", *[f"uv noise {index}" for index in range(80)]]
            ),
            "python_package",
            "Resolved 192 packages",
        ),
        (
            "rg -F 'Reading package lists' src; apt-get update",
            "\n".join(
                ["Reading package lists... Done", *[f"apt progress {index}" for index in range(80)]]
            ),
            "apt",
            "Reading package lists",
        ),
        (
            "rg -F 'load metadata' src; docker build .",
            "\n".join(
                [
                    "#1 [internal] load metadata for image",
                    *[f"#1 noise {index}" for index in range(80)],
                ]
            ),
            "docker_build",
            "load metadata",
        ),
        (
            "rg -F 'load .dockerignore' src; docker build .",
            "\n".join(
                [
                    "#1 [internal] load .dockerignore",
                    *[f"#1 noise {index}" for index in range(80)],
                ]
            ),
            "docker_build",
            "load .dockerignore",
        ),
        (
            "rg -F 'writing image' src; docker build .",
            "\n".join(
                ["#3 writing image sha256:abc", *[f"#3 noise {index}" for index in range(80)]]
            ),
            "docker_build",
            "writing image",
        ),
        (
            "rg -F -I 'npm ERR! code ERESOLVE' src && npm install",
            "\n".join(
                [
                    *[f"npm progress before {index}" for index in range(80)],
                    "npm ERR! code ERESOLVE",
                    *[f"npm progress after {index}" for index in range(80)],
                ]
            ),
            "node",
            "npm ERR! code ERESOLVE",
        ),
        (
            "rg -F -I 'ERROR: No matching distribution found' src && uv sync",
            "\n".join(
                [
                    *[f"uv progress before {index}" for index in range(80)],
                    "ERROR: No matching distribution found",
                    *[f"uv progress after {index}" for index in range(80)],
                ]
            ),
            "python_package",
            "No matching distribution found",
        ),
        (
            "rg -F -I '0 upgraded, 1 newly installed' src && apt install jq",
            "\n".join(
                [
                    *[f"apt progress before {index}" for index in range(80)],
                    "0 upgraded, 1 newly installed, 0 to remove and 0 not upgraded.",
                    *[f"apt progress after {index}" for index in range(80)],
                ]
            ),
            "apt",
            "newly installed",
        ),
        (
            "rg -F ERROR src && uv sync",
            "\n".join(
                [
                    *[f"uv progress before {index}" for index in range(80)],
                    "ERROR: No matching distribution found for missing-package",
                    "ERROR: Could not build wheels for missing-package",
                    *[f"uv progress after {index}" for index in range(80)],
                ]
            ),
            "python_package",
            "No matching distribution found",
        ),
    ):
        compacted_later_output = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=compact_opts,
        )
        assert compacted_later_output.changed is True, command
        assert compacted_later_output.metadata["command_class"] == expected_class, command
        assert expected_signal in compacted_later_output.text, command

    double_failed_read_then_npm = reduce_text(
        "\n".join(
            [
                "cat: MISSING: No such file or directory",
                "cat: README: No such file or directory",
                "added 451 packages in 12s",
                *[f"npm noise {index}" for index in range(80)],
            ]
        ),
        command="cat MISSING || cat README || npm install",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert double_failed_read_then_npm.changed is True
    assert double_failed_read_then_npm.metadata["command_class"] == "node"
    assert "added 451 packages" in double_failed_read_then_npm.text

    for raw in (
        "\n".join(f"src/file.py | Get:{index} http://deb.example pkg" for index in range(80)),
        "\n".join(f"src/file.py | {index}% [Working]" for index in range(80)),
    ):
        compacted_apt = reduce_text(
            raw,
            command="rg target src && apt-get update",
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert compacted_apt.changed is True
        assert compacted_apt.metadata["command_class"] == "apt"

    compacted_buildkit = reduce_text(
        "\n".join(
            f"src/file.py | #{index} [internal] load build definition from Dockerfile"
            for index in range(80)
        ),
        command="rg target src && docker build .",
        tool_name="terminal",
        exit_code=1,
        options=compact_opts,
    )
    assert compacted_buildkit.changed is True
    assert compacted_buildkit.metadata["command_class"] == "docker_build"

    for command in (
        "pnpm -F app exec rg target src",
        "pnpm exec -F app rg target src",
        "yarn workspace web exec rg target src",
    ):
        exact_runner_search = reduce_text(
            yarn_cwd_source_search,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert exact_runner_search.changed is False, command
        assert exact_runner_search.metadata["command_class"] == "source_search", command


def test_tight_failure_excerpt_marks_gap_between_noncontiguous_anchors() -> None:
    raw_stdout = "\n".join(
        [
            "ModuleNotFoundError: No module named missing_pkg",
            *[f"pytest setup noise {index}" for index in range(16)],
            "FAILED tests/test_demo.py::test_signal",
        ]
    )

    result = reduce_text(
        raw_stdout,
        command="pytest -q",
        tool_name="terminal",
        exit_code=1,
        options=NoisegateOptions(max_chars=220, max_lines=5),
    )

    assert result.changed is True
    assert "ModuleNotFoundError: No module named missing_pkg" in result.text
    assert "FAILED tests/test_demo.py::test_signal" in result.text
    assert "[noisegate: omitted 16 lines]" in result.text
    assert result.text.index("ModuleNotFoundError") < result.text.index("[noisegate: omitted")
    assert result.text.index("[noisegate: omitted") < result.text.index("FAILED")


def test_redirect_visibility_controls_exact_output_ownership() -> None:
    compact_opts = opts(max_chars=220, max_lines=8, head_lines=1, tail_lines=1)
    package_literal_output = "\n".join(
        "added 451 packages in 12s" for _ in range(80)
    )

    hidden_search_tail = reduce_text(
        package_literal_output,
        command=(
            'rg -F -I "added 451 packages in 12s" src '
            "&& npm install >/dev/null"
        ),
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert hidden_search_tail.changed is False
    assert hidden_search_tail.text == package_literal_output
    assert hidden_search_tail.metadata["command_class"] == "source_search"

    hidden_read_tail = reduce_text(
        package_literal_output,
        command="cat README; npm install >/dev/null",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert hidden_read_tail.changed is False
    assert hidden_read_tail.text == package_literal_output
    assert hidden_read_tail.metadata["command_class"] == "file_read"

    for fully_hidden_redirect in (
        "&>/dev/null",
        ">&/dev/null",
        ">& /dev/null",
        ">/dev/null 2>&1",
    ):
        hidden_bash_combined_redirect = reduce_text(
            package_literal_output,
            command=f"cat build.log && npm install {fully_hidden_redirect}",
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert hidden_bash_combined_redirect.changed is False, fully_hidden_redirect
        assert hidden_bash_combined_redirect.text == package_literal_output
        assert hidden_bash_combined_redirect.metadata["command_class"] == "file_read"

    for redirect in (">&2", ">&1", ">/dev/stdout", ">/dev/stderr", "1>&2"):
        visible_background_tail = reduce_text(
            package_literal_output,
            command=f'rg -I "foo" src & npm install {redirect}',
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert visible_background_tail.changed is True, redirect
        assert visible_background_tail.metadata["command_class"] == "node", redirect

    exact_search_output = "\n".join(
        f"src/app.py:{index}:target source hit" for index in range(80)
    )
    exact_read_output = "\n".join(
        f"plain README line {index}" for index in range(80)
    )
    for exit_code in (0, None, 1):
        for command, exact_output, expected_class in (
            ("rg target src & npm install >&2", exact_search_output, "source_search"),
            ("cat README & npm install >&2", exact_read_output, "file_read"),
        ):
            visible_tail_without_own_output = reduce_text(
                exact_output,
                command=command,
                tool_name="terminal",
                exit_code=exit_code,
                options=compact_opts,
            )
            assert visible_tail_without_own_output.changed is False, (command, exit_code)
            assert visible_tail_without_own_output.text == exact_output, (command, exit_code)
            assert visible_tail_without_own_output.metadata["command_class"] == expected_class, (
                command,
                exit_code,
            )

    for exact_read_redirect in ("2>&1", "1>&1", ">&1", "1>&2", ">&2"):
        visible_exact_read_redirect = reduce_text(
            exact_read_output,
            command=f"cat README {exact_read_redirect}",
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert visible_exact_read_redirect.changed is False, exact_read_redirect
        assert visible_exact_read_redirect.text == exact_read_output
        assert visible_exact_read_redirect.metadata["command_class"] == "file_read"

    for package_manager in ("npm", "pnpm"):
        for prefix in ("", " ", "\t", "\u2009"):
            redirected_stderr = "\n".join(
                [
                    "src/app.py:1:FAILED literal source hit",
                    f"{prefix}{package_manager} error code ERESOLVE",
                    *[
                        f"{prefix}{package_manager} error dependency noise {index}"
                        for index in range(80)
                    ],
                ]
            )
            for exit_code in (0, None, 1):
                visible_redirected_error = reduce_text(
                    redirected_stderr,
                    command=f"rg FAILED src & {package_manager} install >/dev/null",
                    tool_name="terminal",
                    exit_code=exit_code,
                    options=compact_opts,
                )
                assert visible_redirected_error.changed is True, (
                    package_manager,
                    prefix,
                    exit_code,
                )
                assert visible_redirected_error.metadata["command_class"] == "node", (
                    package_manager,
                    prefix,
                    exit_code,
                )
    for prefix in ("", " ", "\u2009"):
        pnpm_redirected_stderr = "\n".join(
            [
                "# README",
                "plain docs",
                f"{prefix}ERR_PNPM_FETCH_404 GET https://registry.npmjs.org/missing: Not Found",
                *[f"{prefix}ERR_PNPM dependency noise {index}" for index in range(80)],
            ]
        )
        for command in (
            "cat README && pnpm install >/dev/null",
            "rg FAILED src & pnpm install >/dev/null",
        ):
            visible_pnpm_error = reduce_text(
                pnpm_redirected_stderr,
                command=command,
                tool_name="terminal",
                exit_code=1,
                options=compact_opts,
            )
            assert visible_pnpm_error.changed is True, (prefix, command)
            assert visible_pnpm_error.metadata["command_class"] == "node", (prefix, command)

    stylized_pnpm_warnings = "\n".join(
        f"\u2009WARN\u2009 deprecated package-{index}" for index in range(80)
    )
    stylized_visible_stderr = reduce_text(
        stylized_pnpm_warnings,
        command="cat README && pnpm install >/dev/null",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert stylized_visible_stderr.changed is True
    assert stylized_visible_stderr.metadata["command_class"] == "node"

    for prefix in (" ", "\t", "\u2009"):
        indented_npm_warnings = "\n".join(
            ["# README", "plain docs"]
            + [f"{prefix}npm WARN dependency noise {index}" for index in range(80)]
        )
        for connector in ("&&", ";", "&"):
            visible_indented_stderr = reduce_text(
                indented_npm_warnings,
                command=f"cat README {connector} npm install >/dev/null",
                tool_name="terminal",
                exit_code=0,
                options=compact_opts,
            )
            assert visible_indented_stderr.changed is True, (prefix, connector)
            assert visible_indented_stderr.metadata["command_class"] == "node", (
                prefix,
                connector,
            )

    visible_stderr_exact_left = reduce_text(
        package_literal_output,
        command="cat README 2>&1 || npm install",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert visible_stderr_exact_left.changed is False
    assert visible_stderr_exact_left.text == package_literal_output
    assert visible_stderr_exact_left.metadata["command_class"] == "file_read"

    fallback_node_output = "\n".join(
        ["npm error code ERESOLVE", *["added 451 packages in 12s" for _ in range(80)]]
    )
    hidden_middle_failed_then_visible_fallback = reduce_text(
        fallback_node_output,
        command="cat README && npm install >/dev/null || npm install",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert hidden_middle_failed_then_visible_fallback.changed is True
    assert hidden_middle_failed_then_visible_fallback.metadata["command_class"] == "node"

    npm_warning_output = "\n".join(
        f"npm WARN deprecated package-{index}" for index in range(80)
    )
    visible_stderr_after_duplication = reduce_text(
        npm_warning_output,
        command="cat README && npm install 2>&1 >/dev/null",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert visible_stderr_after_duplication.changed is True
    assert visible_stderr_after_duplication.metadata["command_class"] == "node"

    fully_hidden_tail = reduce_text(
        npm_warning_output,
        command="cat README && npm install >/dev/null 2>&1",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert fully_hidden_tail.changed is False
    assert fully_hidden_tail.text == npm_warning_output
    assert fully_hidden_tail.metadata["command_class"] == "file_read"

    hidden_error_output = "\n".join(
        ["npm ERR! code ERESOLVE", *[f"npm ERR! dependency noise {index}" for index in range(220)]]
    )
    for command, expected_class in (
        ("cat build.log && npm install >/dev/null 2>&1", "file_read"),
        (
            "rg -F -I 'npm ERR!' src && npm install >/dev/null 2>&1",
            "source_search",
        ),
    ):
        fully_hidden_error_tail = reduce_text(
            hidden_error_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert fully_hidden_error_tail.changed is False, command
        assert fully_hidden_error_tail.text == hidden_error_output, command
        assert fully_hidden_error_tail.metadata["command_class"] == expected_class, command

    for command in (
        "cat empty.txt && npm install >/dev/null && npm install",
        "rg target src && npm install >/dev/null && npm install",
    ):
        visible_tail_after_hidden_middle = reduce_text(
            package_literal_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert visible_tail_after_hidden_middle.changed is True, command
        assert visible_tail_after_hidden_middle.metadata["command_class"] == "node", command


def test_active_substitution_in_later_segment_does_not_steal_prior_exact_output() -> None:
    compact_opts = opts(max_chars=220, max_lines=8, head_lines=1, tail_lines=1)
    read_output = "\n".join(f"plain README line {index}" for index in range(80))
    search_output = "\n".join(
        f"src/app.py:{index}:target source hit" for index in range(80)
    )

    for separator in ("&&", ";", "&"):
        exact_read = reduce_text(
            read_output,
            command=f'cat README {separator} rg "$(pytest -q)" src',
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert exact_read.changed is False, separator
        assert exact_read.text == read_output, separator
        assert exact_read.metadata["command_class"] == "file_read", separator

        exact_search = reduce_text(
            search_output,
            command=f'rg target src {separator} rg "$(pytest -q)" src',
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert exact_search.changed is False, separator
        assert exact_search.text == search_output, separator
        assert exact_search.metadata["command_class"] == "source_search", separator

    for command, expected_class, exact_output in (
        (
            'cat README || { rg "$(pytest -q)" src; exit 7; } || npm install',
            "file_read",
            read_output,
        ),
        (
            'rg target src || { rg "$(pytest -q)" src; exit 7; } || npm install',
            "source_search",
            search_output,
        ),
    ):
        skipped_grouped_substitution = reduce_text(
            exact_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert skipped_grouped_substitution.changed is False, command
        assert skipped_grouped_substitution.text == exact_output, command
        assert skipped_grouped_substitution.metadata["command_class"] == expected_class, command

    pytest_output = "\n".join(
        [
            "Traceback (most recent call last):",
            *[f"pytest setup noise {index}" for index in range(80)],
            "AssertionError: substitution failed",
            "FAILED tests/test_demo.py::test_signal",
        ]
    )
    visible_substitution_failure = reduce_text(
        pytest_output,
        command='cat README && rg "$(pytest -q)" src',
        tool_name="terminal",
        exit_code=1,
        options=compact_opts,
    )
    assert visible_substitution_failure.changed is True
    assert visible_substitution_failure.metadata["command_class"] == "pytest"
    assert "AssertionError: substitution failed" in visible_substitution_failure.text


def test_direct_dynamic_file_reads_require_noncompactable_ordinary_substitutions() -> None:
    compact_opts = opts(max_chars=180, max_lines=8, head_lines=1, tail_lines=1)
    plain_output = "\n".join(f"opaque payload record {index}" for index in range(100))

    for command in (
        "cat README # <(printf ignored)", 'cat README "# <(printf literal)"',
        "cat README \\\n# <(printf ignored)",
        'cat "$(cat path.txt)"',
        "cat $(cat path.txt)",
        'cat "$(printf %s fixtures)/$(cat path.txt)"',
        'cat "$(printf \'# )\'; cat path.txt)"',
    ):
        exact_read = reduce_text(
            plain_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )

        assert exact_read.changed is False, command
        assert exact_read.text == plain_output, command
        assert exact_read.metadata["command_class"] == "file_read", command

    pytest_output = "\n".join(
        [
            *[f"pytest substitution noise {index}" for index in range(100)],
            "FAILED tests/test_demo.py::test_signal",
        ]
    )
    for command in (
        'cat "$(pytest -q)"',
        'cat "$(cat path.txt # )\npytest -q\n)"',
        'cat "$(cat path.txt \\\n# )\npytest -q\n)"',
    ):
        compactable_substitution = reduce_text(
            pytest_output,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=compact_opts,
        )

        assert compactable_substitution.changed is True, command
        assert compactable_substitution.metadata["command_class"] == "pytest", command
        assert "FAILED tests/test_demo.py::test_signal" in compactable_substitution.text, command

    compound_dynamic_read = reduce_text(
        plain_output,
        command='echo heading && cat "$(cat path.txt)"',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )

    assert compound_dynamic_read.changed is True
    assert compound_dynamic_read.metadata["command_class"] == "generic"

    source_shaped_output = "\n".join(
        f"def generated_{index}(): return {index}" for index in range(100)
    )
    for command in (
        "cat <(cat path.txt)", "cat >(cat path.txt)",
        "cat README foo#<(printf active)", "cat README # ignored\ncat <(printf active)",
        "cat README foo\\\n#<(printf active)",
    ):
        process_substitution = reduce_text(
            source_shaped_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )

        assert process_substitution.changed is True, command
        assert process_substitution.metadata["command_class"] == "generic", command

def test_direct_hidden_dynamic_searches_use_command_specific_filename_flags() -> None:
    compact_opts = opts(max_chars=180, max_lines=8, head_lines=1, tail_lines=1)
    hidden_output = "\n".join(f"opaque matching record {index}" for index in range(100))

    for command in (
        'rg -I "$(cat pattern.txt)" "$(cat path.txt)"',
        "rg -I $(cat pattern.txt) $(cat path.txt)",
        'rg --no-filename "$(cat pattern.txt)" "$(cat path.txt)"',
        'rg -nI "$(cat pattern.txt)" "$(cat path.txt)"',
        'grep -h "$(cat pattern.txt)" "$(cat path.txt)"',
    ):
        exact_search = reduce_text(
            hidden_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )

        assert exact_search.changed is False, command
        assert exact_search.text == hidden_output, command
        assert exact_search.metadata["command_class"] == "source_search", command

    pytest_output = "\n".join(
        [
            *[f"pytest dynamic-pattern noise {index}" for index in range(100)],
            "FAILED tests/test_demo.py::test_signal",
        ]
    )
    for command in (
        'rg -I "$(pytest -q)" src',
        'rg --no-filename "$(pytest -q)" src',
        'rg -nI "$(pytest -q)" src',
        'grep -h "$(pytest -q)" src',
    ):
        compactable_substitution = reduce_text(
            pytest_output,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=compact_opts,
        )

        assert compactable_substitution.changed is True, command
        assert compactable_substitution.metadata["command_class"] == "pytest", command

    for command in ('rg -h', 'rg -h "$(cat pattern.txt)" src'):
        ripgrep_help = reduce_text(
            hidden_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )

        assert ripgrep_help.changed is True, command
        assert ripgrep_help.metadata["command_class"] == "generic", command

    for command in (
        'true && rg -I "$(cat pattern.txt)" "$(cat path.txt)"',
        "rg -I target <(cat paths.txt)",
        "grep -h target >(cat paths.txt)",
    ):
        indirect_dynamic_search = reduce_text(
            hidden_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )

        assert indirect_dynamic_search.changed is True, command
        assert indirect_dynamic_search.metadata["command_class"] == "generic", command


def test_concatenated_process_substitutions_never_prove_exact_ownership() -> None:
    compact_opts = opts(max_chars=180, max_lines=8, head_lines=1, tail_lines=1)
    package_output = "added 451 packages in 12s\n" + package_progress("npm fallback", 100)

    for command in (
        "cat $UNSET<(printf source.py) || npm install",
        "rg -I target $UNSET>(printf paths.txt) || npm install",
    ):
        result = reduce_text(
            package_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )

        assert result.changed is True, command
        assert result.metadata["command_class"] == "node", command


def test_dynamic_search_filename_mode_uses_the_last_effective_flag() -> None:
    compact_opts = opts(max_chars=180, max_lines=8, head_lines=1, tail_lines=1)
    hidden_output = "\n".join(f"opaque matching record {index}" for index in range(100))

    hidden_commands = (
        'rg -HI "$(cat pattern.txt)" "$(cat path.txt)"',
        'grep -Hh "$(cat pattern.txt)" "$(cat path.txt)"',
        'rg --with-filename --no-filename "$(cat pattern.txt)" "$(cat path.txt)"',
        'grep --with-filename --no-filename "$(cat pattern.txt)" "$(cat path.txt)"',
        'grep -h --group-separator -H "$(cat pattern.txt)" "$(cat path.txt)"',
        'rg -I --type -H "$(cat pattern.txt)" "$(cat path.txt)"',
        'rg -Ir -H "$(cat pattern.txt)" "$(cat path.txt)"',
        'grep -he -H "$(cat path.txt)"',
    )
    visible_commands = (
        'rg -IH "$(cat pattern.txt)" "$(cat path.txt)"',
        'grep -hH "$(cat pattern.txt)" "$(cat path.txt)"',
        'rg --no-filename --with-filename "$(cat pattern.txt)" "$(cat path.txt)"',
        'grep --no-filename --with-filename "$(cat pattern.txt)" "$(cat path.txt)"',
        'grep -h --group-separator -H -H "$(cat pattern.txt)" "$(cat path.txt)"',
        'rg -I --type -H -H "$(cat pattern.txt)" "$(cat path.txt)"',
        'rg -Ir -H -H "$(cat pattern.txt)" "$(cat path.txt)"',
        'grep -he -H -H "$(cat path.txt)"',
    )
    for command in hidden_commands + visible_commands:
        result = reduce_text(
            hidden_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )

        assert result.changed is (command in visible_commands), command
        expected_class = "generic" if command in visible_commands else "source_search"
        assert result.metadata["command_class"] == expected_class, command
        if command in hidden_commands:
            assert result.text == hidden_output, command


def test_jq_null_input_requires_a_valid_explicit_file_option() -> None:
    compact_opts = opts(max_chars=180, max_lines=8, head_lines=1, tail_lines=1)
    file_output = "\n".join(f"loaded file value {index}" for index in range(100))

    for tool in ("jq", "yq"):
        for option, path in (
            ("--rawfile", "data.txt"),
            ("--slurpfile", "data.json"),
            ("--argfile", "data.json"),
        ):
            command = f"{tool} -n {option} payload {path} '$payload'"
            exact_file_option = reduce_text(
                file_output,
                command=command,
                tool_name="terminal",
                exit_code=0,
                options=compact_opts,
            )

            assert exact_file_option.changed is False, command
            assert exact_file_option.text == file_output, command
            assert exact_file_option.metadata["command_class"] == "file_read", command

    for command in ("jq -Lvendor . data.json", "jq -f filter.jq input.json"):
        explicit_jq_read = reduce_text(
            file_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )

        assert explicit_jq_read.changed is False, command
        assert explicit_jq_read.text == file_output, command
        assert explicit_jq_read.metadata["command_class"] == "file_read", command

    for command in (
        "yq config.yaml", "yq -P -oy sample.json",
        "yq -p yaml config.yaml", "yq -pyaml config.yaml",
        "yq --output-format=json config.yaml", "yq --indent=2 config.yaml",
        "yq eval '.' config.yaml", "yq e '.' config.yaml",
        "yq eval-all '.' config.yaml", "yq ea '.' config.yaml",
        "yq eval config.yaml", "yq e config.yaml",
        "yq eval --from-file filter.yq config.yaml",
        "yq config.toml",
        "yq -f extract config.yaml", "yq --front-matter extract config.yaml",
    ):
        implicit_yq_read = reduce_text(
            file_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )

        assert implicit_yq_read.changed is False, command
        assert implicit_yq_read.text == file_output, command
        assert implicit_yq_read.metadata["command_class"] == "file_read", command

    generated_output = "\n".join(str(index) for index in range(1000))
    for command in (
        "jq -n 'range(0;1000)'", "jq -c --arg x --rawfile .",
        "jq data.json", "jq --library-path vendor .",
        "jq --args '$ARGS.positional' foo bar", "jq --jsonargs '$ARGS.positional' 1 2",
    ):
        generator = reduce_text(
            generated_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )

        assert generator.changed is True, command
        assert generator.metadata["command_class"] == "generic", command
        assert "[noisegate: omitted" in generator.text, command


def test_yq_value_options_leave_single_expressions_on_stdin() -> None:
    raw = "\n".join(str(index) for index in range(1000))
    commands = (
        "yq -p yaml '.'", "yq -pyaml '.'", "yq --input-format=yaml '.'",
        "yq -o json '.'", "yq --output-format=json '.'", "yq -I 2 '.'",
        "yq -I2 '.'", "yq --indent=2 '.'", "yq '.metadata.config.json'",
        "yq eval '.'", "yq e '.'", "yq eval-all '.'", "yq ea '.'",
        "yq -f extract '.'", "yq --front-matter extract '.'",
        "yq --front-matter=extract '.'",
        "yq '.config.toml'", "yq '.metadata.config.hcl'", "yq '.config.ini'",
    )

    assert {command: classify_command(command, raw) for command in commands} == dict.fromkeys(
        commands, "generic"
    )
    for suffix in (
        "yaml", "yml", "y", "kyaml", "ky", "json", "j", "props", "properties", "p",
        "csv", "c", "tsv", "t", "xml", "x", "base64", "uri", "toml", "hcl", "h", "tf",
        "lua", "l", "ini", "i",
    ):
        assert classify_command(f"yq config.{suffix}", raw) == "file_read", suffix


def test_git_grep_follows_global_value_options_only_to_the_subcommand() -> None:
    compact_opts = opts(max_chars=180, max_lines=8, head_lines=1, tail_lines=1)
    search_output = "\n".join(
        f"src/module_{index}.py:{index}:target source hit" for index in range(100)
    )

    for command in (
        "git --git-dir .git grep target",
        "git --git-dir=.git grep target",
        "git --work-tree repo grep target",
        "git --work-tree=repo grep target",
        "git --attr-source HEAD grep target",
        "git --attr-source=HEAD grep target",
        "git --git-dir .git --work-tree repo --attr-source HEAD grep target",
        "git -P grep target",
        "git --no-pager grep target",
        "git --no-advice grep target",
        "git --no-lazy-fetch grep target",
        "git -Crepo grep target",
        "git -ccolor.grep=false grep target",
        "git --namespace ns grep target",
        "git --namespace=ns grep target",
        "git --config-env token=GIT_TOKEN grep target",
        "git --exec-path=/usr/lib/git-core grep target",
        "git --exec-path= grep target",
    ):
        exact_search = reduce_text(
            search_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )

        assert exact_search.changed is False, command
        assert exact_search.text == search_output, command
        assert exact_search.metadata["command_class"] == "source_search", command

    for command in (
        "git --git-dir .git status",
        "git --git-dir=.git status",
        "git --work-tree repo status",
        "git --work-tree=repo status",
        "git --attr-source HEAD status",
        "git --attr-source=HEAD status",
        "git --no-advice status",
        "git --no-lazy-fetch status",
        "git --exec-path grep target",
        "git --html-path grep target",
        "git --man-path grep target",
        "git --info-path grep target",
    ):
        assert classify_command(command, search_output, exit_code=0) != "source_search", command


def test_unreachable_substitution_branches_do_not_veto_exact_ownership() -> None:
    compact_opts = opts(max_chars=180, max_lines=8, head_lines=1, tail_lines=1)
    plain_output = "\n".join(f"opaque payload record {index}" for index in range(100))

    for command, expected_class in (
        ('cat "$(false && pytest -q; cat path.txt)"', "file_read"),
        (
            'rg -I "$(false && pytest -q; cat pattern.txt)" "$(cat path.txt)"',
            "source_search",
        ),
        ('cat "$(false && cat <(printf ignored); cat path.txt)"', "file_read"),
        ('cat "$(false && echo $(pytest -q); cat path.txt)"', "file_read"),
    ):
        exact = reduce_text(
            plain_output,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert exact.changed is False, command
        assert exact.text == plain_output, command
        assert exact.metadata["command_class"] == expected_class, command

    for command, expected_class in (
        ('cat "$(true ||\nfalse && pytest -q\ncat path.txt)"', "pytest"),
        ('cat "$(false && (true || true) || pytest -q; cat path.txt)"', "pytest"),
        ('cat "$(TRUE || pytest -q; cat path.txt)"', "pytest"),
        ('cat "$(true$(printf x) || pytest -q; cat path.txt)"', "pytest"),
        ('cat "$(true && echo $(pytest -q); cat path.txt)"', "pytest"),
        ('cat "$(true$(printf x) || cat <(printf active); cat path.txt)"', "generic"),
    ):
        unsafe = reduce_text(
            plain_output,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=compact_opts,
        )
        assert unsafe.changed is True, command
        assert unsafe.metadata["command_class"] == expected_class, command

    escaped_backtick = reduce_text(
        plain_output,
        command=r'cat "`true\; || pytest -q; cat path.txt`"',
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert escaped_backtick.changed is True
    assert escaped_backtick.metadata["command_class"] == "pytest"


def test_log_stream_filters_do_not_claim_exact_source_ownership() -> None:
    compact_opts = opts(max_chars=180, max_lines=8, head_lines=1, tail_lines=1)
    log_output = "\n".join(f"ERROR request failed {index}" for index in range(100))

    for command in (
        "journalctl -u api | grep ERROR",
        "kubectl logs pod | rg ERROR",
        "kubectl -n prod logs pod | rg ERROR",
        "kubectl --namespace=prod logs pod | rg ERROR",
    ):
        result = reduce_text(
            log_output,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=compact_opts,
        )
        assert result.changed is True, command
        assert result.metadata["command_class"] == "generic", command

    exact_file_search = reduce_text(
        log_output,
        command="cat api.log | grep ERROR",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert exact_file_search.changed is False
    assert exact_file_search.text == log_output
    assert exact_file_search.metadata["command_class"] == "source_search"


def test_later_failed_test_command_owns_sparse_failure_output() -> None:
    compact_opts = opts(max_chars=180, max_lines=8, head_lines=1, tail_lines=1)
    cases = (
        (
            "cat README && npm test",
            "\n".join(
                [
                    *(f"plain README line {index}" for index in range(100)),
                    "Error: Cannot find module demo",
                ]
            ),
            "node",
        ),
        (
            "cat README && /usr/bin/npm test",
            "\n".join(
                [
                    *(f"plain README line {index}" for index in range(100)),
                    "Error: Cannot find module demo",
                ]
            ),
            "node",
        ),
        (
            "cat README && pytest -q",
            "\n".join(
                [
                    *(f"plain README line {index}" for index in range(100)),
                    "FAILED tests/test_demo.py::test_signal",
                ]
            ),
            "pytest",
        ),
        (
            "cat README && pytest -q",
            "\n".join(
                [
                    *(f"plain README line {index}" for index in range(100)),
                    "FAILED specs/test_demo.py::test_signal",
                ]
            ),
            "pytest",
        ),
    )
    for command, raw, expected_class in cases:
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=compact_opts,
        )
        assert result.changed is True, command
        assert result.metadata["command_class"] == expected_class, command

    literal_read = "\n".join(
        [
            *(f"plain README line {index}" for index in range(100)),
            "FAILED tests/test_demo.py::test_signal",
        ]
    )
    hidden_pytest = reduce_text(
        literal_read,
        command="cat README && pytest -q >/dev/null 2>&1",
        tool_name="terminal",
        exit_code=1,
        options=compact_opts,
    )
    assert hidden_pytest.changed is False
    assert hidden_pytest.text == literal_read
    assert hidden_pytest.metadata["command_class"] == "file_read"

    hidden_npm_stderr = "\n".join(
        [
            *(f"plain README line {index}" for index in range(100)),
            "Error: Cannot find module demo",
        ]
    )
    hidden_npm = reduce_text(
        hidden_npm_stderr,
        command="cat README && npm test 2>/dev/null",
        tool_name="terminal",
        exit_code=1,
        options=compact_opts,
    )
    assert hidden_npm.changed is False
    assert hidden_npm.text == hidden_npm_stderr
    assert hidden_npm.metadata["command_class"] == "file_read"


def test_successful_regex_search_owns_skipped_package_fallback() -> None:
    compact_opts = opts(max_chars=180, max_lines=8, head_lines=1, tail_lines=1)
    search_output = "\n".join(f"Error: boom {index}" for index in range(100))

    exact_search = reduce_text(
        search_output,
        command="grep 'Error: .*' file || npm install",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert exact_search.changed is False
    assert exact_search.text == search_output
    assert exact_search.metadata["command_class"] == "source_search"

    package_output = "\n".join(f"added {100 + index} packages in 12s" for index in range(100))
    case_mismatch_output = "\n".join(
        f"error: fallback diagnostic {index}" for index in range(100)
    )
    for command, raw in (
        ("grep 'Error: .*' file || npm install", package_output),
        ("grep -F 'Error: .*' file || npm install", package_output),
        ("grep 'Error: .*' file || npm install", case_mismatch_output),
    ):
        fallback = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=0,
            options=compact_opts,
        )
        assert fallback.changed is True, command
        assert fallback.metadata["command_class"] == "node", command

    ignored_case = reduce_text(
        case_mismatch_output,
        command="grep -i 'Error: .*' file || npm install",
        tool_name="terminal",
        exit_code=0,
        options=compact_opts,
    )
    assert ignored_case.changed is False
    assert ignored_case.text == case_mismatch_output
    assert ignored_case.metadata["command_class"] == "source_search"


def test_apt_fix_broken_flag_does_not_consume_install_subcommand() -> None:
    compact_opts = opts(max_chars=180, max_lines=8, head_lines=1, tail_lines=1)
    apt_output = "\n".join(
        [
            "Reading package lists... Done",
            *(f"apt package progress {index}" for index in range(100)),
        ]
    )

    for command in (
        "apt-get -f install imaginary-package",
        "apt-get --fix-broken install imaginary-package",
    ):
        result = reduce_text(
            apt_output,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=compact_opts,
        )
        assert result.changed is True, command
        assert result.metadata["command_class"] == "apt", command
