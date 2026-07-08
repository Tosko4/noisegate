# ruff: noqa: E501, RUF001
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from noisegate.engine import NoisegateOptions, reduce_text

REPO_ROOT = Path(__file__).resolve().parents[1]


def options(**overrides: object) -> NoisegateOptions:
    values: dict[str, object] = {
        "max_chars": 900,
        "max_lines": 60,
        "head_lines": 5,
        "tail_lines": 5,
        "important_context_lines": 2,
    }
    values.update(overrides)
    return NoisegateOptions(**values)  # type: ignore[arg-type]


def numbered(prefix: str, count: int) -> list[str]:
    return [f"{prefix} {index:03d}" for index in range(1, count + 1)]


def assert_metadata(
    metadata: Mapping[str, object],
    *,
    reducer: str,
    command_class: str,
    changed: bool = True,
) -> None:
    assert metadata["reducer"] == reducer
    assert metadata["command_class"] == command_class
    original_chars = metadata["original_chars"]
    original_lines = metadata["original_lines"]
    omitted_chars = metadata["omitted_chars"]
    omitted_lines = metadata["omitted_lines"]
    assert isinstance(original_chars, int)
    assert isinstance(original_lines, int)
    assert isinstance(omitted_chars, int)
    assert isinstance(omitted_lines, int)
    assert original_chars > 0
    assert original_lines > 0
    assert omitted_chars >= 0
    assert omitted_lines >= 0
    if changed:
        assert omitted_chars > 0
        assert omitted_lines > 0
    else:
        assert omitted_chars == 0
        assert omitted_lines == 0


def test_agent_code_inspection_keeps_file_reads_exact_and_search_safe() -> None:
    engine_source = (REPO_ROOT / "noisegate" / "engine.py").read_text(encoding="utf-8")
    cat_result = reduce_text(
        engine_source,
        command="cat noisegate/engine.py",
        tool_name="terminal",
        options=options(max_chars=120),
    )

    assert cat_result.changed is False
    assert cat_result.text == engine_source
    assert_metadata(
        cat_result.metadata,
        reducer="protected_file_read",
        command_class="file_read",
        changed=False,
    )

    plugin_source = "\n".join(
        (REPO_ROOT / "noisegate" / "plugin.py").read_text(encoding="utf-8").splitlines()[:260]
    )
    sed_result = reduce_text(
        plugin_source,
        command="sed -n '1,260p' noisegate/plugin.py",
        tool_name="terminal",
        options=options(max_chars=120),
    )

    assert sed_result.changed is False
    assert sed_result.text == plugin_source
    assert_metadata(
        sed_result.metadata,
        reducer="protected_file_read",
        command_class="file_read",
        changed=False,
    )

    rg_output = "\n".join(
        [
            "noisegate/engine.py:179:def reduce_text(",
            "noisegate/plugin.py:21:    reduce_text,",
            *[f"tests/test_generated_{index}.py:{index}:result = reduce_text(raw)" for index in range(80)],
            "tests/test_reducers.py:416:    result = reduce_text(raw, command=\"pytest\")",
        ]
    )
    rg_result = reduce_text(
        rg_output,
        command='rg "reduce_text" noisegate tests',
        tool_name="terminal",
        options=options(max_chars=350, max_lines=20, head_lines=3, tail_lines=3),
    )

    assert rg_result.changed is True
    assert "noisegate/engine.py:179:def reduce_text(" in rg_result.text
    assert "tests/test_reducers.py:416:" in rg_result.text
    assert "[noisegate: omitted" in rg_result.text
    assert_metadata(rg_result.metadata, reducer="search", command_class="search")


def test_agent_pytest_failure_keeps_test_name_path_assertion_and_summary() -> None:
    raw = "\n".join(
        [
            "============================= test session starts =============================",
            "platform linux -- Python 3.12.0, pytest-8.4.0",
            *[f"tests/test_bulk_{index}.py::test_ok PASSED" for index in range(120)],
            "=================================== FAILURES ===================================",
            "______________________ test_reducer_keeps_failure_context ______________________",
            "tests/test_agent_workflows.py:44: in test_reducer_keeps_failure_context",
            "    assert visible == 'important context'",
            "E   AssertionError: expected important context",
            "E   assert 'noise' == 'important context'",
            *[f"captured stdout spam line {index}" for index in range(120)],
            "FAILED tests/test_agent_workflows.py::test_reducer_keeps_failure_context - AssertionError: expected important context",
            "========================= 1 failed, 240 passed in 8.42s =========================",
        ]
    )

    result = reduce_text(
        raw,
        command="uv run python -m pytest -q",
        tool_name="terminal",
        exit_code=1,
        options=options(max_chars=1_200, max_lines=45, head_lines=4, tail_lines=4),
    )

    assert result.changed is True
    assert "test_reducer_keeps_failure_context" in result.text
    assert "tests/test_agent_workflows.py" in result.text
    assert "AssertionError: expected important context" in result.text
    assert "[noisegate: omitted" in result.text
    assert_metadata(result.metadata, reducer="pytest", command_class="pytest")


def test_agent_git_diff_stays_exact_even_when_large() -> None:
    raw = "\n".join(
        [
            "diff --git a/noisegate/engine.py b/noisegate/engine.py",
            "index 1111111..2222222 100644",
            "--- a/noisegate/engine.py",
            "+++ b/noisegate/engine.py",
            "@@ -1,4 +1,4 @@",
            " def reduce_text(raw):",
            "-    return raw",
            "+    return compact(raw)",
            *[f"+exact added context {index}" for index in range(120)],
        ]
    )

    result = reduce_text(
        raw,
        command="git diff",
        tool_name="terminal",
        options=options(max_chars=200, max_lines=20),
    )

    assert result.changed is False
    assert result.text == raw
    assert_metadata(result.metadata, reducer="protected_diff", command_class="git_diff", changed=False)


def test_agent_dependency_install_spam_compacts_but_keeps_resolver_errors() -> None:
    raw = "\n".join(
        [
            "Resolved 184 packages in 1.42s",
            *[f"Downloading package-{index} ({index}.0MiB)" for index in range(80)],
            "  × No solution found when resolving dependencies:",
            "  ╰─▶ Because demo depends on missing-package>=9 and no versions match, resolution failed.",
            "ERROR: Could not find a version that satisfies the requirement missing-package>=9",
            *[f"Installing dependency-{index}" for index in range(80)],
        ]
    )

    for command, expected in (
        ("uv sync", "dependency_install"),
        ("pip install -r requirements.txt", "dependency_install"),
        ("npm install", "dependency_install"),
    ):
        result = reduce_text(
            raw,
            command=command,
            tool_name="terminal",
            exit_code=1,
            options=options(max_chars=900, max_lines=35, head_lines=4, tail_lines=4),
        )

        assert result.changed is True
        assert "No solution found" in result.text
        assert "missing-package>=9" in result.text
        assert "Could not find a version" in result.text
        assert "[noisegate: omitted" in result.text
        assert_metadata(result.metadata, reducer=expected, command_class=expected)


def test_agent_os_package_maintenance_spam_compacts_but_keeps_apt_errors() -> None:
    raw = "\n".join(
        [
            "Hit:1 http://archive.ubuntu.com/ubuntu noble InRelease",
            *[f"Get:{index} http://archive.ubuntu.com/ubuntu package-list-{index} [123 kB]" for index in range(90)],
            "Err:91 http://ppa.launchpad.net/example/ubuntu noble Release",
            "  404  Not Found [IP: 185.125.190.80 80]",
            "W: GPG error: http://example.invalid stable InRelease: The following signatures couldn't be verified",
            "E: The repository 'http://ppa.launchpad.net/example/ubuntu noble Release' does not have a Release file.",
            *[f"Reading package lists... {index}%" for index in range(90)],
        ]
    )

    result = reduce_text(
        raw,
        command="sudo apt-get update",
        tool_name="terminal",
        exit_code=100,
        options=options(max_chars=1_000, max_lines=40, head_lines=4, tail_lines=4),
    )

    assert result.changed is True
    assert "404  Not Found" in result.text
    assert "GPG error" in result.text
    assert "does not have a Release file" in result.text
    assert "[noisegate: omitted" in result.text
    assert_metadata(result.metadata, reducer="os_package", command_class="os_package")


def test_agent_docker_build_layer_spam_compacts_but_keeps_failing_step() -> None:
    raw = "\n".join(
        [
            "#0 building with \"default\" instance using docker driver",
            *[f"#{index} [stage 1/{index}] CACHED" for index in range(1, 80)],
            "#80 [stage 12/12] RUN python -m pytest -q",
            "#80 0.432 FAILED tests/test_container.py::test_smoke - AssertionError: image missing /app",
            "#80 0.433 ERROR: process \"/bin/sh -c python -m pytest -q\" did not complete successfully: exit code: 1",
            "------",
            "> [stage 12/12] RUN python -m pytest -q:",
            "0.432 AssertionError: image missing /app",
            *[f"#{index} exporting layer sha256:deadbeef{index}" for index in range(81, 160)],
        ]
    )

    result = reduce_text(
        raw,
        command="docker build .",
        tool_name="terminal",
        exit_code=1,
        options=options(max_chars=1_000, max_lines=40, head_lines=4, tail_lines=4),
    )

    assert result.changed is True
    assert "RUN python -m pytest -q" in result.text
    assert "FAILED tests/test_container.py::test_smoke" in result.text
    assert "did not complete successfully" in result.text
    assert "[noisegate: omitted" in result.text
    assert_metadata(result.metadata, reducer="docker_build", command_class="docker_build")
