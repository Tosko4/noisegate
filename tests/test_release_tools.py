from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from scripts.release_tools import (
    ReleaseError,
    changelog_notes_for_version,
    check_contributors_file,
    git_contributor_names,
    prepare_release,
    read_versions,
    release_notes_for_version,
    release_pull_request_summary,
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


def test_build_metadata_includes_product_contract_docs() -> None:
    root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    include = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    wheel_force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"][
        "force-include"
    ]
    npm_package = json.loads(
        (root / "npm" / "noisegate" / "package.json").read_text(encoding="utf-8")
    )
    product_contract = (root / "docs" / "product-contract.md").read_text(
        encoding="utf-8"
    )
    npm_product_contract = (
        root / "npm" / "noisegate" / "docs" / "product-contract.md"
    ).read_text(encoding="utf-8")

    assert "/npm" in include
    assert "/docs" in include
    assert wheel_force_include["docs/product-contract.md"] == "docs/product-contract.md"
    assert "docs/product-contract.md" in npm_package["files"]
    assert npm_product_contract == product_contract


def test_standalone_publish_workflows_checkout_requested_tag() -> None:
    root = Path(__file__).resolve().parents[1]
    expected_ref = (
        "ref: ${{ startsWith(inputs.version, 'v') "
        "&& inputs.version || format('v{0}', inputs.version) }}"
    )
    for workflow in ("publish-pypi.yml", "publish-npm.yml"):
        text = (root / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
        assert "Checkout release tag" in text
        assert expected_ref in text


def test_release_notes_include_categorized_prs_and_new_contributors(
    monkeypatch, tmp_path: Path
) -> None:
    write_project(tmp_path, "0.2.0")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [0.2.0] - 2026-07-07\n\n"
        "### Added\n"
        "- Release notes now include PR context.\n",
        encoding="utf-8",
    )
    merged_prs = [
        {
            "number": 1,
            "title": "Old release setup",
            "author": {"login": "Alice"},
            "mergedAt": "2026-07-01T10:00:00Z",
            "mergeCommit": {"oid": "old-sha"},
            "url": "https://github.com/Tosko4/noisegate/pull/1",
            "body": "",
        },
        {
            "number": 7,
            "title": "Add npm installer packaging",
            "author": {"login": "Bob"},
            "mergedAt": "2026-07-07T10:00:00Z",
            "mergeCommit": {"oid": "release-sha"},
            "url": "https://github.com/Tosko4/noisegate/pull/7",
            "body": "## Summary\n- add npm package and PyPI release notes",
        },
        {
            "number": 8,
            "title": "Harden artifact writes",
            "author": {"login": "Alice"},
            "mergedAt": "2026-07-07T11:00:00Z",
            "mergeCommit": {"oid": "security-sha"},
            "url": "https://github.com/Tosko4/noisegate/pull/8",
            "body": "## Summary\n- protect artifact writes under concurrency",
        },
        {
            "number": 9,
            "title": "Docs: clarify install flow",
            "author": {"login": "Charlie"},
            "mergedAt": "2026-07-07T12:00:00Z",
            "mergeCommit": {"oid": "docs-sha"},
            "url": "https://github.com/Tosko4/noisegate/pull/9",
            "body": "README polish",
        },
    ]

    def fake_which(name: str) -> str:
        return f"/usr/bin/{name}"

    def fake_run(argv, **_kwargs):
        if argv[:2] == ["/usr/bin/git", "describe"]:
            return subprocess.CompletedProcess(argv, 0, stdout="v0.1.0\n", stderr="")
        if argv[:2] == ["/usr/bin/git", "rev-list"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="release-sha\nsecurity-sha\ndocs-sha\n",
                stderr="",
            )
        if argv[:3] == ["/usr/bin/gh", "api", "graphql"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "data": {
                            "repository": {
                                "pullRequests": {
                                    "nodes": merged_prs,
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    }
                ),
                stderr="",
            )
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr("scripts.release_tools.shutil.which", fake_which)
    monkeypatch.setattr("scripts.release_tools.subprocess.run", fake_run)

    notes = release_notes_for_version(tmp_path, "0.2.0", repo="Tosko4/noisegate")
    summary = release_pull_request_summary(tmp_path, "0.2.0", repo="Tosko4/noisegate")

    assert "### Added" in notes
    assert "## How to update" in notes
    assert "uvx --from noisegate-hermes noisegate install-hermes" in notes
    assert "npx -p noisegate-hermes noisegate install-hermes" in notes
    assert "install/enable/doctor commands" in notes
    assert "removes any stale `plugins.disabled` entry" in notes
    assert "restart or reload that Hermes process" in notes
    assert "## Included pull requests" in notes
    assert "Release range: `v0.1.0...v0.2.0`." in notes
    assert "### Release / Packaging" in notes
    assert (
        "[#7](https://github.com/Tosko4/noisegate/pull/7) "
        "Add npm installer packaging — @Bob"
    ) in notes
    assert "### Security / Safety" in notes
    assert (
        "[#8](https://github.com/Tosko4/noisegate/pull/8) "
        "Harden artifact writes — @Alice"
    ) in notes
    assert "### Documentation" in notes
    assert (
        "[#9](https://github.com/Tosko4/noisegate/pull/9) "
        "Docs: clarify install flow — @Charlie"
    ) in notes
    assert "https://github.com/Tosko4/noisegate/pull/7\n" not in notes
    assert "https://github.com/Tosko4/noisegate/pull/8\n" not in notes
    assert "https://github.com/Tosko4/noisegate/pull/9\n" not in notes
    assert "## New contributors" in notes
    assert "@Bob made their first merged Noisegate PR in #7" in notes
    assert "@Charlie made their first merged Noisegate PR in #9" in notes
    assert "@Alice made their first" not in notes
    assert [pr.number for pr in summary.included] == [7, 8, 9]
    assert [pr.number for pr in summary.new_contributors] == [7, 9]


def test_release_notes_paginates_merged_prs_for_contributor_history(
    monkeypatch, tmp_path: Path
) -> None:
    write_project(tmp_path, "0.2.0")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [0.2.0] - 2026-07-07\n\n"
        "### Changed\n"
        "- Release notes page through merged PR metadata.\n",
        encoding="utf-8",
    )
    pages = [
        {
            "nodes": [
                {
                    "number": 101,
                    "title": "Clarify README update flow",
                    "author": {"login": "Bob"},
                    "mergedAt": "2026-07-07T10:00:00Z",
                    "mergeCommit": {"oid": "release-sha"},
                    "url": "https://github.com/Tosko4/noisegate/pull/101",
                    "body": "README updates",
                }
            ],
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
        },
        {
            "nodes": [
                {
                    "number": 2,
                    "title": "Initial docs contribution",
                    "author": {"login": "Bob"},
                    "mergedAt": "2026-07-01T10:00:00Z",
                    "mergeCommit": {"oid": "old-sha"},
                    "url": "https://github.com/Tosko4/noisegate/pull/2",
                    "body": "",
                }
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        },
    ]
    graph_argvs: list[list[str]] = []

    def fake_which(name: str) -> str:
        return f"/usr/bin/{name}"

    def fake_run(argv, **_kwargs):
        if argv[:2] == ["/usr/bin/git", "describe"]:
            return subprocess.CompletedProcess(argv, 0, stdout="v0.1.0\n", stderr="")
        if argv[:2] == ["/usr/bin/git", "rev-list"]:
            return subprocess.CompletedProcess(argv, 0, stdout="release-sha\n", stderr="")
        if argv[:3] == ["/usr/bin/gh", "api", "graphql"]:
            graph_argvs.append(argv)
            page = pages.pop(0)
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"data": {"repository": {"pullRequests": page}}}),
                stderr="",
            )
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr("scripts.release_tools.shutil.which", fake_which)
    monkeypatch.setattr("scripts.release_tools.subprocess.run", fake_run)

    summary = release_pull_request_summary(tmp_path, "0.2.0", repo="Tosko4/noisegate")

    assert [pr.number for pr in summary.included] == [101]
    assert summary.new_contributors == []
    assert len(graph_argvs) == 2
    assert "cursor=cursor-1" in graph_argvs[1]


def test_release_pr_categories_use_word_boundaries(monkeypatch, tmp_path: Path) -> None:
    write_project(tmp_path, "0.2.0")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [0.2.0] - 2026-07-07\n\n"
        "### Changed\n"
        "- Release notes categorize PR titles without substring false positives.\n",
        encoding="utf-8",
    )
    merged_prs = [
        {
            "number": 12,
            "title": "Address review feedback",
            "author": {"login": "Alice"},
            "mergedAt": "2026-07-07T10:00:00Z",
            "mergeCommit": {"oid": "review-sha"},
            "url": "https://github.com/Tosko4/noisegate/pull/12",
            "body": "",
        }
    ]

    def fake_which(name: str) -> str:
        return f"/usr/bin/{name}"

    def fake_run(argv, **_kwargs):
        if argv[:2] == ["/usr/bin/git", "describe"]:
            return subprocess.CompletedProcess(argv, 0, stdout="v0.1.0\n", stderr="")
        if argv[:2] == ["/usr/bin/git", "rev-list"]:
            return subprocess.CompletedProcess(argv, 0, stdout="review-sha\n", stderr="")
        if argv[:3] == ["/usr/bin/gh", "api", "graphql"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "data": {
                            "repository": {
                                "pullRequests": {
                                    "nodes": merged_prs,
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    }
                ),
                stderr="",
            )
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr("scripts.release_tools.shutil.which", fake_which)
    monkeypatch.setattr("scripts.release_tools.subprocess.run", fake_run)

    notes = release_notes_for_version(tmp_path, "0.2.0", repo="Tosko4/noisegate")

    assert "### Changed" in notes
    assert "[#12](https://github.com/Tosko4/noisegate/pull/12) Address review feedback" in notes
    assert "### Added" not in notes


def test_manual_release_workflow_respects_protected_main() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "Prepare release metadata for manual release" not in text
    assert "git push origin HEAD:main" not in text
    assert "Manual releases must be prepared through a PR before dispatch." in text
    assert "git push origin \"$TAG\"" in text



def test_npm_oidc_publish_uses_supported_node_version() -> None:
    root = Path(__file__).resolve().parents[1]
    for workflow in ("release.yml", "publish-npm.yml"):
        text = (root / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
        assert "node-version: '24'" in text


def test_release_pypi_publish_is_retry_safe_after_partial_release() -> None:
    root = Path(__file__).resolve().parents[1]
    for workflow in ("release.yml", "publish-pypi.yml"):
        text = (root / ".github" / "workflows" / workflow).read_text(
            encoding="utf-8"
        )
        assert "skip-existing: true" in text

def test_release_workflow_updates_existing_release_notes() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "GH_TOKEN: ${{ github.token }}" in text
    assert "GITHUB_REPOSITORY: ${{ github.repository }}" in text
    assert "pull-requests: read" in text
    assert (
        'uv run python scripts/build_release_notes.py "$RELEASE_TAG" '
        '--repo "$GITHUB_REPOSITORY" --output dist/release-notes.md'
    ) in text
    assert 'gh release edit "$RELEASE_TAG" --notes-file dist/release-notes.md' in text
    assert 'gh release upload "$RELEASE_TAG" dist/* --clobber' in text


def test_git_contributor_names_requires_resolved_git(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("scripts.release_tools.shutil.which", lambda _name: None)

    try:
        git_contributor_names(tmp_path)
    except ReleaseError as exc:
        assert "git executable was not found" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected missing git executable to fail")


def test_git_contributor_names_requires_absolute_resolved_git(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], Path]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs["cwd"]))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Tosko4\x001234+Tosko4@users.noreply.github.com\n",
            stderr="",
        )

    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    monkeypatch.setattr("scripts.release_tools.shutil.which", lambda _name: "tools/git")
    monkeypatch.setattr("scripts.release_tools.subprocess.run", fake_run)

    assert git_contributor_names(tmp_path / "repo") == ["Tosko4"]
    expected_git = str((tmp_path / "tools" / "git").resolve())
    assert calls == [
        (
            [expected_git, "log", "--no-merges", "--format=%aN%x00%aE"],
            tmp_path / "repo",
        )
    ]


def test_git_contributor_names_ignores_synthetic_merge_commits(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Tosko4\x001234+Tosko4@users.noreply.github.com\n",
            stderr="",
        )

    monkeypatch.setattr("scripts.release_tools.shutil.which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr("scripts.release_tools.subprocess.run", fake_run)

    assert git_contributor_names(tmp_path) == ["Tosko4"]
    assert calls == [["/usr/bin/git", "log", "--no-merges", "--format=%aN%x00%aE"]]


def test_git_contributor_names_ignores_codex_automation_author(
    monkeypatch, tmp_path: Path
) -> None:
    def fake_run(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                "Tosko4\x001294707+Tosko4@users.noreply.github.com\n"
                "Codex\x00codex@openai.com\n"
            ),
            stderr="",
        )

    monkeypatch.setattr("scripts.release_tools.shutil.which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr("scripts.release_tools.subprocess.run", fake_run)

    assert git_contributor_names(tmp_path) == ["Tosko4"]


def test_git_contributor_names_wraps_git_log_failure(monkeypatch, tmp_path: Path) -> None:
    def fake_run(argv, **_kwargs):
        raise subprocess.CalledProcessError(128, argv, stderr="not a git repository")

    monkeypatch.setattr("scripts.release_tools.shutil.which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr("scripts.release_tools.subprocess.run", fake_run)

    try:
        git_contributor_names(tmp_path)
    except ReleaseError as exc:
        assert "git log failed while reading contributor names: not a git repository" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected git log failure to be wrapped")


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
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                "Alice\x00123+Alice@users.noreply.github.com\n"
                "Display Name\x00Bob@users.noreply.github.com\n"
                "Alice\x00123+Alice@users.noreply.github.com\n"
            ),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert git_contributor_names(tmp_path) == ["Alice", "Bob"]
    assert "--no-merges" in seen_args
    assert "--format=%aN%x00%aE" in seen_args


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
