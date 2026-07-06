from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from scripts.release_tools import (
    ReleaseError,
    changelog_notes_for_version,
    check_contributors_file,
    git_contributor_names,
    prepare_release,
    read_versions,
    validate_release_state,
)


def write_project(root: Path, version: str = "0.1.0") -> None:
    (root / "noisegate").mkdir()
    (root / "noisegate" / "_version.py").write_text(
        f'from __future__ import annotations\n\n__version__ = "{version}"\n',
        encoding="utf-8",
    )
    (root / "noisegate" / "plugin.yaml").write_text(
        f'name: noisegate\nversion: "{version}"\n',
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "noisegate-hermes"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        "version = 1\n"
        "revision = 3\n"
        'requires-python = ">=3.11"\n\n'
        "[[package]]\n"
        'name = "noisegate-hermes"\n'
        f'version = "{version}"\n'
        'source = { editable = "." }\n',
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "### Added\n"
        "- Release automation.\n",
        encoding="utf-8",
    )
    (root / "CONTRIBUTORS.md").write_text(
        "# Contributors\n\n"
        "- Alice\n"
        "- Bob\n",
        encoding="utf-8",
    )


def test_validate_release_state_requires_matching_versions(tmp_path: Path) -> None:
    write_project(tmp_path)
    (tmp_path / "noisegate" / "plugin.yaml").write_text(
        'name: noisegate\nversion: "0.2.0"\n',
        encoding="utf-8",
    )

    try:
        validate_release_state(tmp_path)
    except ReleaseError as exc:
        assert "Version mismatch" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected version mismatch")


def test_prepare_release_updates_all_version_files_and_changelog(tmp_path: Path) -> None:
    write_project(tmp_path)

    notes = prepare_release(tmp_path, "0.2.0", release_date="2026-07-06")

    assert "Release automation." in notes
    assert read_versions(tmp_path) == {
        "pyproject.toml": "0.2.0",
        "noisegate/_version.py": "0.2.0",
        "noisegate/plugin.yaml": "0.2.0",
        "uv.lock": "0.2.0",
    }
    changelog = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.2.0] - 2026-07-06" in changelog
    assert "## [Unreleased]" in changelog
    assert changelog_notes_for_version(tmp_path, "0.2.0").startswith("### Added")


def test_prepare_release_requires_unreleased_notes(tmp_path: Path) -> None:
    write_project(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n",
        encoding="utf-8",
    )

    try:
        prepare_release(tmp_path, "0.2.0", release_date="2026-07-06")
    except ReleaseError as exc:
        assert "Unreleased section has no release notes" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected missing release notes to fail")


def test_validate_release_state_accepts_matching_tag(tmp_path: Path) -> None:
    write_project(tmp_path, "1.2.3")
    result = validate_release_state(
        tmp_path,
        expected_version="1.2.3",
        tag="v1.2.3",
        require_changelog=False,
    )

    assert result.version == "1.2.3"
    assert result.files["pyproject.toml"] == "1.2.3"


def test_check_contributors_file_reports_missing_names(tmp_path: Path) -> None:
    write_project(tmp_path)

    missing = check_contributors_file(tmp_path, contributor_names={"Alice", "Charlie"})

    assert missing == ["Charlie"]


def test_release_scripts_are_executable_from_repo_root() -> None:
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["python", "scripts/check_release.py"],
        cwd=root,
        check=True,
    )


def test_git_contributor_names_ignores_merge_commits(monkeypatch: Any, tmp_path: Path) -> None:
    seen_args: list[str] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen_args.extend(args)
        return subprocess.CompletedProcess(args, 0, stdout="Alice\nBob\nAlice\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert git_contributor_names(tmp_path) == ["Alice", "Bob"]
    assert "--no-merges" in seen_args


def test_git_contributor_names_reports_git_failures(monkeypatch: Any, tmp_path: Path) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            128,
            ["git", "log", "--format=%aN"],
            stderr="fatal: not a git repository",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    try:
        git_contributor_names(tmp_path)
    except ReleaseError as exc:
        assert "git log failed while reading contributor names" in str(exc)
        assert "fatal: not a git repository" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected git failure to be wrapped")


def test_git_contributor_names_reports_missing_root(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    try:
        git_contributor_names(missing)
    except ReleaseError as exc:
        assert "repository root does not exist while reading contributor names" in str(exc)
        assert str(missing) in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected missing root to be wrapped")
