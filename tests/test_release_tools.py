from __future__ import annotations

import subprocess
from pathlib import Path

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
    npm_root = root / "npm" / "noisegate"
    npm_root.mkdir(parents=True)
    (npm_root / "package.json").write_text(
        '{\n  "name": "noisegate",\n  "version": "' + version + '"\n}\n',
        encoding="utf-8",
    )
    (npm_root / "package-lock.json").write_text(
        "{\n"
        '  "name": "noisegate",\n'
        f'  "version": "{version}",\n'
        '  "lockfileVersion": 3,\n'
        '  "packages": {\n'
        '    "": {\n'
        '      "name": "noisegate",\n'
        f'      "version": "{version}"\n'
        "    }\n"
        "  }\n"
        "}\n",
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
        "npm/noisegate/package.json": "0.2.0",
        "npm/noisegate/package-lock.json": "0.2.0",
    }
    changelog = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    npm_package = (tmp_path / "npm" / "noisegate" / "package.json").read_text(encoding="utf-8")
    npm_lock = (tmp_path / "npm" / "noisegate" / "package-lock.json").read_text(
        encoding="utf-8"
    )
    assert '"version": "0.2.0"' in npm_package
    assert npm_lock.count('"version": "0.2.0"') == 2
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


def test_git_contributor_names_ignores_merge_commits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(argv, 0, stdout="Tosko4\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert git_contributor_names(tmp_path) == ["Tosko4"]
    assert captured["argv"] == ["git", "log", "--no-merges", "--format=%aN"]
    assert captured["cwd"] == tmp_path


def test_release_scripts_are_executable_from_repo_root() -> None:
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["python", "scripts/check_release.py"],
        cwd=root,
        check=True,
    )
